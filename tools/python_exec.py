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
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema
from core.exception_policy import downgrade


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
#: 这些模块允许,但要提醒一句 —— 它们能访问系统资源
RESTRICTED_MODULES: Set[str] = {
    "os", "shutil", "tempfile", "glob",
}


class SecurityError(Exception):
    """代码被静态检查拦下。"""


class Severity(str, Enum):
    """检查结果的严重级别。

    **拦截判断只能看这个字段，不能看提示文案。** 历史实现是
    `p.startswith(("禁止", "语法错误"))` —— 谁把"禁止 import sys"改成
    "不建议 import sys"，就会把硬拦截静默降级成提示，而这类改动看起来
    完全无害。
    """

    BLOCK = "block"
    WARN = "warn"


@dataclass(frozen=True)
class Finding:
    """一条静态检查结果。"""

    severity: Severity
    code: str          # 稳定的机器可读标识，如 blocked_import
    message: str       # 给人/模型看的说明
    lineno: int = 0

    def __str__(self) -> str:
        return self.message


def blocking_findings(findings: List[Finding]) -> List[Finding]:
    """只要该拦截的那部分。"""
    return [f for f in findings if f.severity is Severity.BLOCK]


def format_findings(findings: List[Finding]) -> str:
    """拼成多行文本（给错误信息 / 日志用）。"""
    return "\n".join(f.message for f in findings)


def _module_root() -> Path:
    return Path(__file__).parent.parent


def _workspace_dir() -> Path:
    """agent 的可写工作区(产物落这里)。"""
    root = _module_root() / "memory" / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


#: 缩进自动修复最多迭代几次（防止死循环 / 越修越坏）
_MAX_INDENT_FIXES = 8


def _indent_of(line: str) -> int:
    """行首空白的字符数（tab 算 1，够用）。"""
    return len(line) - len(line.lstrip(" \t"))


def try_repair_indentation(code: str) -> tuple:
    """保守修复 LLM 生成代码里**孤立的缩进错误**。

    ## 为什么需要

    规划层把整段 Python 塞进 JSON 的 `code` 字符串字段。JSON 字符串里没有
    编辑器的缩进对齐，模型偶发会让某一行行首多出一个空格，例如：

        print("日期列:", date_col)   # 顶层，无缩进
        # 2. 定位合计行
         total = None                # <-- 行首多一个空格

    第 2 行是注释（不参与缩进判定），于是 Python 看到顶层代码后面突然出现
    缩进语句 → `IndentationError: unexpected indent`，整段代码一行都不执行。
    实机踩过（2026-11-20 报销表加行那次，第 34 行）。

    ## 修复判据（宁可少修，不可误伤）

    只处理一种情况：出错行 `cur` 的缩进 ** > ** 前一个"有效代码行" `prev` 的缩进，
    且 `prev` **没有开启代码块**（不以 `:` 结尾）。这说明 cur 是孤立冒出来的，
    不是合法的块内缩进。此时把 cur 的缩进拉回 prev 级别。

    合法的 for/if/def 块内缩进不会被误伤 —— 它们的上一行必然以 `:` 结尾。

    注释行与空行不参与判定（Python 缩进规则里它们不占层级）。

    Returns:
        `(修复后的 code, 修复说明列表)`。修不动就原样返回，说明列表为空。
    """
    lines = code.split("\n")
    fixes: List[str] = []

    for _ in range(_MAX_INDENT_FIXES):
        try:
            ast.parse("\n".join(lines))
            break  # 已经能解析
        except IndentationError as exc:
            # 只修 "unexpected indent"；其它缩进错误（如 unindent 不匹配）不碰
            if "unexpected indent" not in str(exc.msg):
                break
            lineno = exc.lineno or 0
            if not (1 <= lineno <= len(lines)):
                break
            cur = lines[lineno - 1]
            cur_indent = _indent_of(cur)
            stripped = cur.strip()

            # 找前一个有效代码行（跳过空行和纯注释）
            prev_idx = lineno - 2
            while prev_idx >= 0:
                prev_line = lines[prev_idx]
                if prev_line.strip() and not prev_line.lstrip().startswith("#"):
                    break
                prev_idx -= 1
            if prev_idx < 0:
                break
            prev = lines[prev_idx]
            prev_indent = _indent_of(prev)

            # 核心判据：prev 没开启块，cur 却比它缩进更深 → cur 是孤立错误
            if prev.rstrip().endswith(":"):
                break  # 前面开了块，cur 的缩进可能是合法的
            if cur_indent <= prev_indent:
                break
            if not stripped:
                break

            lines[lineno - 1] = " " * prev_indent + stripped
            fixes.append(f"第 {lineno} 行：行首多余空格已校正（{cur_indent} → {prev_indent}）")
        except SyntaxError:
            # 其它语法错误不归缩进修复管
            break

    return "\n".join(lines), fixes


