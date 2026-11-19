"""写脚本工具 —— 让 agent 把脚本**保存成文件**给用户。

## 为什么单独一个工具

`python_exec` 跑完就把临时目录删了。用户说"把这段脚本保存下来给我",
或者下一轮想"改一下刚才那个脚本再跑",都需要脚本**持久存在**。

所以拆成两个工具:

| 工具 | 做什么 | 产物 |
|---|---|---|
| `write_script` | 保存脚本到 workspace | 持久文件,返回路径 |
| `python_exec` | 跑代码 | 临时目录,产物回传到 workspace |

`python_exec` 也接受 `files` 参数(运行前预置文件),所以
"用 write_script 写好 → 用 python_exec 读它跑" 这条链路是通的。

## 安全

和 `python_exec` 同一套模型:
- 文件名不允许路径分隔符 / `..`(防穿越)
- 只能写进 workspace 目录
- 路径带 `run_id` 子目录,避免不同会话互相覆盖
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


MAX_SCRIPT_CHARS = 200_000
#: 允许的脚本扩展名
ALLOWED_SUFFIXES = (".py", ".sh", ".bat", ".ps1", ".js", ".ts", ".sql", ".txt", ".md", ".json", ".yaml", ".yml")


def workspace_dir() -> Path:
    """agent 的可写工作区。"""
    root = Path(__file__).parent.parent / "memory" / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_name(name: str) -> Optional[str]:
    """校验并归一化文件名。非法返回 None。

    允许:字母数字、点、下划线、连字符、空格、中文
    拒绝:路径分隔符、`..`、绝对路径、控制字符
    """
    if not name:
        return None
    name = name.strip()
    if not name or len(name) > 200:
        return None
    if "/" in name or "\\" in name:
        return None
    if ".." in name:
        return None
    if any(ord(c) < 32 for c in name):
        return None
    if Path(name).is_absolute():
        return None
    # Windows 保留名
    if re.match(r"^(CON|PRN|AUX|NUL|COM\d|LPT\d)(\.|$)", name, re.I):
        return None
    return name


class WriteScriptTool(Tool):
    """保存一段脚本到 workspace,返回路径。"""

    name = "write_script"
    #: 安全开关:只有 CODE_EXEC_ENABLED=true 才能用
    required_flag = "code_exec_enabled"
    description = (
        "把一段脚本保存成文件(存到 agent 工作区),返回文件路径。"
        "适合:用户要'把脚本保存下来'、或下一轮要基于上次脚本修改。"
        "配合 python_exec 可以先写文件再在代码里读它。"
    )

    def __init__(self, run_id: str = ""):
        self.logger = get_logger()
        #: 显式指定的目录名（测试与特殊场景用）。为空则按会话作用域自动算。
        self.run_id = run_id or ""

    def _scope_dir(self) -> str:
        """脚本落盘的子目录名。

        **按会话隔离**（见 `core/exec_context.py`）：
        同一会话内目录稳定 → 下一轮还能引用上一轮存的脚本；
        不同会话/用户目录不同 → 并发时不会互相覆盖同名脚本。

        实机踩过（docs/17 P0-2）：原来是**进程级随机**目录（工具实例化时
        随机一次，全进程共享），多用户共用一个目录。
        """
        if self.run_id:
            return self.run_id
        from core.exec_context import current_scope_key

        return current_scope_key()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="name",
                    type="string",
                    description="文件名(如 analyze.py)。不能带路径分隔符。",
                    required=True,
                ),
                ToolParam(
                    name="content",
                    type="string",
                    description="文件内容",
                    required=True,
                ),
                ToolParam(
                    name="description",
                    type="string",
                    description="这个脚本是干什么的(可选,会记进清单)",
                    required=False,
                    default=None,
                ),
            ],
            returns={
                "path": "绝对路径",
                "name": "文件名",
                "size": "字节数",
                "run_id": "本次运行的工作区子目录",
            },
        )

    async def execute(self, **kwargs) -> ToolResult:
        name = safe_name(str(kwargs.get("name") or ""))
        if not name:
            return ToolResult(
                success=False,
                error="非法文件名(不允许路径分隔符、.. 或控制字符)",
            )
        content = kwargs.get("content")
        if content is None:
            return ToolResult(success=False, error="content 不能为空")
        if not isinstance(content, str):
            content = str(content)
        if len(content) > MAX_SCRIPT_CHARS:
            return ToolResult(
                success=False,
                error=f"内容过长({len(content)} 字符,上限 {MAX_SCRIPT_CHARS})",
            )

        suffix = Path(name).suffix.lower()
        if suffix and suffix not in ALLOWED_SUFFIXES:
            return ToolResult(
                success=False,
                error=f"不支持的文件类型 {suffix}(允许: {', '.join(ALLOWED_SUFFIXES)})",
            )

        scope_dir = self._scope_dir()
        target_dir = workspace_dir() / scope_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name

        try:
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            return ToolResult(success=False, error=f"写入失败: {exc}")

        description = str(kwargs.get("description") or "").strip()
        if description:
            # 顺手写个同名 .md 说明,免得用户拿到脚本不知道干嘛
            try:
                (target_dir / (Path(name).stem + ".md")).write_text(
                    f"# {name}\n\n{description}\n", encoding="utf-8"
                )
            except Exception:
                pass

        self.logger.info("WriteScript", f"已保存 {target} ({len(content)} 字符)")
        rel = str(target.relative_to(workspace_dir())).replace("\\", "/")
        return ToolResult(
            success=True,
            data={
                "path": str(target),
                "name": name,
                "size": len(content.encode("utf-8")),
                "run_id": scope_dir,
                "description": description,
                # 脚本也是交付物 —— 用统一的 artifacts 结构,
                # 前端「生成的文件」区才能把它列出来下载
                "artifacts": [{
                    "name": name,
                    "path": rel,
                    "size": len(content.encode("utf-8")),
                }],
            },
            meta={"run_id": scope_dir},
        )


class ListScriptsTool(Tool):
    """列出 workspace 里已保存的脚本。"""

    name = "list_scripts"
    required_flag = "code_exec_enabled"
    description = "列出 agent 工作区里已保存的脚本文件(路径、大小、说明)。"

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[],
            returns={"scripts": "[{name, path, size, description}]"},
        )

    async def execute(self, **kwargs) -> ToolResult:
        ws = workspace_dir()
        # **只列当前作用域的脚本** —— 否则 A 能看到 B 存的脚本。
        from core.exec_context import get_exec_context

        ctx = get_exec_context()
        if ctx.is_scoped:
            ws = ws / ctx.scope_key
        items: List[Dict[str, Any]] = []
        if not ws.exists():
            return ToolResult(success=True, data={"scripts": [], "count": 0})
        for path in sorted(ws.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            desc = ""
            md = path.with_suffix(".md")
            if md.exists() and md != path:
                try:
                    desc = "\n".join(md.read_text(encoding="utf-8").splitlines()[2:]).strip()
                except Exception:
                    desc = ""
            items.append({
                "name": path.name,
                "path": str(path),
                "size": size,
                "description": desc,
            })
        return ToolResult(success=True, data={"scripts": items, "count": len(items)})
