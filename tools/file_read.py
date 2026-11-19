"""文件读取工具 —— 让 agent 能读**任意文本文件**(源码/配置/日志/数据)。

## 为什么要有这个工具

实机踩过的坑:用户说"看一下这个配置文件里写了什么",agent 手里只有两条路:

- `read_document` —— 只认 pdf/docx/xlsx/pptx/md/txt/csv/html/json/xml/log,
  输出还是"带锚点的 markdown 块",读代码片段很别扭
- `python_exec` 里写 `open()` —— 能读,但要多起一次子进程,
  而且代码执行是**安全开关**(`CODE_EXEC_ENABLED=false` 就直接没了)

两个都不对劲:一个是"读文件"被绑成了"读文档",一个是"读文件"要绕一圈"执行代码"。

所以补一个**专门读文件**的工具:只读、无副作用、轻量。

## 限制

- **单个文件最大 10MB**(`MAX_FILE_BYTES`)——
  超了**直接报错**,不做"只读前 10MB"。
  截断读取最坏的情况是 agent 以为看到了全文,然后据此下结论 —— 宁可明确失败。
- 只读文本:图片/可执行文件/压缩包明确报错,并指向该用的工具。
- **凭据类文件**(`.env` / `*.pem` / `*.key` …)里的密钥值会先打码再返回 ——
  工具结果会进对话、进会话库、进日志,明文密钥不该从这条路流出去。

## 相对路径怎么解析

agent 眼里的"工作区文件"和进程的 cwd 常常不是一个地方:

- `python_exec` 的执行目录是 `memory/workspace/_run`
- 产物目录是 `memory/workspace`
- 进程 cwd 是应用根目录

所以相对路径依次尝试:**cwd → workspace/_run → workspace**,
并在报错时把试过的位置全列出来 —— agent 才知道该改哪个路径。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from infra.logger import get_logger, redact_secrets
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


# ===== 限制 =====

#: 单个文件最大字节数(10MB)
MAX_FILE_BYTES = 10 * 1024 * 1024
#: 单次返回的字符数上限(默认)
DEFAULT_MAX_CHARS = 60_000
#: 硬上限 —— agent 不能把 max_chars 设成 10 亿
HARD_MAX_CHARS = 200_000
#: 探测二进制时只看文件头这么多字节
_PROBE_BYTES = 8192


#: 明确不是文本的扩展名 —— 读到这些直接给一句有用的提示
BINARY_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".bin", ".obj", ".o", ".a", ".class",
    ".pyc", ".pyo", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3", ".mdb", ".dat",
}

#: 文档类扩展名 —— 不是"不能读",是"应该用 read_document"
DOCUMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".epub"}

#: 凭据类文件 —— 里面的密钥值会打码
_SECRET_NAMES = {
    ".env", ".env.local", ".env.development", ".env.production",
    ".env.staging", ".env.test",
    "credentials", "credentials.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".netrc", ".npmrc", ".pypirc", ".htpasswd",
}
#: 模板文件不算真凭据(里面通常是占位值),别误伤
_SECRET_NAME_EXCLUDES = {".env.example", ".env.sample", ".env.template"}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}


# ============================================================
# 小工具
# ============================================================

def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _to_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """把 LLM 给的参数字段安全地转成 int(转不了就用默认值)。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("false", "0", "no", "off", "none"):
        return False
    if text in ("true", "1", "yes", "on"):
        return True
    return default


def decode_bytes(raw: bytes) -> Tuple[str, str]:
    """解码字节流,返回 (文本, 编码名)。

    顺序:BOM → utf-8 → gb18030 → utf-8(replace)。

    **为什么 gb18030 在 utf-8 之后**:Windows 上的中文文件(GBK/GB2312)很常见,
    而 gb18030 是 GBK 的超集,能兜住绝大多数中文编码。
    但它对任意字节几乎都能解码,所以必须排在 utf-8 后面 ——
    否则 UTF-8 文件会被当成 GBK 解出乱码。
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gb18030"), "gb18030"
    except UnicodeDecodeError:
        pass
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def looks_binary(path: Path, raw: bytes) -> bool:
    """粗判是不是二进制。

    两道:
    1. 扩展名黑名单(短小二进制嗅探不出来)
    2. 内容嗅探:文件头里有 NUL 字节
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    return b"\x00" in raw[:_PROBE_BYTES]


def is_secret_file(path: Path) -> bool:
    """这个文件是不是"存着凭据"的文件。"""
    name = path.name.lower()
    if name in _SECRET_NAME_EXCLUDES:
        return False
    if name in _SECRET_NAMES:
        return True
    return path.suffix.lower() in _SECRET_SUFFIXES


# ============================================================
# 路径解析
# ============================================================