def check_code(code: str) -> List[Finding]:
    """静态检查:返回**结构化问题列表**(空 = 通过)。

    用 `ast` 解析而不是正则 —— 正则能绕过的写法太多了
    (`getattr(os, "system")`、`__import__("os")`)。
    AST 至少能可靠地看到 import 了什么、调了哪些名字。

    拦截与否请看 `Finding.severity`，不要解析 `message`。
    """
    findings: List[Finding] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [
            Finding(
                severity=Severity.BLOCK,
                code="syntax_error",
                message=f"语法错误(第 {exc.lineno} 行): {exc.msg}",
                lineno=exc.lineno or 0,
            )
        ]

    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", 0)
        # --- import ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_MODULES:
                    findings.append(
                        Finding(
                            Severity.BLOCK,
                            "blocked_import",
                            f"禁止 import {root}(第 {lineno} 行)",
                            lineno,
                        )
                    )
                elif root in RESTRICTED_MODULES:
                    findings.append(
                        Finding(
                            Severity.WARN,
                            "restricted_import",
                            f"注意 import {root}(第 {lineno} 行):"
                            f"它能访问系统资源 —— "
                            f"只需要读写文件的话用 open() 就行",
                            lineno,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BLOCKED_MODULES:
                findings.append(
                    Finding(
                        Severity.BLOCK,
                        "blocked_import",
                        f"禁止 from {root} import ...(第 {lineno} 行)",
                        lineno,
                    )
                )
            elif root in RESTRICTED_MODULES:
                findings.append(
                    Finding(
                        Severity.WARN,
                        "restricted_import",
                        f"注意 from {root} import ...(第 {lineno} 行):"
                        f"它能访问系统资源 —— "
                        f"只需要读写文件的话用 open() 就行",
                        lineno,
                    )
                )

        # --- 危险调用 ---
        if isinstance(node, ast.Call):
            dotted = _dotted_name(node.func)

            # 裸调用 eval/exec/compile/__import__ → 拦
            if isinstance(node.func, ast.Name) and dotted in DANGEROUS_BARE_CALLS:
                findings.append(
                    Finding(
                        Severity.BLOCK,
                        "dangerous_call",
                        f"禁止调用 {dotted}()(第 {lineno} 行):"
                        f"它是动态执行入口,静态检查无法保证安全",
                        lineno,
                    )
                )
            # 带模块前缀的危险调用(os.system / builtins.compile …) → 拦
            elif dotted in DANGEROUS_QUALIFIED_CALLS:
                findings.append(
                    Finding(
                        Severity.BLOCK,
                        "dangerous_call",
                        f"禁止调用 {dotted}()(第 {lineno} 行):"
                        f"它能执行命令或动态代码,静态检查无法保证安全",
                        lineno,
                    )
                )

            # getattr/setattr 只在裸调用时提醒(减少误报)
            if isinstance(node.func, ast.Name) and dotted in ("getattr", "setattr"):
                findings.append(
                    Finding(
                        Severity.WARN,
                        "getattr_bypass",
                        f"不建议调用 {dotted}()(第 {lineno} 行):"
                        f"常用它绕过静态检查",
                        lineno,
                    )
                )

    return findings


def static_check(code: str) -> List[str]:
    """**已弃用** —— 只返回提示文案的兼容壳。

    新代码请用 `check_code()` 拿结构化结果；需要判断"是否拦截"时用
    `blocking_findings()`，不要对中文文案做 `startswith`。
    """
    return [f.message for f in check_code(code)]


def _call_name(func: ast.AST) -> str:
    """取调用的名字(只取最后一段,如 `os.system` → `system`)。

    **注意:这个函数只用于提示文案,不能用它做拦截判断。**
    历史上用它对一切调用做黑名单匹配,结果 `re.compile(...)`
    被当成"调用了 compile()"直接拦掉 —— 见 `_dotted_name`。
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


#: **裸调用**就危险的内建函数（不带任何模块前缀）
DANGEROUS_BARE_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "breakpoint",
}

#: 需要**带模块前缀**才算危险的调用：`re.compile` 安全，`builtins.compile` 不安全
DANGEROUS_QUALIFIED_CALLS = {
    "builtins.eval",
    "builtins.exec",
    "builtins.compile",
    "builtins.__import__",
    "importlib.__import__",
    "importlib.import_module",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execvp",
    "os.spawnl",
    "os.spawnv",
    "os.startfile",
    "ctypes.CDLL",
    "ctypes.cdll",
}


def _dotted_name(func: ast.AST) -> str:
    """把调用目标还原成点号全名，如 `re.compile` / `os.path.join` / `eval`。

    **为什么必须看全名**：只取最后一段会把
    `re.compile`(编译正则)、`pandas.DataFrame.eval`(表达式求值)
    这类完全合法的调用误判成动态执行入口。
    实机踩过：agent 写了一段用 `re.compile` 的校验脚本，被拦两次，
    它只能绕开正则、改用别的方式，白折腾。
    """
    parts = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    elif isinstance(node, ast.Call):
        # 形如 foo().bar  —— 取不到稳定全名，返回空表示"判断不了"
        return ""
    else:
        return ""
    return ".".join(reversed(parts))


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
        "适合:数据计算、格式转换、批量处理、生成产物文件、"
        "**调用外部 HTTP API**(requests / httpx / urllib 都可用)。"
        "不可以:启动子进程、动态执行(eval/exec/__import__)、装包、"
        "读进程环境变量里的密钥(密钥不在环境里,要用请显式传入)。"
        "生成的文件会作为产物返回,路径在 artifacts 里。"
        "**产物放到当前目录就行**(例如 `output/a.mp4` 或 `a.mp4`),"
        "不要再套一层 `workspace/` 前缀 —— 否则会变成 workspace/workspace/… 的套娃路径。"
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

        # --- 缩进自动修复（在静态检查之前）---
        #
        # 模型把代码塞进 JSON 字符串时偶发行首多空格 → IndentationError，
        # 整段不执行。能确定性修复的先修掉，修不了再交给下面的 check_code 阻断。
        indent_fixes: List[str] = []
        if code:
            code, indent_fixes = try_repair_indentation(code)
            if indent_fixes:
                self.logger.info("PythonExec", f"缩进自动修复: {indent_fixes}")

        # --- 静态检查 ---
        #
        # 拦截判断只看 severity。历史实现按中文文案前缀筛（"禁止"/"语法错误"），
        # 改一句提示语就可能把硬拦截降级成提示。
        findings = check_code(code) if code else []
        hard = blocking_findings(findings)
        if hard:
            return ToolResult(
                success=False,
                error=(
                    "代码被安全检查拦截:\n"
                    + format_findings(hard)
                    + "\n\n**这不是「什么都做不了」** —— 请改写成下面这些等价做法:\n"
                    "- 需要 HTTP 请求 → 用 `import requests` 或 `import httpx`(允许)\n"
                    "- 需要读写文件 → 用 `open()` / `pathlib`(允许)\n"
                    "- 需要算数据 → 直接写 Python(允许)\n"
                    "- 需要装包/起子进程 → 做不到,改用已有工具或请用户提供\n"
                    "- 需要读密钥 → 密钥不在子进程环境里,请显式作为参数传入\n"
                ),
                meta={
                    "blocked": True,
                    "problems": [f.message for f in findings],
                    "codes": [f.code for f in findings],
                },
            )
        if findings:
            # 只是"不建议",放行但提示
            self.logger.info("PythonExec", f"代码有建议项: {[f.message for f in findings]}")

        # --- 准备工作目录（**持久**，不是一次性的临时目录）---
        #
        # 实机踩过：agent 第一轮用相对路径建了
        # `ai_drama_project/04_分镜/分镜表.csv`，第二轮再读同一个相对路径
        # → FileNotFoundError，因为临时目录跑完就被 rmtree 了。
        # 多步文件任务因此根本做不下去，agent 只能被迫改成绝对路径硬编码，
        # 又依赖它自己记住写到哪了 —— 非常脆。
        #
        # 现在用 workspace 下的 `_run` 作为常驻执行目录：
        #   - 相对路径跨调用保持有效（符合"在一个工作区里做事"的直觉）
        #   - 与产物根目录分离，workspace 根目录仍然只放交付物
        workdir = _run_dir()
        before = _snapshot(workdir)
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
                script = workdir / _script_name()
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

            artifacts = self._collect_artifacts(workdir, before)

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
                meta={
                    "duration_ms": duration_ms,
                    "workdir": str(workdir),
                    "indent_fixed": indent_fixes,
                },
            )
        except Exception as exc:
            # 执行环境本身出错（建目录、写脚本、起进程……）—— 返回结构化失败，
            # 不要让异常冒泡成 500，那样 agent 只看到"工具崩了"，查不出原因。
            self.logger.warning("PythonExec", f"执行环境异常: {exc}")
            return ToolResult(success=False, error=f"执行环境异常: {exc}")
        # 注意：**不要删 workdir**。
        # 它是常驻执行目录，删了就等于让相对路径再次失效（就是上面那个 bug）。

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

    #: 执行器自己写的文件，永远不算产物
    _INFRA_FILES = {"main.py", "input.json"}

    @classmethod
    def _is_infra_file(cls, name: str) -> bool:
        """执行器自己写的脚本不算产物。

        脚本名带 execution 后缀（`run_xxxxxxxx.py`），所以不能只比对
        固定文件名 —— 要同时挡住这一类。
        """
        if name in cls._INFRA_FILES:
            return True
        return name.startswith("run_") and name.endswith(".py")

    def _collect_artifacts(self, workdir: Path,
                           before: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
        """把**本次执行新产生/改动过**的文件复制到 workspace，并返回清单。

        为什么要 `before` 快照：执行目录现在是常驻的（相对路径要跨调用有效），
        所以直接遍历会把**上一次甚至上上一次**产生的文件也当成本次产物。
        用 mtime 快照做差集，只报这次真正动过的。
        """
        out: List[Dict[str, Any]] = []
        ws = _workspace_dir()
        total = 0
        for path in sorted(workdir.rglob("*")):
            if not path.is_file():
                continue
            if self._is_infra_file(path.name):
                continue  # 我们自己写的脚本/输入文件,不算产物
            if before is not None:
                try:
                    if before.get(str(path)) == path.stat().st_mtime_ns:
                        continue      # 这次没动过 → 不是本次产物
                except OSError:
                    continue
            if len(out) >= MAX_ARTIFACT_FILES:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total + size > MAX_ARTIFACT_BYTES:
                break
            rel = path.relative_to(workdir)

            # **剥掉多套的一层 `workspace/`。**
            #
            # 执行目录现在常驻在 `memory/workspace/_run`。如果脚本按老习惯写
            # `workspace/output/x.mp4`（以为 cwd 是上级目录），就会落到
            # `_run/workspace/output/x.mp4`，产物路径变成 workspace/workspace/…
            # 实机踩过：成片落在 `_run/workspace/output/` 里，用户在界面上
            # 根本下载不到 —— 明明文件是好的。
            rel_parts = list(rel.parts)
            if len(rel_parts) > 1 and rel_parts[0] == "workspace":
                rel = Path(*rel_parts[1:])

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
                # **相对工作区的路径** —— 下载接口按这个取文件。
                # 早先这里给的是绝对路径(`D:\...\workspace\a.txt`),
                # 拼成下载 URL 会得到 `/api/artifacts/D:/...`,
                # 前端点开就是 404。
                "path": str(rel).replace("\\", "/"),
                "abs_path": str(dest),
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
        except Exception as exc:
            downgrade(exc, where='tools/python_exec.py')


def _run_dir() -> Path:
    """常驻执行目录（相对路径跨调用的前提）。

    放在 workspace 下的 `_run/<作用域>`，和交付物根目录分开 ——
    根目录只放用户能下载的东西。

    **作用域 = 用户 + 会话**（见 `core/exec_context.py`）：

    - 同一会话内 → 目录不变 → 相对路径跨调用仍然有效（这是这个目录存在的理由）；
    - 不同会话 / 不同用户 → 目录不同 → **并发执行不会互相覆盖 `main.py`**。

    实机踩过（docs/17 P0-2）：原来是全局单例 `_run`，两个用户并发跑代码
    时，A 的脚本被 B 的覆盖，A 实际执行了 B 的代码 —— 是数据正确性事故，
    不是"输出串了"。
    """
    from core.exec_context import current_scope_key

    d = _workspace_dir() / "_run" / current_scope_key()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        downgrade(exc, where='tools/python_exec.py')
    return d


def _script_name() -> str:
    """本次执行的脚本文件名。

    **带 execution 后缀** —— 同一会话内的两次并发执行也不会抢同一个文件。
    （workdir 是会话级的，相对路径跨调用有效；脚本文件是一次性的。）
    """
    from core.exec_context import get_exec_context

    ctx = get_exec_context()
    if not ctx.execution_id:
        return "main.py"
    return f"run_{ctx.execution_id[-8:]}.py"



def _snapshot(root: Path, *, max_files: int = 20000) -> Dict[str, int]:
    """记录目录下所有文件的 mtime，用来算"这次改了什么"。"""
    snap: Dict[str, int] = {}
    try:
        for i, path in enumerate(root.rglob("*")):
            if i >= max_files:
                break
            try:
                if path.is_file():
                    snap[str(path)] = path.stat().st_mtime_ns
            except OSError:
                continue
    except Exception as exc:
        downgrade(exc, where='tools/python_exec.py')
    return snap


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
