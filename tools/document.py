"""文档读取工具 —— 让 agent 能读文档内容,并能**指回原文**。

## 两个层次

1. **读**:把文档变成带锚点的 markdown,喂给 LLM
2. **定位**:`locate_document` 把锚点还原成原文位置(页码/表名/单元格)

第 2 步不能省。没有它,LLM 拿到的是一段"扁平文本",
用户问"原文怎么说的""第 3 页那句呢",它只能**转述或猜** ——
用户无法核对,agent 也没有可信的引用。

## 为什么不直接用 markitdown

markitdown 很好,但它输出**扁平 markdown**,丢掉了位置信息
(第几页、哪个 sheet、哪个单元格)。而位置正是锚点的原料。

所以:
- **docx / xlsx / pptx / pdf 走本模块自己的解析器** —— 能拿到块级定位
- **其他格式(html/epub/…)退回 markitdown** —— 有就用,没有就报错

依赖是**可选**的:没装 pdfplumber 就不能读 PDF,但 docx/xlsx 照常。

## 扫描件必须明确报错

中文公文 PDF 大量是**扫描件**(整页图片,没有文本层)。
pdfplumber 对这类文件返回 **0 个字符**。

如果照常返回空字符串,LLM 会回答"文档中未提及相关内容" ——
那是**幻觉**,而且看起来完全正常。

所以本模块会检测这种情况,并明确返回
「这份 PDF 是扫描件,没有文本层,需要 OCR」。
"""
from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


# ===== 默认限制 =====

DEFAULT_MAX_CHARS = 60_000
MAX_FILE_BYTES = 100 * 1024 * 1024
#: 单次最多返回多少个块
DEFAULT_MAX_BLOCKS = 400

#: 支持的扩展名
SUPPORTED_SUFFIXES = {
    ".docx", ".xlsx", ".xlsm", ".pptx", ".pdf", ".md", ".markdown",
    ".txt", ".csv", ".html", ".htm", ".json", ".xml", ".log",
}


# ============================================================
# 数据模型
# ============================================================

@dataclass
class Locator:
    """块在原文中的位置。字段按类型可选。"""
    page: Optional[int] = None        # PDF 页码(1 起)
    sheet: Optional[str] = None       # xlsx 工作表名
    slide: Optional[int] = None       # pptx 幻灯片号(1 起)
    paragraph: Optional[int] = None   # docx 段落序号(0 起)
    cell: Optional[str] = None        # xlsx 单元格范围,如 "A1:C3"
    path: Optional[str] = None        # 标题路径,如 "项目与方案 > 环境说明"

    def label(self) -> str:
        """人类/模型可读的短标签。"""
        parts = []
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.sheet is not None:
            parts.append(self.sheet)
        if self.slide is not None:
            parts.append(f"slide.{self.slide}")
        if self.cell is not None:
            parts.append(self.cell)
        if self.paragraph is not None and self.path is None:
            parts.append(f"¶{self.paragraph}")
        return "|".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class DocBlock:
    """文档中的一个块(段落/标题/表格/单元格组/页)。"""
    id: str                 # B1, B2, ...
    kind: str               # heading/paragraph/table/code/list
    text: str
    locator: Locator = field(default_factory=Locator)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "locator": self.locator.to_dict(),
        }


@dataclass
class ParseResult:
    """解析结果。"""
    blocks: List[DocBlock] = field(default_factory=list)
    fmt: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    #: 非空 = 这份文档读不了,原因写在这里
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


class DocumentError(Exception):
    """文档读不了(缺依赖、扫描件、加密……)。"""


# ============================================================
# 格式探测
# ============================================================

def detect_format(path: Path) -> str:
    """按扩展名 + 文件头判断格式。

    **扩展名不可全信**:一个叫 `a.docx` 的文件可能根本不是 OOXML。
    所以 OOXML 三兄弟(.docx/.xlsx/.pptx)必须验内部结构,
    验不过就当"未知格式" —— 不能按扩展名硬解,否则会拿到乱码。
    """
    ext = path.suffix.lower()
    if ext in (".docx", ".xlsx", ".xlsm", ".pptx"):
        # OOXML 是 zip,扩展名不可信,验一下内部结构
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
            if "word/document.xml" in names:
                return "docx"
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("ppt/") for n in names):
                return "pptx"
        except Exception:
            pass
        return "unknown"
    if ext == ".pdf":
        return "pdf"
    if ext in (".md", ".markdown"):
        return "md"
    if ext == ".txt":
        return "txt"
    if ext == ".csv":
        return "csv"
    if ext in (".html", ".htm"):
        return "html"
    if ext == ".json":
        return "json"
    if ext == ".xml":
        return "xml"
    if ext == ".log":
        return "log"
    return "unknown"


