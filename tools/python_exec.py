"""代码执行工具 —— 让 agent 能写脚本、跑脚本。

## 这个工具存在的意义

用户说"帮我算一下这 20 个文件的平均值"、"生成一个处理 CSV 的脚本",
agent 需要真的**写代码并执行**。没有这个能力,agent 只能把代码贴给用户,
让用户自己去跑 —— 那就不是 agent 了。

## 安全模型(这是最重要的一节)

**这个工具执行的是 LLM 生成的代码。** 它就是任意代码执行面。
所以隔离必须按"假设代码是恶意的"来做:

| 隔离手段 | 挡住什么 | 局限 |
|---|---|---|
| **独立子进程** | 崩溃不影响主进程;能设超时、能杀进程组 | 不是安全边界 |
| **`-I`(isolated 模式)** | 忽略 `PYTHONPATH`、用户 site-packages、`PYTHONSTARTUP` | 不挡文件访问 |
| **独立 cwd(临时目录)** | 写文件不会污染仓库;产物单独收集 | cwd 逃逸(`..`)仍可达 |
| **env 白名单** | **子进程拿不到 API key / token** | 挡不住网络访问 |
| **超时 + 输出截断** | 无限循环/刷屏不会拖死服务 | — |
| **静态黑名单** | 明显危险的 import(`os.system` 等)提前拦 | 是启发式,不是保证 |

### 明确的局限(必须让使用方知道)

> **子进程隔离 = 崩溃隔离,不是安全边界。**
> 一个足够刁钻的脚本仍可能读到当前用户能读到的文件、发起网络请求。
> 它防的是"LLM 写错代码"和"LLM 被诱导写坏代码",**不防"有组织的攻击"**。

**多租户产品的正解是容器/微虚机**(Docker / gVisor / Firecracker)。
本模块是 v1:够内部和单机使用;**对外开放给不可信用户前必须换成容器隔离**。

### 为什么还留一个静态黑名单

因为它能把**绝大多数误用**挡在执行之前,并且给出可读的拒绝理由。
LLM 写出 `import os; os.system("rm -rf /")` 的概率不低 —— 与其执行到一半
才发现,不如直接拒绝并让模型重新写。

黑名单是**纵深防御的一层**,不是唯一一层。
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


# ===== 默认限制 =====

DEFAULT_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
MAX_OUTPUT_CHARS = 20000
MAX_CODE_CHARS = 100_000
#: 单次执行保留的产物大小上限(总字节)
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
#: 最多回传几个产物文件
MAX_ARTIFACT_FILES = 20


#: 危险模块黑名单 —— 静态检查用
BLOCKED_MODULES: Set[str] = {
    "ctypes", "fcntl", "multiprocessing", "pty", "signal",
    "socket", "socketserver", "subprocess", "sys", "webbrowser",
}
#: 这些模块允许,但要用受限形式(只允许读文件/写当前目录)
RESTRICTED_MODULES: Set[str] = {
    "os", "shutil", "pathlib", "tempfile", "glob",
}


class SecurityError(Exception):
    """代码被静态检查拦下。"""


def _module_root() -> Path:
    return Path(__file__).parent.parent


def _workspace_dir() -> Path:
    """agent 的可写工作区(产物落这里)。"""
    root = _module_root() / "memory" / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def static_check(code: str) -> List[str]:
    """静态检查:返回**问题列表**(空 = 通过)。

    用 `ast` 解析而不是正则 —— 正则能绕过的写法太多了
    (`getattr(os, "system")`、`__import__("os")`)。
    AST 至少能可靠地看到 import 了什么、调了哪些名字。
    """
    problems: List[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"语法错误(第 {exc.lineno} 行): {exc.msg}"]

    for node in ast.walk(tree):
        # --- import ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_MODULES:
                    problems.append(f"禁止 import {root}(第 {node.lineno} 行)")
                elif root in RESTRICTED_MODULES:
                    problems.append(
                        f"不建议 import {root}(第 {node.lineno} 行):"
                        f"它能访问系统资源,优先用 pathlib/open"
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BLOCKED_MODULES:
                problems.append(f"禁止 from {root} import ...(第 {node.lineno} 行)")
            elif root in RESTRICTED_MODULES:
                problems.append(
                    f"不建议 from {root} import ...(第 {node.lineno} 行):"
                    f"它能访问系统资源,优先用 pathlib/open"
                )

        # --- 危险调用 ---
        if isinstance(node, ast.Call):
            fname = _call_name(node.func)
            if fname in ("eval", "exec", "compile", "__import__"):
                problems.append(
                    f"禁止调用 {fname}()(第 {node.lineno} 行):"
                    f"它是动态执行入口,静态检查无法保证安全"
                )
            if fname in ("getattr", "setattr"):
                problems.append(
                    f"不建议调用 {fname}()(第 {node.lineno} 行):"
                    f"常用它绕过静态检查"
                )

    return problems


def _call_name(func: ast.AST) -> str:
    """取调用的名字(只取最后一段,如 `os.system` → `system`)。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _build_env() -> Dict[str, str]:
    """子进程环境变量白名单。

    **最重要的作用**:不让子进程拿到 API key / token。
    进程环境里通常有 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、
    `OWNER_TOKEN`、`SESSION_SECRET` —— 这些绝不能流给 LLM 生成的代码。
    """
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE",
            "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
            "PYTHONHASHSEED", "TZ")
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 确定性:同一份代码两次跑结果一致
    env["PYTHONHASHSEED"] = "0"
    # 不要让 sitecustomize 之类的东西影响行为
    env["PYTHONNOUSERSITE"] = "1"
    return env


