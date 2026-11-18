"""把已有文件登记成可下载产物。

## 为什么需要

`/api/artifacts` 只服务 `memory/workspace` 这一个目录，而 agent 干活的地方
往往是 MCP 授权目录（比如 `D:/llm_chat_workspace/ai_drama_project`）。

结果就是实机踩到的：agent 在项目目录里生成了 2 MB 的 mp4，
用户在界面上**根本下载不到** —— 产物区里没有它，链接也拼不出来。

`python_exec` 的产物收集只覆盖它自己的执行目录，管不到这种情况。
所以需要一个显式动作：**把某个已有文件登记成产物**。

## 安全边界（这一段比功能本身重要）

这个工具会把文件复制到「认证用户可下载」的地方，所以它不能是万能读盘：

1. **来源必须在授权根目录内**（产物目录 + MCP 授权目录）
2. **应用自身安装目录一律拒绝** —— 那里有 `.env`（真实 API key）、
   `.git`、源码，绝不能通过下载链接泄露
3. **敏感文件名一律拒绝**：`.env` / `*.env` / `*.pem` / `*.key` /
   `id_rsa*` / `*secret*` 等
4. 目标名不允许路径穿越

> 判据：**能被 agent 读 ≠ 能被用户下载。** 前者是干活需要，
> 后者是数据外流通道，必须更严。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


#: 拒绝登记的文件名模式（大小写不敏感）
DENY_PATTERNS = (
    re.compile(r"^\.env", re.I),
    re.compile(r"\.env$", re.I),
    re.compile(r"\.env\.", re.I),
    re.compile(r"\.pem$", re.I),
    re.compile(r"\.key$", re.I),
    re.compile(r"\.p12$", re.I),
    re.compile(r"\.crt$", re.I),
    re.compile(r"^id_rsa", re.I),
    re.compile(r"^id_ed25519", re.I),
    re.compile(r"secret", re.I),
    re.compile(r"credential", re.I),
    re.compile(r"^\.git", re.I),
)


def _is_sensitive(name: str) -> bool:
    return any(p.search(name) for p in DENY_PATTERNS)


def _authorized_roots() -> List[Path]:
    """可登记的来源根目录：产物目录 + MCP 授权目录，**排除应用自身目录**。"""
    roots: List[Path] = []
    app_root = Path(__file__).resolve().parent.parent

    try:
        from tools.script_writer import workspace_dir

        ws = Path(workspace_dir()).resolve()
        if ws.is_dir():
            roots.append(ws)
    except Exception:
        pass

    try:
        from core.workspace_view import _mcp_dirs

        for d in _mcp_dirs():
            try:
                resolved = d.resolve()
            except Exception:
                continue
            if resolved == app_root:
                continue          # 应用自身目录绝不开放
            if all(resolved != r for r in roots):
                roots.append(resolved)
    except Exception:
        pass

    return roots


class PublishArtifactTool(Tool):
    """把一个已有文件登记成可下载的产物。"""

    name = "publish_artifact"
    description = (
        "把一个**已经存在**的文件登记成可下载产物，登记后用户能在界面上下载/预览它。\n"
        "典型场景：你在 D:/llm_chat_workspace 之类的目录里生成了 mp4/报表/文档，"
        "但产物区里看不到 —— 用它登记一下就有下载链接了。\n"
        "注意：只能登记授权目录里的文件；密钥类文件（.env/.pem/secret 等）"
        "和应用自身源码目录会被拒绝。"
    )

    def __init__(self):
        self.logger = get_logger()
        self._max_bytes = 512 * 1024 * 1024     # 512 MB 上限

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="path",
                    type="string",
                    required=True,
                    description=(
                        "要登记的文件路径（绝对路径，或相对授权目录的路径）。"
                        "支持一次给单个路径。"
                    ),
                ),
                ToolParam(
                    name="name",
                    type="string",
                    required=False,
                    description="登记后的文件名/相对路径（默认用原文件名，放在 output/ 下）",
                ),
            ],
            returns={"url": "下载/预览地址", "path": "产物区相对路径", "size": "字节数"},
        )

    async def execute(self, path: str = "", name: str = "") -> ToolResult:
        import os

        raw = (path or "").strip().strip('"').strip("'")
        if not raw:
            return ToolResult(success=False, error="path 不能为空")

        roots = _authorized_roots()
        if not roots:
            return ToolResult(success=False, error="没有可用的授权目录")

        # ---- 定位源文件 ----
        src: Optional[Path] = None
        candidate = Path(raw)
        if candidate.is_file():
            src = candidate.resolve()
        else:
            for root in roots:
                probe = (root / raw).resolve()
                if probe.is_file():
                    src = probe
                    break

        if src is None:
            return ToolResult(
                success=False,
                error=(
                    f"文件不存在：{raw}\n"
                    f"如果它在别的目录，请给绝对路径。可登记的根目录：\n"
                    + "\n".join(f"  - {r}" for r in roots)
                ),
            )

        # ---- 安全检查 ----
        if not any(_is_under(src, r) for r in roots):
            return ToolResult(
                success=False,
                error=(
                    f"拒绝登记：{src} 不在授权目录内。\n"
                    "（能被读 ≠ 能被下载。这个工具只允许登记授权目录里的文件。）"
                ),
            )
        if _is_sensitive(src.name):
            return ToolResult(
                success=False,
                error=(
                    f"拒绝登记：{src.name} 看起来是密钥/凭据文件。\n"
                    "这类文件不能变成可下载链接 —— 用 open() 读它，但别对外发布。"
                ),
            )
        try:
            size = src.stat().st_size
        except OSError as exc:
            return ToolResult(success=False, error=f"读取文件信息失败：{exc}")
        if size > self._max_bytes:
            return ToolResult(
                success=False,
                error=f"文件太大（{size / 1024 / 1024:.1f} MB），上限 512 MB",
            )

        # ---- 复制进产物区 ----
        try:
            from tools.script_writer import workspace_dir

            dest_root = Path(workspace_dir())
        except Exception as exc:
            return ToolResult(success=False, error=f"取产物目录失败：{exc}")

        rel = self._dest_rel(name or src.name, dest_root)
        if rel is None:
            return ToolResult(
                success=False,
                error="name 不合法（不允许 `..`、绝对路径或盘符）",
            )

        dest = dest_root / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
        except Exception as exc:
            self.logger.warning("PublishArtifact", f"复制失败 {src} -> {dest}: {exc}")
            return ToolResult(success=False, error=f"复制失败：{exc}")

        rel_posix = str(rel).replace("\\", "/")
        self.logger.info("PublishArtifact", f"已登记产物 {rel_posix} ({size} B)")
        return ToolResult(
            success=True,
            data={
                "name": dest.name,
                "path": rel_posix,
                "abs_path": str(dest),
                "size": size,
                "url": f"/api/artifacts/{rel_posix}",
                # 和 python_exec 保持同一个结构，前端「生成的文件」区才能列出来
                "artifacts": [{
                    "name": dest.name,
                    "path": rel_posix,
                    "size": size,
                }],
            },
        )

    @staticmethod
    def _dest_rel(name: str, dest_root: Path) -> Optional[Path]:
        """算出产物区里的相对路径，默认放 output/ 下。"""
        raw = (name or "").strip().replace("\\", "/")
        if not raw:
            return None
        if ".." in raw or raw.startswith("/") or ":" in raw:
            return None
        # 去掉可能带的目录前缀，只留文件名
        base = raw.split("/")[-1].strip()
        if not base or base in (".", ".."):
            return None
        return Path("output") / base


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False