def _candidate_roots() -> List[Path]:
    """相对路径的搜索顺序:cwd → workspace/_run → workspace。

    顺序有讲究:agent 通过 `python_exec` 写出来的文件落在 `_run`,
    而工作区清单里列的是 workspace 根下的文件 —— 两个都得能命中。
    """
    roots: List[Path] = []
    try:
        roots.append(Path.cwd())
    except OSError:
        pass
    try:
        from tools.script_writer import workspace_dir

        ws = Path(workspace_dir())
        roots.append(ws / "_run")
        roots.append(ws)
    except Exception:
        pass
    return roots


def _workspace_root() -> Optional[Path]:
    try:
        from tools.script_writer import workspace_dir

        return Path(workspace_dir())
    except Exception:
        return None


def resolve_path(raw: str) -> Tuple[Optional[Path], List[str]]:
    """把用户给的路径解析成真实文件。

    返回 (命中路径 或 None, 试过的位置列表)。
    报错时把"试过的位置"原样返回 —— agent 才知道该换哪个路径。
    """
    p = Path(raw).expanduser()
    tried: List[str] = []

    if p.is_absolute():
        tried.append(str(p))
        return (p if p.exists() else None), tried

    for root in _candidate_roots():
        cand = root / p
        tried.append(str(cand))
        if cand.exists():
            return cand, tried

    # "workspace/xxx" 这种写法 —— 很多人会这么写
    parts = p.parts
    if parts and parts[0].lower() == "workspace":
        ws = _workspace_root()
        if ws is not None:
            cand = ws.joinpath(*parts[1:])
            tried.append(str(cand))
            if cand.exists():
                return cand, tried

    return None, tried


def describe_directory(path: Path, limit: int = 30) -> str:
    """目录也能给点有用的信息 —— 顺便充当"列目录"能力。"""
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        return f"{path} 是目录,但列不出来:{exc}"

    n_files = sum(1 for e in entries if e.is_file())
    n_dirs = sum(1 for e in entries if e.is_dir())
    lines = [
        f"{path} 是目录(不是文件)。里面大概有 {n_files} 个文件、{n_dirs} 个子目录:",
    ]
    for e in entries[:limit]:
        if e.is_dir():
            lines.append(f"- {e.name}/")
        else:
            try:
                size = e.stat().st_size
            except OSError:
                size = 0
            lines.append(f"- {e.name} ({_human_size(size)})")
    if len(entries) > limit:
        lines.append(f"- …(还有 {len(entries) - limit} 项)")
    lines.append("要读其中某个文件,把它的路径传给 read_file 即可。")
    return "\n".join(lines)


# ============================================================
# 工具
# ============================================================

