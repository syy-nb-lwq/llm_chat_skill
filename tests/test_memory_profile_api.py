"""用户画像面板的后端接口测试(A-09 / U-04)。

重点在**隔离**:这是用户级接口,最要命的 bug 是"用户 A 看到了/删掉了
用户 B 的画像"。所以每个用例都带着两个用户跑。
"""
import pytest

from infra.auth import issue_session_token, issue_user_token


@pytest.fixture
def client_with_profile(tmp_path, monkeypatch):
    """起一个后端,并清空记忆库。

    **必须清空**:conftest 用的是 session 级共享 DB(SQLite 文件),
    重置单例只是放弃 Python 引用,数据在文件里照旧存在。
    不清的话上一个测试的画像会漏到下一个。
    """
    from core import memory_db as mdb
    from core import semantic_memory as sm

    mdb.get_memory_db().clear_all()
    sm.reset_semantic_memory()

    from backend.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c

    mdb.get_memory_db().clear_all()
    sm.reset_semantic_memory()


def _add_profile(user_id, content, mtype="preference"):
    from core.semantic_memory import get_semantic_memory

    store = get_semantic_memory()
    return store.memory_db.add(
        type=mtype,
        content=content,
        user_id=user_id,
        confidence=0.9,
        importance=0.8,
    )


# ============================================================
# 1. 认证
# ============================================================

def test_profile_requires_token(client_with_profile):
    resp = client_with_profile.get("/api/memory/profile")
    assert resp.status_code == 401


def test_profile_rejects_bad_token(client_with_profile):
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": "Bearer garbage"}
    )
    assert resp.status_code == 401


def test_profile_accepts_session_token(client_with_profile):
    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200


def test_profile_accepts_user_token(client_with_profile):
    """长期用户令牌也要能用 —— 它才是"跨重启还是同一个人"的凭证。"""
    token = issue_user_token("u1")
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "u1"


# ============================================================
# 2. 列出画像
# ============================================================

def test_profile_lists_entries(client_with_profile):
    _add_profile("u1", "喜欢用要点式回答")
    _add_profile("u1", "回答要简洁", mtype="constraint")

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["user_id"] == "u1"
    labels = {e["label"] for e in data["entries"]}
    assert labels == {"偏好", "雷区"}


def test_profile_includes_injected_block(client_with_profile):
    """要能看到"此刻正在注入"的那段 —— 用户才知道什么在影响回答。"""
    _add_profile("u1", "喜欢用要点式回答")

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    data = resp.json()
    assert "要点式" in data["injected_block"]
    assert data["injected_chars"] > 0


def test_profile_empty_is_not_error(client_with_profile):
    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["injected_block"] == ""


# ============================================================
# 3. 隔离 —— 最要命的部分
# ============================================================

def test_user_cannot_see_other_users_profile(client_with_profile):
    _add_profile("u2", "这是另一个人的秘密偏好")

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert "另一个人的秘密" not in resp.text


def test_user_cannot_delete_other_users_entry(client_with_profile):
    """拿别人的 entry_id 来删 —— 必须 404。"""
    _add_profile("u2", "这是另一个人的秘密偏好")
    from core.semantic_memory import get_semantic_memory

    other_id = get_semantic_memory().get_profile("u2")[0].id

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.delete(
        f"/api/memory/profile/{other_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404

    # 对方的条目还在
    assert len(get_semantic_memory().get_profile("u2")) == 1


def test_user_id_cannot_be_forged_via_query_param(client_with_profile):
    """**user_id 只能从令牌来** —— 否则传个 query 就越权了。

    这是这个接口最容易写错的地方:
    `GET /api/memory/profile?user_id=u2` 绝不能返回 u2 的数据。
    """
    _add_profile("u2", "这是另一个人的秘密偏好")

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.get(
        "/api/memory/profile?user_id=u2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "u1"
    assert resp.json()["count"] == 0


# ============================================================
# 4. 忘掉
# ============================================================

def test_forget_one_entry(client_with_profile):
    _add_profile("u1", "喜欢用要点式回答")
    _add_profile("u1", "不要用 emoji", mtype="constraint")

    from core.semantic_memory import get_semantic_memory

    target = get_semantic_memory().get_profile("u1")[0].id

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.delete(
        f"/api/memory/profile/{target}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["forgotten"] == target

    remaining = get_semantic_memory().get_profile("u1")
    assert len(remaining) == 1
    # 而且真的不再被注入
    assert target not in remaining[0].id


def test_forget_removes_from_injection(client_with_profile):
    """删掉的条目必须立刻不再出现在注入块里 —— 这才是"忘掉"。"""
    _add_profile("u1", "喜欢用要点式回答")

    from core.semantic_memory import get_semantic_memory

    target = get_semantic_memory().get_profile("u1")[0].id
    token = issue_session_token("u1", "s1", "c1")
    client_with_profile.delete(
        f"/api/memory/profile/{target}",
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client_with_profile.get(
        "/api/memory/profile", headers={"Authorization": f"Bearer {token}"}
    )
    assert "要点式" not in resp.json()["injected_block"]


def test_forget_unknown_entry_404(client_with_profile):
    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.delete(
        "/api/memory/profile/nonexistent",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_forget_all(client_with_profile):
    _add_profile("u1", "偏好一")
    _add_profile("u1", "偏好二", mtype="habit")
    _add_profile("u2", "别人的")

    from core.semantic_memory import get_semantic_memory

    token = issue_session_token("u1", "s1", "c1")
    resp = client_with_profile.request(
        "DELETE", "/api/memory/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["forgotten"] == 2

    assert get_semantic_memory().get_profile("u1") == []
    # 别人的不受影响
    assert len(get_semantic_memory().get_profile("u2")) == 1


def test_forget_all_requires_token(client_with_profile):
    resp = client_with_profile.request("DELETE", "/api/memory/profile")
    assert resp.status_code == 401
