"""文件上传接口测试（POST /api/upload）。

这个接口存在的唯一理由：`read_document` 吃的是**后端磁盘上的路径**，
而浏览器里的 File 对象对后端不可见。所以测试的重点不是"能不能存下来"，
而是**存下来的路径能不能真的被文档工具读**（最后一条用例）。

覆盖：
  - 正常上传 → 返回绝对路径 + 格式
  - 白名单外扩展名 / 空文件 / 超限 → 拒
  - 恶意文件名（路径穿越）→ 只能落在上传区内
  - 改了后缀的假 docx → readable=False（用户点发送前就知道读不了）
  - 按会话分目录、同名不覆盖
  - 端到端：上传 → read_document 读回来
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from backend.main import app

    return TestClient(app)


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    """把上传目录指到临时目录 —— 绝不能往真实 memory/ 里写测试文件。"""
    import backend.main as main_mod

    root = tmp_path / "uploads"
    monkeypatch.setattr(main_mod, "_UPLOAD_ROOT", root)
    return root


def _minimal_docx() -> bytes:
    """最小的合法 OOXML（`detect_format` 只验 `word/document.xml` 在不在）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<document/>")
    return buf.getvalue()


def _post(client, name, data, session_id="s-test"):
    return client.post(
        "/api/upload",
        files={"file": (name, data, "application/octet-stream")},
        data={"session_id": session_id},
    )


def test_upload_docx_returns_readable_path(client, upload_root):
    r = _post(client, "季度报告.docx", _minimal_docx())

    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "季度报告.docx"
    assert body["format"] == "docx"
    assert body["readable"] is True
    assert body["size"] > 0
    # path 必须是**后端真实存在**的绝对路径，否则 agent 拿到也用不了
    from pathlib import Path

    target = Path(body["path"])
    assert target.is_absolute()
    assert target.is_file()


def test_upload_rejects_unsupported_suffix(client, upload_root):
    """只收文档工具能解析的格式。放进一个 .exe 只是给 read_document 添堵。"""
    r = _post(client, "setup.exe", b"MZ\x90\x00")
    assert r.status_code == 415
    assert "不支持的文件类型" in r.json()["detail"]
    # 拒绝要发生在落盘之前
    assert not upload_root.exists() or not list(upload_root.rglob("*"))


def test_upload_rejects_empty_file(client, upload_root):
    r = _post(client, "empty.txt", b"")
    assert r.status_code == 400


def test_upload_rejects_oversized(client, upload_root, monkeypatch):
    import backend.main as main_mod

    monkeypatch.setattr(main_mod, "MAX_UPLOAD_BYTES", 16)
    r = _post(client, "big.txt", b"x" * 100)
    assert r.status_code == 413
    assert "上限" in r.json()["detail"]


def test_upload_sanitizes_path_traversal_filename(client, upload_root):
    """客户端可以发 `../../evil.txt` 这种名字 —— 只能落在上传区内。"""
    r = _post(client, "../../evil.txt", b"pwned")

    assert r.status_code == 200
    from pathlib import Path

    target = Path(r.json()["path"]).resolve()
    assert upload_root.resolve() in target.parents
    # basename 被保留，但父目录就是上传根下的一层会话目录
    assert target.parent.parent == upload_root.resolve()


def test_upload_rejects_dotfile_suffix(client, upload_root):
    """`.env` 这种白名单外的名字（哪怕是穿越过来的）在落盘前就该拒。"""
    r = _post(client, "../../.env", b"SECRET=1")
    assert r.status_code == 415
    assert not upload_root.exists() or not list(upload_root.rglob("*.env"))


def test_upload_flags_fake_docx(client, upload_root):
    """改了后缀的假 docx：能存，但要在用户点发送前就说清楚读不了。"""
    r = _post(client, "假的.docx", b"this is plain text, not a zip")

    assert r.status_code == 200
    body = r.json()
    assert body["readable"] is False
    assert body["hint"]


