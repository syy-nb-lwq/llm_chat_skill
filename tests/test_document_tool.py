"""文档工具的测试。

重点:
1. 各格式能不能读出来(docx/xlsx/pptx/pdf/md/csv/html)
2. **锚点能不能定位回原文** —— 这是这层的核心价值
3. **扫描件必须明确报错** —— 返回空会让 LLM 幻觉"文档里没有提到…"
4. 大文档能分段读,不会一次撑爆上下文
"""
import pytest

from tools.document import (
    Locator,
    detect_format,
    parse_block_range,
    parse_document,
    render_anchored_markdown,
)
from tools.document import LocateDocumentTool, ReadDocumentTool


# ============================================================
# 测试固件(用真实的文件格式,不用 mock)
# ============================================================

@pytest.fixture
def docx_file(tmp_path):
    docx = pytest.importorskip("docx", reason="需要 python-docx")
    from docx import Document

    doc = Document()
    doc.add_heading("项目与方案", level=1)
    doc.add_paragraph("本文档用于验证文档解析能力。")
    doc.add_heading("环境说明", level=2)
    doc.add_paragraph("开发环境:Windows。生产环境:Linux。")
    t = doc.add_table(rows=2, cols=2)
    t.cell(0, 0).text = "项"
    t.cell(0, 1).text = "值"
    t.cell(1, 0).text = "超时"
    t.cell(1, 1).text = "30s"
    path = tmp_path / "fixture.docx"
    doc.save(str(path))
    return path


@pytest.fixture
def xlsx_file(tmp_path):
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "数据"
    ws.append(["城市", "年度", "价格"])
    ws.append(["北京", 2025, 100])
    ws.append(["上海", 2025, 120])
    ws2 = wb.create_sheet("说明")
    ws2["A1"] = "采样时间:2025-12-31"
    path = tmp_path / "fixture.xlsx"
    wb.save(str(path))
    return path


@pytest.fixture
def md_file(tmp_path):
    path = tmp_path / "a.md"
    path.write_text(
        "# 标题\n\n第一段内容。\n\n第二段内容。\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def text_pdf(tmp_path):
    """有文本层的 PDF。"""
    pytest.importorskip("pdfplumber")
    reportlab = pytest.importorskip("reportlab", reason="需要 reportlab(仅测试用)")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = tmp_path / "text.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 16)
    c.drawString(72, 780, "Quarterly Report")
    c.setFont("Helvetica", 11)
    c.drawString(72, 740, "Revenue grew by 12 percent.")
    c.showPage()
    c.setFont("Helvetica", 16)
    c.drawString(72, 780, "Appendix A: Method")
    c.save()
    return path


@pytest.fixture
def scanned_pdf(tmp_path):
    """扫描件 PDF —— 整页图片,没有文本层。

    没有 reportlab 就用 PyPDF2 造一个空页(同样没有文本层)。
    """
    pytest.importorskip("pdfplumber")
    path = tmp_path / "scanned.pdf"
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        c = canvas.Canvas(str(path), pagesize=A4)
        c.showPage()   # 空白页 = 无文本层
        c.save()
    except ImportError:
        import PyPDF2

        writer = PyPDF2.PdfWriter()
        writer.add_blank_page(width=595, height=842)
        with open(path, "wb") as f:
            writer.write(f)
    return path


# ============================================================
# 1. 格式探测
# ============================================================

def test_detect_format_by_extension(docx_file, xlsx_file, md_file):
    assert detect_format(docx_file) == "docx"
    assert detect_format(xlsx_file) == "xlsx"
    assert detect_format(md_file) == "md"


def test_detect_format_rejects_fake_docx(tmp_path):
    """扩展名说是 docx,内容不是 —— 不能信扩展名。"""
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"this is not a zip")
    assert detect_format(fake) != "docx"


# ============================================================
# 2. 解析
# ============================================================

def test_parse_docx_keeps_document_order(docx_file):
    """段落和表格必须按**文档里的真实顺序**返回。

    python-docx 的 paragraphs / tables 是两个独立列表,
    直接用会丢掉"表格在第几段之后"的信息。
    """
    result = parse_document(docx_file)
    kinds = [b.kind for b in result.blocks]
    assert kinds == ["heading", "paragraph", "heading", "paragraph", "table"]
    texts = [b.text for b in result.blocks]
    assert "项目与方案" in texts[0]
    assert "环境说明" in texts[2]
    assert "超时" in texts[4]


def test_parse_docx_tracks_heading_path(docx_file):
    result = parse_document(docx_file)
    # "环境说明" 下面的段落应该带标题路径
    para = [b for b in result.blocks if "开发环境" in b.text][0]
    assert para.locator.path == "项目与方案 > 环境说明"


def test_parse_xlsx_one_block_per_sheet(xlsx_file):
    result = parse_document(xlsx_file)
    assert len(result.blocks) == 2
    assert result.blocks[0].locator.sheet == "数据"
    assert result.blocks[1].locator.sheet == "说明"
    assert "北京" in result.blocks[0].text
    assert result.blocks[0].locator.cell == "A1:C3"


def test_parse_pdf_pages_as_blocks(text_pdf):
    result = parse_document(text_pdf)
    assert len(result.blocks) == 2
    assert result.blocks[0].locator.page == 1
    assert result.blocks[1].locator.page == 2
    assert "Appendix" in result.blocks[1].text