#: 已知二进制扩展名 —— 直接拒,不做内容嗅探
#:
#: 内容嗅探(含 NUL 字节 / 非 UTF-8)能挡住绝大多数二进制,
#: 但挡不住"短小且恰好全是 ASCII"的二进制(比如 2 字节的 "MZ")。
#: 扩展名黑名单补上这一类。
BINARY_SUFFIXES = {
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".bin", ".obj", ".o", ".a",
    ".class", ".pyc", ".pyo", ".pyd", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".webp", ".tiff",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".wav", ".flac",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".db", ".sqlite", ".sqlite3", ".mdb", ".dat",
    ".py", ".pyc",
}


def _looks_binary(path: Path, probe: int = 4096) -> bool:
    """粗判是不是二进制文件。

    用途:未知格式兜底时,别把 .exe / .dll 这类二进制当文本读出来 ——
    读出来是乱码,LLM 会拿着乱码编答案。

    两道:
    1. 扩展名黑名单(短小二进制嗅探不出来)
    2. 内容嗅探:含 NUL 字节,或不是合法 UTF-8
    """
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(probe)
    except Exception:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


# ============================================================
# 解析器
# ============================================================

def _add(blocks: List[DocBlock], kind: str, text: str, locator: Locator) -> None:
    text = (text or "").rstrip()
    if not text.strip():
        return
    blocks.append(DocBlock(id=f"B{len(blocks) + 1}", kind=kind, text=text, locator=locator))


def parse_docx(path: Path) -> ParseResult:
    """解析 .docx —— 按**文档顺序**取段落与表格。

    python-docx 的 `document.paragraphs` 和 `document.tables` 是**两个独立列表**,
    直接用会丢掉"哪个表格在哪个段落之间"的顺序信息。
    这里遍历 body 的子元素按真实顺序取。
    """
    try:
        from docx import Document
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:
        raise DocumentError(
            "读 .docx 需要 python-docx:pip install python-docx"
        ) from exc

    doc = Document(str(path))
    blocks: List[DocBlock] = []
    heading_stack: List[str] = []
    para_idx = 0

    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = para.text
            style = (para.style.name if para.style is not None else "") or ""
            is_heading = style.lower().startswith("heading")
            if is_heading:
                # 维护标题路径(用于定位)
                try:
                    level = int(style.split()[-1])
                except Exception:
                    level = 1
                heading_stack = heading_stack[: level - 1]
                heading_stack.append(text.strip())
                _add(blocks, "heading", text, Locator(paragraph=para_idx))
            else:
                path_label = " > ".join(heading_stack) if heading_stack else None
                _add(blocks, "paragraph", text, Locator(paragraph=para_idx, path=path_label))
            para_idx += 1
        elif tag == "tbl":
            table = Table(child, doc)
            rows = []
            for row in table.rows:
                rows.append([c.text.strip() for c in row.cells])
            if not rows:
                continue
            md_rows = ["| " + " | ".join(r) + " |" for r in rows]
            if len(rows) > 1:
                md_rows.insert(1, "| " + " | ".join(["---"] * len(rows[0])) + " |")
            path_label = " > ".join(heading_stack) if heading_stack else None
            _add(blocks, "table", "\n".join(md_rows), Locator(path=path_label))

    return ParseResult(
        blocks=blocks,
        fmt="docx",
        meta={"paragraphs": para_idx, "tables": sum(1 for b in blocks if b.kind == "table")},
    )


