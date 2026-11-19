"""用户画像面板的后端接口测试(A-09 / U-04)。

本地单用户：没有令牌，session_id 从 query 参数取。
"""
import pytest


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


def _add_profile(content, mtype="preference"):
    from core.semantic_memory import get_semantic_memory

    store = get_semantic_memory()
    return store.memory_db.add(
        type=mtype,
        content=content,
        confidence=0.9,
        importance=0.8
    )


# ============================================================
# 1. 访问
# ============================================================

def test_profile_is_open(client_with_profile):
    """本地单用户：画像接口无需令牌。"""
    resp = client_with_profile.get("/api/memory/profile")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "local"


# ============================================================
# 2. 列出画像
# ============================================================

def test_profile_lists_entries(client_with_profile):
    _add_profile("喜欢用要点式回答")
    _add_profile("回答要简洁", mtype="constraint")

    resp = client_with_profile.get("/api/memory/profile?session_id=s1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["user_id"] == "local"
    labels = {e["label"] for e in data["entries"]}
    assert labels == {"偏好", "雷区"}


def test_profile_includes_injected_block(client_with_profile):
    """要能看到"此刻正在注入"的那段 —— 用户才知道什么在影响回答。"""
    _add_profile("喜欢用要点式回答")

    resp = client_with_profile.get("/api/memory/profile?session_id=s1")
    data = resp.json()
    assert "要点式" in data["injected_block"]
    assert data["injected_chars"] > 0


def test_profile_empty_is_not_error(client_with_profile):
    resp = client_with_profile.get("/api/memory/profile?session_id=s1")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["injected_block"] == ""


# ============================================================
# 3. 隔离 —— 最要命的部分
# ============================================================







# ============================================================
# 4. 忘掉
# ============================================================

def test_forget_one_entry(client_with_profile):
    _add_profile("喜欢用要点式回答")
    _add_profile("不要用 emoji", mtype="constraint")

    from core.semantic_memory import get_semantic_memory

    target = get_semantic_memory().get_profile()[0].id

    resp = client_with_profile.delete(
        f"/api/memory/profile/{target}",
        
    )
    assert resp.status_code == 200
    assert resp.json()["forgotten"] == target

    remaining = get_semantic_memory().get_profile()
    assert len(remaining) == 1
    # 而且真的不再被注入
    assert target not in remaining[0].id


def test_forget_removes_from_injection(client_with_profile):
    """删掉的条目必须立刻不再出现在注入块里 —— 这才是"忘掉"。"""
    _add_profile("喜欢用要点式回答")

    from core.semantic_memory import get_semantic_memory

    target = get_semantic_memory().get_profile()[0].id
    client_with_profile.delete(
        f"/api/memory/profile/{target}",
        
    )

    resp = client_with_profile.get(
        "/api/memory/profile"
    )
    assert "要点式" not in resp.json()["injected_block"]


def test_forget_unknown_entry_404(client_with_profile):
    resp = client_with_profile.delete(
        "/api/memory/profile/nonexistent",
        
    )
    assert resp.status_code == 404


def test_forget_all(client_with_profile):
    _add_profile("偏好一")
    _add_profile("偏好二", mtype="habit")

    from core.semantic_memory import get_semantic_memory

    resp = client_with_profile.request("DELETE", "/api/memory/profile")
    assert resp.status_code == 200
    assert resp.json()["forgotten"] == 2

    assert get_semantic_memory().get_profile() == []


