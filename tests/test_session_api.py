"""会话内容 REST API 测试。

覆盖:
  - GET /api/sessions 列出
  - GET /api/sessions/{id}/messages 取消息
  - DELETE /api/sessions/{id} 删除
  - 隔离:传 user_id 时只返回该用户的消息
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from backend.main import app

    return TestClient(app)


@pytest.fixture
def store(session_db_store):
    session_db_store.clear_all()
    return session_db_store


def test_list_sessions_empty(client, store):
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json()["sessions"] == []


def test_list_sessions_with_data(client, store):
    store.ensure_session("s1")
    store.append_message("s1", "user", "你好")
    r = client.get("/api/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["message_count"] == 1


def test_list_sessions(client, store):
    store.ensure_session("s1")
    store.ensure_session("s2")
    r = client.get("/api/sessions")
    assert r.status_code == 200
    assert len(r.json()["sessions"]) == 2


def test_get_messages(client, store):
    store.ensure_session("s1")
    store.append_message("s1", "user", "第一条")
    store.append_message("s1", "assistant", "第二条")
    r = client.get("/api/sessions/s1/messages")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["content"] for m in msgs] == ["第一条", "第二条"]
    assert msgs[0]["seq"] == 1


def test_get_messages_all_of_session(client, store):
    store.ensure_session("s1")
    store.append_message("s1", "user", "第一条")
    store.append_message("s1", "user", "第二条")
    r = client.get("/api/sessions/s1/messages")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 2


def test_get_messages_unknown_session(client, store):
    r = client.get("/api/sessions/s-nope/messages")
    assert r.status_code == 404


def test_delete_session(client, store):
    store.ensure_session("s1")
    store.append_message("s1", "user", "内容")
    r = client.delete("/api/sessions/s1")
    assert r.status_code == 200
    assert store.count_messages("s1") == 0
    assert store.session_exists("s1") is False


def test_delete_unknown_session(client, store):
    r = client.delete("/api/sessions/s-nope")
    assert r.status_code == 404


# ===== 列表标题（会话列表靠它认人） =====
#
# 没有 title 列，标题是从首条用户消息现算的（见
# `SessionMemoryStore.list_sessions`）。下面这几条钉的是"算规则"本身。


def test_session_title_comes_from_first_user_message(client, store):
    store.ensure_session("s1")
    store.append_message("s1", "system", "系统提示不算标题")
    store.append_message("s1", "user", "帮我写个爬虫")
    store.append_message("s1", "user", "再改一下频率")
    r = client.get("/api/sessions")
    assert r.json()["sessions"][0]["title"] == "帮我写个爬虫"


def test_session_title_is_flattened_to_one_line(client, store):
    """用户经常整段日志/代码贴进来，标题必须压成一行，否则侧栏被撑开。"""
    store.ensure_session("s1")
    store.append_message("s1", "user", "第一行\n\n  第二行\t第三行")
    r = client.get("/api/sessions")
    assert r.json()["sessions"][0]["title"] == "第一行 第二行 第三行"


def test_session_title_is_truncated(client, store):
    store.ensure_session("s1")
    store.append_message("s1", "user", "长" * 100)
    title = client.get("/api/sessions").json()["sessions"][0]["title"]
    assert len(title) == 36
    assert title.endswith("…")


def test_session_title_follows_reset(client, store):
    """reset 之后标题要换成新话题 —— 不然列表指着一段已经忘掉的内容。"""
    store.ensure_session("s1")
    store.append_message("s1", "user", "旧话题")
    store.mark_reset("s1")
    store.append_message("s1", "user", "新话题")
    assert client.get("/api/sessions").json()["sessions"][0]["title"] == "新话题"


def test_session_title_empty_for_silent_session(client, store):
    """建了没说话的会话 title 是空串，由前端显示成「新对话」。"""
    store.ensure_session("s1")
    assert client.get("/api/sessions").json()["sessions"][0]["title"] == ""
