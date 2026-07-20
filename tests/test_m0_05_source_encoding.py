"""M0-05 修复源码编码问题。

验收点:
- 编辑器/终端无乱码
- 接口行为不变

具体覆盖:
1. infra.logger._Utf8Stream 把 str 编码为 UTF-8 bytes 再写入底层流
2. infra.logger.StreamHandler 在 stdlib logging 中能消费 _Utf8Stream
3. core.memory_db.MemoryDB 存取中文 metadata/tags 时不被转义成 \\uXXXX
4. core.memory_db.MemoryDB.add/update 的 metadata/tags 列在 sqlite 中是合法 JSON 字符串
"""
import io
import json
import logging

import pytest


# ===== 1. _Utf8Stream 行为 =====

def test_utf8_stream_encodes_str_to_utf8_bytes():
    """_Utf8Stream.write 应把 str 先编码为 UTF-8 bytes 再写入底层流。"""
    from infra.logger import _Utf8Stream

    sink = io.BytesIO()
    s = _Utf8Stream(sink)
    n = s.write("你好,世界")
    assert n == len("你好,世界".encode("utf-8"))
    assert sink.getvalue() == "你好,世界".encode("utf-8")


def test_utf8_stream_passthrough_for_bytes():
    """_Utf8Stream.write 接收到 bytes 时应原样下传。"""
    from infra.logger import _Utf8Stream

    sink = io.BytesIO()
    s = _Utf8Stream(sink)
    s.write(b"raw bytes")
    assert sink.getvalue() == b"raw bytes"


def test_utf8_stream_handles_unencodable_via_replace():
    """当某些字符在窄代码页下不可编码时,应使用 errors='replace' 而不是抛错。"""
    from infra.logger import _Utf8Stream

    sink = io.BytesIO()
    s = _Utf8Stream(sink, encoding="ascii")
    # 不应抛 UnicodeEncodeError, 而是把不能编码的字符替换为 '?'
    s.write("中文")
    out = sink.getvalue()
    assert isinstance(out, bytes)
    assert b"?" in out


def test_utf8_stream_flush_and_isatty():
    from infra.logger import _Utf8Stream

    sink = io.BytesIO()
    s = _Utf8Stream(sink)
    assert s.flush() is None
    assert s.isatty() is False
    assert s.closed is False


# ===== 2. logging.StreamHandler 能消费 _Utf8Stream =====

def test_logger_stream_handler_consumes_utf8_stream(monkeypatch):
    """StreamHandler(_Utf8Stream(raw)) 能把日志写入二进制流,字节是 UTF-8。"""
    from infra import logger as logger_mod

    # 不污染全局 _logger,直接用 stdlib logger + handler
    test_logger = logging.getLogger("test_m0_05_stream_handler")
    test_logger.handlers.clear()
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    sink = io.BytesIO()
    handler = logging.StreamHandler(logger_mod._Utf8Stream(sink))
    handler.setFormatter(logging.Formatter("%(message)s"))
    test_logger.addHandler(handler)

    test_logger.info("终端中文: %s", "你好")
    out = sink.getvalue()
    assert "终端中文: 你好" in out.decode("utf-8")
    # 字节流里不能含 \\u 转义(默认 ensure_ascii=True 的情形)
    assert "\\u" not in out.decode("utf-8")


# ===== 3-4. MemoryDB 中文存取 =====

@pytest.fixture
def mem_db(tmp_path):
    """构造一个隔离在 tmp_path 的 MemoryDB 实例。"""
    from core.memory_db import MemoryDB

    db_path = tmp_path / "memory.db"
    return MemoryDB(db_path=db_path)


def test_memory_db_chinese_metadata_roundtrip(mem_db):
    """含中文的 metadata 写入后再读取应是原始中文,而不是 \\u 转义。"""
    meta = {"主题": "技能系统", "tags": ["教学", "召回"], "nested": {"子项": "值"}}
    entry = mem_db.add(type="preference", content="我偏好中文", metadata=meta)
    fetched = mem_db.get(entry.id)
    assert fetched is not None
    assert fetched.metadata == meta
    # 关键:不应该是 \\u 转义
    raw_meta_str = json.dumps(meta, ensure_ascii=False)
    assert raw_meta_str == json.dumps(fetched.metadata, ensure_ascii=False)


def test_memory_db_chinese_tags_roundtrip(mem_db):
    """中文 tags 列表写入后再读取应是中文列表。"""
    tags = ["中文", "测试", "记忆"]
    entry = mem_db.add(type="context", content="ctx", tags=tags)
    fetched = mem_db.get(entry.id)
    assert fetched.tags == tags


def test_memory_db_metadata_column_stored_as_native_unicode(mem_db, tmp_path):
    """直接检查 sqlite 数据库内容,确认 metadata / tags 列是原生中文(不含 \\u 转义)。"""
    import sqlite3

    entry = mem_db.add(
        type="preference",
        content="content-zh",
        metadata={"k": "中文值"},
        tags=["标签1", "标签2"],
    )

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT metadata, tags FROM memories WHERE id = ?", (entry.id,)
    ).fetchone()
    conn.close()

    metadata_str, tags_str = row[0], row[1]
    # 不应出现 \\u 转义序列
    assert "\\u" not in metadata_str, f"metadata 仍含 \\u 转义: {metadata_str}"
    assert "\\u" not in tags_str, f"tags 仍含 \\u 转义: {tags_str}"
    # 必须含原始中文
    assert "中文值" in metadata_str
    assert "标签1" in tags_str and "标签2" in tags_str
    # 反序列化合法
    assert json.loads(metadata_str) == {"k": "中文值"}
    assert json.loads(tags_str) == ["标签1", "标签2"]


def test_memory_db_update_preserves_chinese_metadata(mem_db):
    """update(metadata=...) 时也要用 ensure_ascii=False 写原生中文。"""
    entry = mem_db.add(type="preference", content="c", metadata={"old": "v"})
    ok = mem_db.update(entry.id, metadata={"new_key": "中文新值"})
    assert ok is True
    fetched = mem_db.get(entry.id)
    assert fetched.metadata == {"new_key": "中文新值"}


# ===== 5. 回归: 所有源码都是合法 UTF-8 =====

def test_all_python_source_is_valid_utf8():
    """所有 .py 文件能被 UTF-8 解码(防止新增乱码字节)。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    bad = []
    for path in root.rglob("*.py"):
        if any(seg in path.parts for seg in ("__pycache__", ".git")):
            continue
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            bad.append((path, str(exc)))
    assert not bad, f"非 UTF-8 文件: {bad}"