def parse_xlsx(path: Path) -> ParseResult:
    """解析 .xlsx —— 每个工作表一个表块。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise DocumentError(
            "读 .xlsx 需要 openpyxl:pip install openpyxl"
        ) from exc

    wb = load_workbook(str(path), data_only=True, read_only=True)
    blocks: List[DocBlock] = []
    sheets = []
    try:
        for ws in wb.worksheets:
            sheets.append(ws.title)
            rows = []
            for row in ws.iter_rows(values_only=True):
                vals = ["" if v is None else str(v) for v in row]
                # 跳过整行空
                if any(v.strip() for v in vals):
                    rows.append(vals)
            if not rows:
                continue
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            md_rows = ["| " + " | ".join(r) + " |" for r in rows]
            if len(rows) > 1:
                md_rows.insert(1, "| " + " | ".join(["---"] * width) + " |")
            cell_range = f"A1:{_col_letter(width)}{len(rows)}"
            _add(blocks, "table", "\n".join(md_rows), Locator(sheet=ws.title, cell=cell_range))
    finally:
        try:
            wb.close()
        except Exception:
            pass

    return ParseResult(blocks=blocks, fmt="xlsx", meta={"sheets": sheets})


def _col_letter(idx: int) -> str:
    """1 → A, 26 → Z, 27 → AA。"""
    out = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"


def parse_pptx(path: Path) -> ParseResult:
    """解析 .pptx —— 每张幻灯片一个块。"""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise DocumentError(
            "读 .pptx 需要 python-pptx:pip install python-pptx"
        ) from exc

    prs = Presentation(str(path))
    blocks: List[DocBlock] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    text = "".join(r.text for r in para.runs).strip()
                    if text:
                        lines.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
        _add(blocks, "paragraph", "\n".join(lines), Locator(slide=i))

    return ParseResult(blocks=blocks, fmt="pptx", meta={"slides": len(prs.slides._sldIdLst)})


def parse_pdf(path: Path) -> ParseResult:
    """解析 .pdf —— 按页切块。

    **扫描件必须识别出来**:pdfplumber 对整页图片的 PDF 返回 0 字符。
    照常返回空会让 LLM 回答"文档里没有提到…" —— 那是幻觉。
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise DocumentError(
            "读 .pdf 需要 pdfplumber:pip install pdfplumber(或 markitdown[pdf])"
        ) from exc

    blocks: List[DocBlock] = []
    total_pages = 0
    empty_pages = 0
    try:
        with pdfplumber.open(str(path)) as pdf:
            total_pages = len(pdf.pages)
            for pno, page in enumerate(pdf.pages, start=1):
                text = (page.extract_text() or "").strip()
                tables = page.extract_tables() or []
                if not text and not tables:
                    empty_pages += 1
                    continue
                if text:
                    _add(blocks, "paragraph", text, Locator(page=pno))
                for ti, tbl in enumerate(tables, start=1):
                    md_rows = []
                    for row in tbl:
                        cells = ["" if c is None else str(c).replace("\n", " ") for c in row]
                        if any(c.strip() for c in cells):
                            md_rows.append("| " + " | ".join(cells) + " |")
                    if md_rows:
                        _add(blocks, "table", "\n".join(md_rows), Locator(page=pno))
    except Exception as exc:
        raise DocumentError(f"PDF 解析失败:{exc}") from exc

    if not blocks:
        if total_pages and empty_pages == total_pages:
            raise DocumentError(
                f"这份 PDF 是**扫描件**(共 {total_pages} 页,全部没有文本层),"
                f"无法直接提取文字,需要先做 OCR。"
            )
        raise DocumentError("PDF 里没有可提取的内容(可能是空文件或加密文件)")

    return ParseResult(
        blocks=blocks,
        fmt="pdf",
        meta={"pages": total_pages, "empty_pages": empty_pages},
    )


