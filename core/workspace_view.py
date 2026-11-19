"""工作区现状 —— 让 agent 知道自己**手上已经有什么**。

## 为什么需要这个

实机踩过：agent 在前几轮里把用户的 MiniMax key 保存到了
`ai_drama_project/config/minimax.env`，下一轮它却说

> 密钥不在我的执行环境里……必须你在对话里显式给我一次。

它**不是撒谎**：子进程环境确实拿不到 key（安全策略），
但它**自己刚把 key 写进了文件**，只要 `open()` 就能读到。
它没去查，因为它不知道自己有这个文件。

同理还有"任务清单还是空模板"这类判断 —— 它是**临时去扫目录**才知道的，
每次都靠运气和它有没有想起来。

## 根因

agent 的"我有什么"完全依赖**对话历史**。而上下文只恢复最近若干条
（`SESSION_RESTORE_LIMIT`），更早的会被滚动摘要压缩 ——
摘要记不住"我把 key 存在哪个路径"这种细节。

**所以：事实性状态不该只存在对话里，要能从磁盘看到。**

## 做什么

把工作区里的文件列成一段紧凑清单，注入**规划层**提示词。
规划层是"决定下一步做什么"的地方，最需要知道现存资产。

## 不做

- 不注入文件**内容**（只给路径和大小）—— 内容让 agent 自己去读
- 不做全量递归（有上限）—— 视频项目动辄几千个文件，会挤爆提示词
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from infra.logger import get_logger


#: 最多列多少个文件
MAX_ENTRIES = 40
#: 整块最大字符数
MAX_CHARS = 1400

#: 这些目录是执行器的草稿区 / 噪音，不是用户的资产
_SKIP_DIRS = {
    "_run", "__pycache__", ".pytest_cache", ".git", "node_modules",
    "dist", "build", ".venv", "venv", ".idea", ".vscode", "memory",
}

#: 单个根目录最多取多少个文件
PER_ROOT_MAX = 25

#: 噪音后缀（日志/临时文件不是用户的资产）
_SKIP_SUFFIXES = (".log", ".err", ".pyc", ".tmp", ".lock", ".swp")

#: 应用自身的安装目录 —— 它不是用户的数据，别混进清单
_APP_ROOT = Path(__file__).resolve().parent.parent


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _mcp_dirs() -> List[Path]:
    """从 MCP filesystem 配置里抠出被授权的目录。

    **为什么需要**：用户的项目在 `D:/llm_chat_workspace`，
    而产物目录是 `memory/workspace` —— 只看产物目录的话，
    agent 依然"看不见"自己刚建的项目文件。

    配置形如：
        npx -y @modelcontextprotocol/server-filesystem D:\\dirA D:\\dirB
    这里把后面那些**真实存在的目录**取出来。
    """
    out: List[Path] = []
    try:
        from infra.config import config
        from tools.sources.mcp_source import parse_mcp_servers

        servers = parse_mcp_servers(getattr(config, "mcp_servers", "") or "")
    except Exception:
        return out

    if isinstance(servers, dict):
        servers = list(servers.values())

    for server in servers or []:
        try:
            cmd = ""
            if isinstance(server, dict):
                cmd = str(server.get("server_path") or server.get("command") or "")
            else:
                # `parse_mcp_servers()` 返回的是 ToolSource 对象，
                # 真正的配置在 `.config` 里（实机踩过：直接取 server_path 取不到）
                cfg = getattr(server, "config", None)
                if isinstance(cfg, dict):
                    cmd = str(cfg.get("server_path") or cfg.get("command") or "")
                if not cmd:
                    cmd = str(getattr(server, "server_path", "") or "")
            if "filesystem" not in cmd:
                continue
            for token in cmd.split():
                candidate = Path(token.strip('"').strip("'"))
                if candidate.is_dir():
                    out.append(candidate)
        except Exception:
            continue
    return out


def _roots() -> List[Path]:
    """要扫描的根目录：产物目录 + MCP 授权目录（去重、只留存在的）。"""
    roots: List[Path] = []
    try:
        from tools.script_writer import workspace_dir

        artifact_root = Path(workspace_dir())
        if artifact_root.is_dir():
            roots.append(artifact_root)
    except Exception:
        pass

    for d in _mcp_dirs():
        try:
            resolved = d.resolve()
        except Exception:
            continue
        # 应用自己的安装目录不是"用户的文件"，混进来只会制造噪音
        if resolved == _APP_ROOT:
            continue
        if all(resolved != r.resolve() for r in roots):
            roots.append(resolved)
    return roots


def build_workspace_block(
    *,
    max_entries: int = MAX_ENTRIES,
    max_chars: int = MAX_CHARS,
) -> str:
    """生成工作区清单；空工作区返回空串（不注入噪音）。"""
    roots = _roots()
    if not roots:
        return ""

    #: (mtime, size, 展示用相对标签)
    files: list = []
    multi_root = len(roots) > 1

    for root in roots:
        label = root.name
        picked = 0
        try:
            for path in root.rglob("*"):
                if picked >= PER_ROOT_MAX:
                    break
                if not path.is_file():
                    continue
                try:
                    rel_parts = path.relative_to(root).parts
                except ValueError:
                    continue
                if any(part in _SKIP_DIRS for part in rel_parts):
                    continue
                if path.name.lower().endswith(_SKIP_SUFFIXES):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                rel = "/".join(rel_parts)
                shown = f"{label}/{rel}" if multi_root else rel
                files.append((st.st_mtime, st.st_size, shown))
                picked += 1
        except Exception:
            continue

    if not files:
        return ""

    total = len(files)
    files.sort(key=lambda x: -x[0])

    lines = [f"- {shown} ({_human_size(size)})" for _m, size, shown in files[:max_entries]]

    header = f"## 你能访问的文件（最近修改的前 {min(total, max_entries)} 个"
    if total > max_entries:
        header += f"，共扫到 {total} 个"
    header += "）"

    tail = (
        "\n\n**这些文件已经存在，不要再假设它们不存在。**"
        "需要内容就用 `read_file` 读（10MB 以内的文本文件）；"
        "PDF/Word/Excel 用 `read_document`。"
        "找文件先看上面这份清单，不要凭印象猜路径。"
        "清单里没有的，才需要新建。"
    )

    block = header + "\n" + "\n".join(lines) + tail
    if len(block) > max_chars:
        block = block[: max_chars - 12].rstrip() + "\n…（已截断）"
    return block