class ReadFileTool(Tool):
    """读取本机上的文本文件。"""

    name = "read_file"
    #: **第一行会被规划层单独截出来当"工具说明"**(见 `agents/manager.py::_tool_signatures`),
    #: 所以第一行要能独立说清"这个工具干什么、怎么用",不能写成半句。
    description = (
        "读本机文本文件(源码/配置/日志),返回带行号内容;"
        "上限 10MB,大文件用 start_line/end_line 分段读。\n"
        "PDF/Word/Excel 请改用 read_document;图片/压缩包等二进制文件读不了。\n"
        "相对路径依次尝试:当前目录 → 工作区执行目录 → 工作区根目录。"
    )

    def __init__(self, max_bytes: int = MAX_FILE_BYTES):
        self.logger = get_logger()
        self.max_bytes = max(1024, int(max_bytes))

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="path",
                    type="string",
                    description=(
                        "文件路径(绝对路径,或相对当前目录/工作区的路径)。"
                        "相对路径会依次尝试:当前目录 → 工作区执行目录 → 工作区根目录。"
                    ),
                    required=True,
                ),
                ToolParam(
                    name="start_line",
                    type="integer",
                    description="起始行(1 起,含)。不给就从第 1 行开始。",
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="end_line",
                    type="integer",
                    description="结束行(含)。不给就读到文件末尾。",
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="max_chars",
                    type="integer",
                    description=(
                        f"本次返回的字符上限(默认 {DEFAULT_MAX_CHARS},"
                        f"硬上限 {HARD_MAX_CHARS})。超出会截断并告诉你从哪行续读。"
                    ),
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="line_numbers",
                    type="boolean",
                    description="是否给每行加行号(默认 true,方便引用具体行)。",
                    required=False,
                    default=True,
                ),
            ],
            returns={
                "path": "文件真实路径",
                "size": "字节数",
                "encoding": "识别出的编码",
                "total_lines": "总行数",
                "content": "文件内容(带行号)",
                "truncated": "是否被截断",
                "next_start_line": "截断后从哪一行继续读",
            },
            examples=[
                {"path": "core/agent.py"},
                {"path": "D:/project/config/app.yaml", "start_line": 1, "end_line": 80},
            ],
        )

    async def execute(self, **kwargs) -> ToolResult:
        raw_path = str(kwargs.get("path") or "").strip()
        if not raw_path:
            return ToolResult(success=False, error="path 不能为空")

        path, tried = resolve_path(raw_path)
        if path is None:
            return ToolResult(
                success=False,
                error=(
                    f"文件不存在:{raw_path}\n试过的位置:\n"
                    + "\n".join(f"- {t}" for t in tried)
                ),
            )
        if path.is_dir():
            return ToolResult(success=False, error=describe_directory(path))
        if not path.is_file():
            return ToolResult(success=False, error=f"不是普通文件:{path}")

        try:
            stat = path.stat()
        except OSError as exc:
            return ToolResult(success=False, error=f"读取文件属性失败:{exc}")

        size = stat.st_size
        if size > self.max_bytes:
            return ToolResult(
                success=False,
                error=(
                    f"文件太大({_human_size(size)},上限 "
                    f"{_human_size(self.max_bytes)}):{path}\n"
                    "本工具只读 10MB 以内的文件 —— "
                    "请让用户拆分文件,或改用能定位片段的方式(如 grep/日志工具)。"
                ),
                meta={"size": size, "max_bytes": self.max_bytes},
            )

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ToolResult(success=False, error=f"读取失败:{exc}")

        suffix = path.suffix.lower()
        if suffix in DOCUMENT_SUFFIXES:
            return ToolResult(
                success=False,
                error=(
                    f"{path.name} 是文档格式({suffix}),本工具只读文本。"
                    "请改用 read_document —— 它能提取正文并指出原文位置。"
                ),
            )
        if looks_binary(path, raw):
            return ToolResult(
                success=False,
                error=(
                    f"{path.name} 看起来是二进制文件,本工具只读文本文件。"
                    "图片/压缩包/可执行文件请用对应的工具处理。"
                ),
            )

        text, encoding = decode_bytes(raw)

        redacted = False
        if is_secret_file(path):
            text = redact_secrets(text)
            redacted = True

        lines = text.splitlines()
        total = len(lines)

        start_line = _to_int(kwargs.get("start_line"), 1) or 1
        end_line = _to_int(kwargs.get("end_line"), None)
        if start_line < 1:
            start_line = 1
        if total == 0:
            # 空文件:没有行可读,但**不是错误**
            start_line, end_line = 1, 0
        else:
            if start_line > total:
                return ToolResult(
                    success=False,
                    error=(
                        f"起始行 {start_line} 超出文件范围:{path} 一共 {total} 行"
                    ),
                )
            if end_line is None or end_line > total:
                end_line = total
            if end_line < start_line:
                return ToolResult(
                    success=False,
                    error=f"行范围非法:start_line={start_line} > end_line={end_line}",
                )

        max_chars = _to_int(kwargs.get("max_chars"), DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS
        max_chars = max(500, min(max_chars, HARD_MAX_CHARS))
        line_numbers = _to_bool(kwargs.get("line_numbers"), True)

        selected = lines[start_line - 1:end_line] if total else []
        rendered: List[str] = []
        used = 0
        truncated = False
        for offset, line in enumerate(selected):
            piece = f"{start_line + offset:>6} | {line}" if line_numbers else line
            if used + len(piece) + 1 > max_chars:
                truncated = True
                break
            rendered.append(piece)
            used += len(piece) + 1

        content = "\n".join(rendered)
        next_start = start_line + len(rendered)

        data: Dict[str, Any] = {
            "path": str(path),
            "name": path.name,
            "size": size,
            "size_human": _human_size(size),
            "encoding": encoding,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "total_lines": total,
            "start_line": start_line,
            "end_line": end_line,
            "returned_lines": len(rendered),
            "content": content,
            "truncated": truncated,
            "redacted": redacted,
        }
        if truncated:
            data["next_start_line"] = next_start
            data["hint"] = (
                f"内容超出 {max_chars} 字符被截断,"
                f"用 start_line={next_start} 继续读。"
            )
        if redacted:
            data["hint_secret"] = (
                "这是凭据类文件,里面的密钥值已打码 —— "
                "不要把打码后的值当成真 key 使用,需要真 key 请让用户提供。"
            )

        self.logger.info(
            self.name,
            f"读取 {path}({_human_size(size)},{encoding},{total} 行)",
        )

        return ToolResult(
            success=True,
            data=data,
            meta={
                "source": self.name,
                "encoding": encoding,
                "size": size,
                "lines": total,
                "truncated": truncated,
                "redacted": redacted,
            },
        )