def parse_text(path: Path, fmt: str) -> ParseResult:
    """解析纯文本类文件(md/txt/csv/json/xml/log)。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks: List[DocBlock] = []
    if fmt in ("md", "markdown"):
        for i, para in enumerate(re.split(r"\n\s*\n", raw)):
            kind = "heading" if re.match(r"^\s{0,3}#{1,6}\s", para) else "paragraph"
            _add(blocks, kind, para, Locator(paragraph=i))
    else:
        for i, line in enumerate(raw.splitlines()):
            _add(blocks, "paragraph", line, Locator(paragraph=i))
    return ParseResult(blocks=blocks, fmt=fmt, meta={"chars": len(raw)})


def parse_html(path: Path) -> ParseResult:
    """HTML —— 优先 markitdown,否则用 bs4 粗提取。"""
    text = None
    try:
        from markitdown import MarkItDown

        result = MarkItDown().convert(str(path))
        text = result.text_content
    except Exception:
        text = None
    if not text:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = soup.get_text("\n", strip=True)
        except Exception as exc:
            raise DocumentError(f"读 HTML 失败:{exc}") from exc

    blocks: List[DocBlock] = []
    for i, para in enumerate(re.split(r"\n\s*\n", text)):
        _add(blocks, "paragraph", para, Locator(paragraph=i))
    return ParseResult(blocks=blocks, fmt="html", meta={"chars": len(text)})


_PARSERS = {
    "docx": parse_docx,
    "xlsx": parse_xlsx,
    "pptx": parse_pptx,
    "pdf": parse_pdf,
    "html": parse_html,
}


def parse_document(path: Path) -> ParseResult:
    """解析文档 → 带定位的块列表。"""
    if not path.exists():
        raise DocumentError(f"文件不存在:{path}")
    if not path.is_file():
        raise DocumentError(f"不是文件:{path}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise DocumentError(
            f"文件太大({size / 1024 / 1024:.1f} MB,上限 "
            f"{MAX_FILE_BYTES / 1024 / 1024:.0f} MB)"
        )

    fmt = detect_format(path)
    if fmt in ("md", "markdown"):
        fmt = "md"
    if fmt in ("md", "txt", "csv", "json", "xml", "log"):
        return parse_text(path, fmt)
    if fmt == "html":
        return parse_html(path)

    parser = _PARSERS.get(fmt)
    if parser is None:
        # 未知格式 → 试试 markitdown(它支持一堆冷门格式)
        #
        # **但二进制文件不能走这条路** —— markitdown 可能把 .exe 读成乱码,
        # LLM 拿着乱码照样能编出答案。先粗判一下是不是二进制。
        if _looks_binary(path):
            raise DocumentError(
                f"不支持的格式 {path.suffix or '(无扩展名)'!r},"
                f"而且看起来是二进制文件。支持的格式:"
                f"{', '.join(sorted(SUPPORTED_SUFFIXES))}"
            )
        try:
            from markitdown import MarkItDown

            result = MarkItDown().convert(str(path))
            blocks: List[DocBlock] = []
            for i, para in enumerate(re.split(r"\n\s*\n", result.text_content or "")):
                _add(blocks, "paragraph", para, Locator(paragraph=i))
            if blocks:
                return ParseResult(blocks=blocks, fmt=fmt)
        except Exception:
            pass
        raise DocumentError(
            f"不支持的格式 {fmt!r}。支持:{', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    return parser(path)


# ============================================================
# 渲染
# ============================================================

def render_anchored_markdown(blocks: List[DocBlock], title: str = "") -> str:
    """把块渲染成带锚点的 markdown。

    锚点自带定位信息,LLM 引用时可以直接说"见 [B7 p.3]"。
    """
    lines: List[str] = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    for b in blocks:
        loc = b.locator.label()
        anchor = f"[{b.id}{' ' + loc if loc else ''}]"
        if b.kind == "heading":
            lines.append(f"{anchor} {b.text}")
        elif b.kind == "table":
            lines.append(f"{anchor}")
            lines.append(b.text)
        else:
            lines.append(f"{anchor} {b.text}")
        lines.append("")
    return "\n".join(lines).strip()


def parse_block_range(spec: str, total: int) -> Tuple[int, int]:
    """解析块范围 "10-40" / "10" / "10-" → (start, end)(1 起,闭区间)。"""
    spec = (spec or "").strip()
    if not spec:
        return 1, total
    m = re.match(r"^(\d+)?\s*-\s*(\d+)?$", spec)
    if m:
        start = int(m.group(1)) if m.group(1) else 1
        end = int(m.group(2)) if m.group(2) else total
    else:
        # 单个数字:"10" = 从第 10 块到末尾
        m = re.match(r"^(\d+)$", spec)
        if not m:
            raise ValueError(f"无法解析范围 {spec!r}(用 '10-40' 或 '10' 这种写法)")
        start = int(m.group(1))
        end = total
    start = max(1, start)
    end = min(total, end)
    if start > end:
        raise ValueError(f"范围 {spec!r} 是空的")
    return start, end


# ============================================================
# 工具
# ============================================================

class ReadDocumentTool(Tool):
    """读取文档内容,返回带锚点的 markdown。"""

    name = "read_document"
    description = (
        "读取文档(pdf/docx/xlsx/pptx/md/txt/html/csv)并返回带锚点的 markdown。"
        "每个块前面有锚点(如 [B7 p.3]),回答时引用锚点,用户可以核对原文。"
        "文档很大时用 blocks 参数分段读。"
        "扫描件 PDF 没有文本层,会明确报错而不是返回空。"
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="path",
                    type="string",
                    description="文件路径(绝对或相对当前工作目录)",
                    required=True,
                ),
                ToolParam(
                    name="blocks",
                    type="string",
                    description="要读的块范围,如 '1-40'。不给则从头读。",
                    required=False,
                    default=None,
                ),
                ToolParam(
                    name="max_chars",
                    type="integer",
                    description=f"返回字符上限(默认 {DEFAULT_MAX_CHARS})",
                    required=False,
                    default=None,
                ),
            ],
            returns={
                "blocks": "块总数",
                "markdown": "带锚点的 markdown",
                "truncated": "是否因长度被截断",
                "format": "文档格式",
            },
        )

    async def execute(self, **kwargs) -> ToolResult:
        path_str = str(kwargs.get("path") or "").strip()
        if not path_str:
            return ToolResult(success=False, error="path 不能为空")
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        try:
            result = parse_document(path)
        except DocumentError as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            return ToolResult(success=False, error=f"解析失败:{type(exc).__name__}: {exc}")

        total = len(result.blocks)
        try:
            start, end = parse_block_range(str(kwargs.get("blocks") or ""), total)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

        selected = result.blocks[start - 1:end]
        max_chars = int(kwargs.get("max_chars") or DEFAULT_MAX_CHARS)
        markdown = render_anchored_markdown(selected, title=f"{path.name}")

        truncated = False
        if len(markdown) > max_chars:
            markdown = markdown[:max_chars] + "\n\n[内容超出长度上限,已截断。用 blocks 参数继续读。]"
            truncated = True

        return ToolResult(
            success=True,
            data={
                "path": str(path),
                "format": result.fmt,
                "total_blocks": total,
                "returned_blocks": len(selected),
                "range": f"{start}-{end}",
                "markdown": markdown,
                "truncated": truncated,
                "meta": result.meta,
            },
        )


class LocateDocumentTool(Tool):
    """把锚点还原成原文位置与内容。"""

    name = "locate_document"
    description = (
        "把 read_document 返回的锚点(如 B7)还原成原文位置与内容。"
        "用户问'原文怎么说的'时用它去核对。"
    )

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="path", type="string", description="文档路径", required=True),
                ToolParam(
                    name="anchor",
                    type="string",
                    description="锚点 id,如 'B7'。不带参数则列出前若干块。",
                    required=False,
                    default=None,
                ),
            ],
            returns={
                "anchor": "锚点",
                "text": "原文",
                "locator": "位置(页码/表名/单元格)",
            },
        )

    async def execute(self, **kwargs) -> ToolResult:
        path_str = str(kwargs.get("path") or "").strip()
        if not path_str:
            return ToolResult(success=False, error="path 不能为空")
        path = Path(path_str).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        try:
            result = parse_document(path)
        except DocumentError as exc:
            return ToolResult(success=False, error=str(exc))

        anchor = str(kwargs.get("anchor") or "").strip().upper()
        if not anchor:
            return ToolResult(
                success=True,
                data={
                    "path": str(path),
                    "total_blocks": len(result.blocks),
                    "blocks": [b.to_dict() for b in result.blocks[:50]],
                },
            )

        want = anchor if anchor.startswith("B") else f"B{anchor}"
        for b in result.blocks:
            if b.id.upper() == want:
                return ToolResult(
                    success=True,
                    data={
                        "path": str(path),
                        "anchor": b.id,
                        "kind": b.kind,
                        "text": b.text,
                        "locator": b.locator.to_dict(),
                        "locator_label": b.locator.label(),
                    },
                )
        return ToolResult(
            success=False,
            error=f"锚点 {want} 不存在(共 {len(result.blocks)} 个块)",
        )