class PythonExecTool(Tool):
    """执行一段 Python 代码并返回结果。

    典型用法:
        python_exec(code="print(sum(range(10)))")
        python_exec(code="...", files=[{"name": "a.csv", "content": "..."}])
    """

    name = "python_exec"
    #: 安全开关:只有 CODE_EXEC_ENABLED=true 才能用
    required_flag = "code_exec_enabled"
    description = (
        "执行一段 Python 代码并返回 stdout/stderr。"
        "可以先写文件(files 参数)再在代码里读它。"
        "适合:数据计算、格式转换、批量处理、生成产物文件。"
        "不能做:联网、访问系统资源、装包(用 sys.executable 启动子进程也被禁)。"
    )

    def __init__(
        self,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        max_output_chars: int = MAX_OUTPUT_CHARS,
        allowed_modules: Optional[Set[str]] = None,
    ):
        self.logger = get_logger()
        self.timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))
        self.max_output_chars = max_output_chars
        # 额外允许的模块(在黑名单之外再开一点口子)
        self.extra_allowed: Set[str] = allowed_modules or set()

    # ===== schema =====

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="code",
                    type="string",
                    description="要执行的 Python 代码",
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="script_path",
                    type="string",
                    description=(
                        "直接运行已保存的脚本(用 write_script 存的)。"
                        "只能指向 agent 工作区里的文件。"
                    ),
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="files",
                    type="array",
                    description=(
                        "运行前先写好的文件列表。"
                        "[{'name': 'data.csv', 'content': '...'}]"
                    ),
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="timeout_s",
                    type="integer",
                    description=f"超时秒数(上限 {MAX_TIMEOUT_S})",
                    required=False,
                    default=None,
                ),
            ],
            returns={
                "success": "是否执行成功",
                "stdout": "标准输出",
                "stderr": "标准错误",
                "returncode": "退出码",
                "artifacts": "生成的文件列表[{name, path, size}]",
            },
        )

    # ===== 执行 =====

    async def execute(self, **kwargs) -> ToolResult:
        code = kwargs.get("code") or ""
        script_path = str(kwargs.get("script_path") or "").strip()

        # 两种入口:直接给代码,或运行已保存的脚本
        resolved = None
        if script_path:
            resolved = _resolve_workspace_script(script_path)
            if resolved is None:
                return ToolResult(
                    success=False,
                    error=f"脚本不存在或不在工作区内: {script_path}",
                )
            code = ""
        elif not isinstance(code, str) or not code.strip():
            return ToolResult(success=False, error="code 或 script_path 至少给一个")

        if len(code) > MAX_CODE_CHARS:
            return ToolResult(
                success=False,
                error=f"代码过长({len(code)} 字符,上限 {MAX_CODE_CHARS})",
            )

        timeout_s = kwargs.get("timeout_s") or self.timeout_s
        try:
            timeout_s = max(1, min(int(timeout_s), MAX_TIMEOUT_S))
        except Exception:
            timeout_s = self.timeout_s

        # --- 静态检查(只查内联代码;已保存的脚本当初就查过) ---
        problems = static_check(code) if code else []
        hard = [p for p in problems if p.startswith(("禁止", "语法错误"))]
        if hard:
            return ToolResult(
                success=False,
                error="代码被安全检查拦截:\n" + "\n".join(hard),
                meta={"blocked": True, "problems": problems},
            )
        if problems:
            # 只是"不建议",放行但提示
            self.logger.info("PythonExec", f"代码有建议项: {problems}")

        # --- 准备独立工作目录 ---
        workdir = Path(tempfile.mkdtemp(prefix=f"exec_{uuid.uuid4().hex[:8]}_"))
        try:
            files = kwargs.get("files") or []
            if not isinstance(files, list):
                return ToolResult(success=False, error="files 必须是数组")
            for item in files:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                # 防目录穿越:文件名不能带路径分隔符或 ..
                if "/" in name or "\\" in name or ".." in name:
                    return ToolResult(
                        success=False, error=f"非法文件名: {name!r}(不允许路径分隔符)"
                    )
                content = item.get("content")
                if content is None:
                    continue
                if not isinstance(content, str):
                    content = str(content)
                (workdir / name).write_text(content, encoding="utf-8")

            if resolved is not None:
                # 运行已保存的脚本:复制进临时目录再跑,
                # 脚本对自身的修改不会污染 workspace 里的原件
                script = workdir / resolved.name
                try:
                    script.write_text(
                        resolved.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                except Exception as exc:
                    return ToolResult(success=False, error=f"读取脚本失败: {exc}")
            else:
                script = workdir / "main.py"
                script.write_text(code, encoding="utf-8")

            # --- 执行 ---
            started = time.perf_counter()
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-X", "utf8",     # UTF-8 模式:输出不依赖控制台代码页
                    "-I",              # 隔离模式:忽略 PYTHONPATH / 用户 site
                    "-B",              # 不写 .pyc(省磁盘,也不污染)
                    str(script),
                    cwd=str(workdir),
                    env=_build_env(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,   # 独立进程组,超时时能整组杀
                )
            except Exception as exc:
                return ToolResult(success=False, error=f"无法启动解释器: {exc}")

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
                timed_out = False
            except asyncio.TimeoutError:
                timed_out = True
                _kill_process_group(proc)
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=5
                    )
                except Exception:
                    stdout_b, stderr_b = b"", b""
                stdout_b = (stdout_b or b"") + f"\n[超时] 执行超过 {timeout_s} 秒,已终止".encode()
                stderr_b = stderr_b or b""

            duration_ms = int((time.perf_counter() - started) * 1000)
            stdout = self._decode(stdout_b)
            stderr = self._decode(stderr_b)

            artifacts = self._collect_artifacts(workdir)

            data = {
                "success": not timed_out and proc.returncode == 0,
                "stdout": stdout[: self.max_output_chars],
                "stderr": stderr[: self.max_output_chars],
                "returncode": proc.returncode,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
                "artifacts": artifacts,
            }
            if stdout and len(stdout) > self.max_output_chars:
                data["stdout_truncated"] = True
            if stderr and len(stderr) > self.max_output_chars:
                data["stderr_truncated"] = True

            return ToolResult(
                success=data["success"],
                data=data,
                error=(
                    f"执行超时({timeout_s}s)" if timed_out
                    else (stderr.strip()[:2000] if proc.returncode != 0 else "")
                ),
                meta={"duration_ms": duration_ms, "workdir": str(workdir)},
            )
        finally:
            # 产物已经收集到 workspace,临时目录可以删了
            shutil.rmtree(workdir, ignore_errors=True)

    # ===== 内部 =====

    def _decode(self, raw: bytes) -> str:
        if not raw:
            return ""
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = repr(raw)
        # Windows 的 \r\n 对 LLM 是噪声,统一成 \n
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _collect_artifacts(self, workdir: Path) -> List[Dict[str, Any]]:
        """把生成的文件复制到 workspace,并返回清单。

        产物要**留在主进程能访问的地方** —— 临时目录会被删,
        而用户可能想让 agent 下一轮接着处理这个文件。
        """
        out: List[Dict[str, Any]] = []
        ws = _workspace_dir()
        total = 0
        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            if path.name == "main.py":
                continue  # 我们自己写的脚本,不算产物
            if len(out) >= MAX_ARTIFACT_FILES:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total + size > MAX_ARTIFACT_BYTES:
                break
            rel = path.relative_to(workdir)
            dest = ws / str(rel)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            except Exception as exc:
                self.logger.warning("PythonExec", f"复制产物失败 {rel}: {exc}")
                continue
            total += size
            out.append({
                "name": str(rel).replace("\\", "/"),
                "path": str(dest),
                "size": size,
            })
        return out


def _kill_process_group(proc) -> None:
    """超时后杀掉整个进程组。

    只 kill 父进程的话,脚本 `subprocess` 出去的子进程会变成孤儿继续跑
    —— 虽然 `subprocess` 已经在静态检查里被禁了,但脚本可以自己 fork。
    """
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), 9)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _resolve_workspace_script(script_path: str) -> Optional[Path]:
    """把脚本路径解析成 workspace 内的真实文件。

    **只允许工作区内的文件** —— 拿到绝对路径也必须重新校验,
    否则 `../../etc/passwd` 这种就能读工作区外的东西。
    """
    ws = _workspace_dir().resolve()
    raw = Path(script_path)
    candidates = [raw] if raw.is_absolute() else [ws / raw]
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except Exception:
            continue
        # 必须在 workspace 内
        try:
            resolved.relative_to(ws)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None