def test_upload_groups_by_session(client, upload_root):
    _post(client, "a.txt", b"aaa", session_id="s-one")
    _post(client, "b.txt", b"bbb", session_id="s-two")

    dirs = sorted(p.name for p in upload_root.iterdir() if p.is_dir())
    assert dirs == ["s-one", "s-two"]


def test_upload_empty_session_goes_to_default(client, upload_root):
    """新建会话还没说过话时 session_id 是空串，不能因此落到 uploads 根上。"""
    r = _post(client, "a.txt", b"aaa", session_id="")
    assert r.status_code == 200
    assert r.json()["session_id"] == "default"


def test_upload_same_name_twice_keeps_both(client, upload_root):
    """同名文件不能互相覆盖 —— 历史消息里的路径要一直有效。"""
    first = _post(client, "报告.txt", b"v1").json()
    second = _post(client, "报告.txt", b"v2").json()

    assert first["path"] != second["path"]
    from pathlib import Path

    assert Path(first["path"]).read_bytes() == b"v1"
    assert Path(second["path"]).read_bytes() == b"v2"


@pytest.mark.asyncio
async def test_uploaded_file_is_readable_by_document_tool(client, upload_root):
    """端到端：上传的内容能被 read_document 原样读回来。

    这是整个功能的验收线 —— 接口返回 200 但 agent 读不到，
    用户看到的就是"传了文件却说没有文件"。
    """
    md = "# 周报\n\n本周完成了三件事。\n"
    body = _post(client, "周报.md", md.encode("utf-8")).json()

    from tools.document import ReadDocumentTool

    result = await ReadDocumentTool().execute(path=body["path"])
    assert result.success is True
    assert "本周完成了三件事" in result.data["markdown"]


# ===== 删会话要把上传物一起清掉 =====
#
# 上传文件按 session 分目录，唯一的引用就是会话里的消息。
# 会话删了，这些文件不会再被任何路径指到 —— 留着只是磁盘垃圾。

def test_delete_session_purges_upload_dir(client, upload_root, session_db_store):
    session_db_store.ensure_session("s-gone")
    session_db_store.append_message("s-gone", "user", "帮我看看这个")
    _post(client, "a.txt", b"hello", session_id="s-gone")
    folder = upload_root / "s-gone"
    assert folder.is_dir()

    r = client.delete("/api/sessions/s-gone")
    assert r.status_code == 200
    assert not folder.exists()


def test_delete_unknown_session_still_purges_orphan_uploads(
    client, upload_root, session_db_store
):
    """会话行不存在但目录还在（之前删除失败留下的孤儿）→ 顺手清掉。

    `delete_session` 靠"删掉了几条消息"判断存不存在，一条消息都没有的会话
    会返回 False（接口 404）—— 但它照样可能有上传文件：新开会话、传完文件、
    一个字没说就删。这时文件已经是孤儿了，不清就永远留着。
    """
    _post(client, "a.txt", b"hello", session_id="s-orphan")
    assert client.delete("/api/sessions/s-orphan").status_code == 404
    assert not (upload_root / "s-orphan").exists()


def test_purge_never_touches_shared_default_dir(upload_root):
    """空 / 异常 session_id 会被归一化成 default —— 那个目录是共享的，绝不能删。"""
    import backend.main as main_mod

    shared = upload_root / "default"
    shared.mkdir(parents=True)
    (shared / "keep.txt").write_bytes(b"x")

    main_mod._purge_upload_dir("")
    main_mod._purge_upload_dir("../")
    main_mod._purge_upload_dir("中文会话")

    assert (shared / "keep.txt").exists()


def test_purge_is_scoped_to_uploads_root(upload_root, tmp_path):
    """归一化后的名字也不能跑到上传区外面去。"""
    import backend.main as main_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_bytes(b"x")

    main_mod._purge_upload_dir("../outside")
    assert (outside / "precious.txt").exists()
