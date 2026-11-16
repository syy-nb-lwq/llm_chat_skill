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
    store.ensure_session("s1", "u1")
    store.append_message("s1", "u1", "user", "你好")
    r = client.get("/api/sessions")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["message_count"] == 1


def test_list_sessions_filter_by_user(client, store):
    store.ensure_session("s1", "u1")
    store.ensure_session("s2", "u2")
    r = client.get("/api/sessions", params={"user_id": "u1"})
    assert r.status_code == 200
    assert len(r.json()["sessions"]) == 1


def test_get_messages(client, store):
    store.ensure_session("s1", "u1")
    store.append_message("s1", "u1", "user", "第一条")
    store.append_message("s1", "u1", "assistant", "第二条")
    r = client.get("/api/sessions/s1/messages")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["content"] for m in msgs] == ["第一条", "第二条"]
    assert msgs[0]["seq"] == 1


def test_get_messages_user_filter(client, store):
    store.ensure_session("s1", "u1")
    store.append_message("s1", "u1", "user", "u1 的")
    store.append_message("s1", "u2", "user", "u2 的")
    r = client.get("/api/sessions/s1/messages", params={"user_id": "u1"})
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == "u1 的"


def test_get_messages_unknown_session(client, store):
    r = client.get("/api/sessions/s-nope/messages")
    assert r.status_code == 404


def test_delete_session(client, store):
    store.ensure_session("s1", "u1")
    store.append_message("s1", "u1", "user", "内容")
    r = client.delete("/api/sessions/s1")
    assert r.status_code == 200
    assert store.count_messages("s1") == 0
    assert store.session_exists("s1") is False


def test_delete_unknown_session(client, store):
    r = client.delete("/api/sessions/s-nope")
    assert r.status_code == 404