def test_parse_md_paragraphs(md_file):
    result = parse_document(md_file)
    assert len(result.blocks) >= 3
    # markdown 的标题应识别出来
    assert result.blocks[0].kind == "heading"
    assert "第一段" in result.blocks[1].text


def test_parse_csv(tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    result = parse_document(path)
    assert result.blocks
    assert "a,b" in result.blocks[0].text


# ============================================================
# 3. 扫描件 —— 必须明确报错
# ============================================================

def test_scanned_pdf_raises_clear_error(scanned_pdf):
    """扫描件不能返回空 —— 返回空会让 LLM 说"文档里没提到…"。

    那是幻觉,而且看起来完全正常。
    """
    from tools.document import DocumentError

    with pytest.raises(DocumentError) as exc:
        parse_document(scanned_pdf)
    assert "扫描件" in str(exc.value)
    assert "OCR" in str(exc.value)


@pytest.mark.asyncio
async def test_read_document_reports_scanned_pdf(scanned_pdf):
    result = await ReadDocumentTool().execute(path=str(scanned_pdf))
    assert not result.success
    assert "扫描件" in result.error


# ============================================================
# 4. 锚点 —— 核心价值
# ============================================================

def test_render_anchors_carry_locator(docx_file, text_pdf):
    md = render_anchored_markdown(parse_document(text_pdf).blocks, title="x")
    assert "[B1 p.1]" in md, "锚点必须带页码"
    assert "[B2 p.2]" in md


def test_render_anchors_carry_sheet(xlsx_file):
    md = render_anchored_markdown(parse_document(xlsx_file).blocks)
    assert "[B1 数据" in md
    assert "A1:C3]" in md


@pytest.mark.asyncio
async def test_locate_resolves_anchor_to_source(docx_file):
    """锚点必须能还原回原文 —— 否则引用无法核对。"""
    result = await ReadDocumentTool().execute(path=str(docx_file))
    assert result.success
    md = result.data["markdown"]
    assert "[B" in md

    # 取一个带正文的锚点
    import re

    m = re.search(r"\[(B\d+)\]\s*(\S[^\n]*)", md)
    anchor = m.group(1)
    expected_text = m.group(2).strip()

    loc = await LocateDocumentTool().execute(path=str(docx_file), anchor=anchor)
    assert loc.success
    assert expected_text.split(" ")[0] in loc.data["text"]


@pytest.mark.asyncio
async def test_locate_unknown_anchor_errors(docx_file):
    loc = await LocateDocumentTool().execute(path=str(docx_file), anchor="B9999")
    assert not loc.success
    assert "不存在" in loc.error


@pytest.mark.asyncio
async def test_locate_without_anchor_lists_blocks(docx_file):
    loc = await LocateDocumentTool().execute(path=str(docx_file))
    assert loc.success
    assert loc.data["total_blocks"] >= 4
    assert len(loc.data["blocks"]) >= 4


# ============================================================
# 5. 分段读
# ============================================================

def test_parse_block_range():
    assert parse_block_range("", 100) == (1, 100)
    assert parse_block_range("10-40", 100) == (10, 40)
    assert parse_block_range("10", 100) == (10, 100)
    assert parse_block_range("10-", 100) == (10, 100)
    # 越界要夹住
    assert parse_block_range("90-200", 100) == (90, 100)


def test_parse_block_range_invalid():
    with pytest.raises(ValueError):
        parse_block_range("abc", 100)
    with pytest.raises(ValueError):
        parse_block_range("50-10", 100)


@pytest.mark.asyncio
async def test_read_document_can_read_a_range(md_file):
    full = await ReadDocumentTool().execute(path=str(md_file))
    assert full.success
    total = full.data["total_blocks"]

    part = await ReadDocumentTool().execute(path=str(md_file), blocks="1-1")
    assert part.success
    assert part.data["returned_blocks"] == 1
    assert part.data["total_blocks"] == total


@pytest.mark.asyncio
async def test_read_document_truncates_long_output(md_file):
    result = await ReadDocumentTool().execute(path=str(md_file), max_chars=50)
    assert result.success
    assert result.data["truncated"] is True
    assert len(result.data["markdown"]) < 200


# ============================================================
# 6. 错误处理
# ============================================================

@pytest.mark.asyncio
async def test_missing_file_errors():
    result = await ReadDocumentTool().execute(path="D:/definitely/not/here.pdf")
    assert not result.success
    assert "不存在" in result.error


@pytest.mark.asyncio
async def test_empty_path_errors():
    result = await ReadDocumentTool().execute(path="")
    assert not result.success


@pytest.mark.asyncio
async def test_unsupported_format_errors(tmp_path):
    path = tmp_path / "a.exe"
    path.write_bytes(b"MZ")
    result = await ReadDocumentTool().execute(path=str(path))
    assert not result.success
    assert "不支持" in result.error or "格式" in result.error


def test_locator_label():
    assert Locator(page=3).label() == "p.3"
    assert Locator(sheet="数据", cell="A1:C3").label() == "数据|A1:C3"
    assert Locator(slide=2).label() == "slide.2"
    assert Locator().label() == ""
