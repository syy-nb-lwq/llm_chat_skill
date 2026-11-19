"""文件读取工具(read_file)的测试。

重点:
1. **10MB 上限** —— 超了必须明确报错,不能"截一半当全文"
2. **二进制/文档文件要指对工具** —— 让 agent 换工具,而不是拿到乱码
3. **相对路径能找得到** —— agent 眼里的路径和进程 cwd 不是一个地方
4. **凭据文件里的密钥要打码** —— 工具结果会进对话/会话库/日志
5. **大文件能分段读** —— 一次读不完时得告诉 agent 从哪行续
"""
from pathlib import Path

import pytest

from tools.file_read import (
    MAX_FILE_BYTES,
    ReadFileTool,
    decode_bytes,
    is_secret_file,
    resolve_path,
)


@pytest.fixture
def tool():
    return ReadFileTool()


@pytest.fixture
def code_file(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(
        "import os\n"
        "\n"
        "\n"
        "def main():\n"
        "    return os.getcwd()\n",
        encoding="utf-8",
    )
    return path


# ============================================================
# 基本读取
# ============================================================

@pytest.mark.asyncio
async def test_reads_text_file_with_line_numbers(tool, code_file):
    result = await tool.execute(path=str(code_file))

    assert result.success
    data = result.data
    assert data["path"] == str(code_file)
    assert data["total_lines"] == 5
    assert data["encoding"] == "utf-8"
    assert "import os" in data["content"]
    # 行号是 1 起
    assert data["content"].splitlines()[0].strip().startswith("1 |")


@pytest.mark.asyncio
async def test_line_numbers_can_be_turned_off(tool, code_file):
    result = await tool.execute(path=str(code_file), line_numbers=False)

    assert result.success
    assert result.data["content"].splitlines()[0] == "import os"


@pytest.mark.asyncio
async def test_reads_only_the_requested_line_range(tool, tmp_path):
    path = tmp_path / "lines.txt"
    path.write_text("\n".join(f"line-{i}" for i in range(1, 21)), encoding="utf-8")

    result = await tool.execute(path=str(path), start_line=3, end_line=5)

    assert result.success
    content = result.data["content"]
    assert "line-3" in content and "line-5" in content
    assert "line-2" not in content and "line-6" not in content
    assert result.data["returned_lines"] == 3


@pytest.mark.asyncio
async def test_truncation_tells_where_to_continue(tool, tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("\n".join("0123456789" for _ in range(200)), encoding="utf-8")

    result = await tool.execute(path=str(path), max_chars=120)

    assert result.success
    assert result.data["truncated"] is True
    assert result.data["next_start_line"] > 1
    assert "start_line=" in result.data["hint"]


@pytest.mark.asyncio
async def test_empty_file_is_not_an_error(tool, tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    result = await tool.execute(path=str(path))

    assert result.success
    assert result.data["total_lines"] == 0
    assert result.data["content"] == ""


# ============================================================
# 10MB 上限
# ============================================================

def test_size_limit_is_10mb():
    assert MAX_FILE_BYTES == 10 * 1024 * 1024


@pytest.mark.asyncio
async def test_rejects_file_over_the_limit(tool, tmp_path):
    path = tmp_path / "big.bin"
    # 稀疏文件:瞬间造出 10MB+1 字节,不用真写 10MB 数据
    with path.open("wb") as f:
        f.truncate(MAX_FILE_BYTES + 1)

    result = await tool.execute(path=str(path))

    assert result.success is False
    assert "太大" in result.error
    assert "10.0 MB" in result.error
    assert result.meta["size"] == MAX_FILE_BYTES + 1


# ============================================================
# 二进制 / 文档文件
# ============================================================

@pytest.mark.asyncio
async def test_rejects_binary_file(tool, tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"\x00\x01\x02\x03binary")

    result = await tool.execute(path=str(path))

    assert result.success is False
    assert "二进制" in result.error


@pytest.mark.asyncio
async def test_document_suffix_points_to_read_document(tool, tmp_path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    result = await tool.execute(path=str(path))

    assert result.success is False
    assert "read_document" in result.error


# ============================================================
# 路径解析
# ============================================================

@pytest.mark.asyncio
async def test_missing_file_reports_where_it_looked(tool, tmp_path):
    result = await tool.execute(path=str(tmp_path / "nope.txt"))

    assert result.success is False
    assert "文件不存在" in result.error
    assert "试过的位置" in result.error


@pytest.mark.asyncio
async def test_relative_path_resolves_against_cwd(tool, tmp_path, monkeypatch):
    (tmp_path / "rel.txt").write_text("相对路径也能读\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await tool.execute(path="rel.txt")

    assert result.success
    assert "相对路径也能读" in result.data["content"]


def test_resolve_path_returns_tried_locations(tmp_path):
    path, tried = resolve_path(str(tmp_path / "absent.txt"))

    assert path is None
    assert tried and all(isinstance(t, str) for t in tried)


@pytest.mark.asyncio
async def test_directory_path_lists_entries(tool, tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()

    result = await tool.execute(path=str(tmp_path))

    assert result.success is False
    assert "是目录" in result.error
    assert "a.txt" in result.error
    assert "sub/" in result.error


# ============================================================
# 编码
# ============================================================

def test_decodes_gbk_file(tmp_path):
    path = tmp_path / "gbk.txt"
    path.write_bytes("中文编码测试\n".encode("gbk"))

    text, encoding = decode_bytes(path.read_bytes())

    assert encoding == "gb18030"
    assert "中文编码测试" in text


def test_handles_utf8_bom():
    text, encoding = decode_bytes("带 BOM 的内容".encode("utf-8-sig"))

    assert encoding == "utf-8-sig"
    assert text == "带 BOM 的内容"


# ============================================================
# 凭据文件打码
# ============================================================

@pytest.mark.asyncio
async def test_secret_file_keys_are_masked(tool, tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "OPENAI_API_KEY=sk-abcdefghijklmnop123456\n"
        "DB_PASSWORD=superlongpassword123\n",
        encoding="utf-8",
    )

    result = await tool.execute(path=str(path))

    assert result.success
    assert result.data["redacted"] is True
    assert "sk-abcdefghijklmnop123456" not in result.data["content"]
    assert "superlongpassword123" not in result.data["content"]
    assert "打码" in result.data["hint_secret"]


def test_secret_detection():
    base = Path("C:/x")
    assert is_secret_file(base / ".env") is True
    assert is_secret_file(base / "server.pem") is True
    assert is_secret_file(base / ".env.example") is False
    assert is_secret_file(base / "main.py") is False


@pytest.mark.asyncio
async def test_normal_file_is_not_redacted(tool, tmp_path):
    path = tmp_path / "app.py"
    path.write_text("API_KEY = 'sk-abcdefghijklmnop123456'\n", encoding="utf-8")

    result = await tool.execute(path=str(path))

    assert result.success
    assert result.data["redacted"] is False
    assert "sk-abcdefghijklmnop123456" in result.data["content"]


# ============================================================
# 注册
# ============================================================

def test_tool_is_registered_in_hub():
    from tools.hub import get_tool_hub

    hub = get_tool_hub()
    assert "read_file" in hub.names()
    assert hub.get_tool("read_file") is not None
