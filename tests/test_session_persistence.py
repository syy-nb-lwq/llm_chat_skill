"""「刷新/重启后历史对话没了」的根因与回归测试。

## 实机现象

用户刷新页面 → 历史对话消失。

## 根因

**身份丢了** —— 当时靠 HMAC 用户令牌证明"你是同一个人"，密钥不稳定就等于
每次都是新用户。本地单用户化后，令牌体系已移除：会话身份由**服务端签发的
session_id** 承担，前端把它存在 localStorage，重连时带回即可续上。

会话内容一直是全量保留的。丢的从来不是数据。
"""



# ============================================================
# 1. 令牌密钥必须跨重启稳定
# ============================================================









# ============================================================
# 2. 存储自愈
# ============================================================

def test_session_store_heals_after_dir_deleted(tmp_path):
    """`memory/` 被删掉后,读会话不能 500。"""
    import shutil

    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "mem" / "sessions.db")
    store.append_message("s1", "user", "hi")

    # 删掉整个目录(模拟外部清理)
    shutil.rmtree(tmp_path / "mem", ignore_errors=True)

    # 读不能抛 —— 目录没了要能自愈(重建目录 + 补建表)
    store.get_messages("s1")

    # 文件真被删掉了 → 数据也该没了(store 会重建一个空库)。
    # 注意:store 现在**复用连接**,Windows 上文件被我们持着句柄,
    # 外部删除会被系统挡掉 —— 这时文件还在,数据也还在,不是 bug。
    db_file = tmp_path / "mem" / "sessions.db"
    if not db_file.exists():
        assert store.session_exists("s1") is False
        assert store.get_messages("s1") == []


def test_session_store_rebuilds_after_db_file_removed(tmp_path):
    """DB 文件被删掉后,store 会重建 —— 读到的是空的,不是旧数据。

    这里先 ``close()`` 再删,是为了让删除在所有平台上都能真正发生
    (Windows 上文件被连接持着句柄时删不掉)。等价于"进程重启后
    目录被清理掉了"的真实场景。
    """
    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "mem" / "sessions.db")
    store.append_message("s1", "user", "hi")
    assert store.get_messages("s1")  # 先确认写进去了

    store.close()
    (tmp_path / "mem" / "sessions.db").unlink()

    # 同一个 store 实例:文件没了 → 自动重建 → 读到的是空的
    assert store.session_exists("s1") is False
    assert store.get_messages("s1") == []

    # 重建之后还能正常写
    store.append_message("s2", "user", "重建后写入")
    assert len(store.get_messages("s2")) == 1


def test_session_store_can_write_after_dir_deleted(tmp_path):
    import shutil

    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "mem" / "sessions.db")
    shutil.rmtree(tmp_path / "mem", ignore_errors=True)

    store.append_message("s1", "user", "恢复后写入")
    msgs = store.get_messages("s1")
    assert len(msgs) == 1
    assert msgs[0].content == "恢复后写入"




# ============================================================
# 3. 端到端:会话能按令牌取回来
# ============================================================

def test_history_roundtrip_through_api(tmp_path, monkeypatch):
    """存消息 → 用 session_id 取历史。这是前端刷新后做的事。"""
    from core import session_memory as sm
    from core.session_memory import SessionMemoryStore

    store = SessionMemoryStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(sm, "get_session_memory_store", lambda: store)

    store.append_message("s-e2e", "user", "记住数字 42")
    store.append_message("s-e2e", "assistant", "好的")

    from backend.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        resp = c.get("/api/session/messages?session_id=s-e2e")
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "s-e2e"
    assert data["count"] == 2
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
