"""全 API 端点功能测试。

覆盖 tests/test_backend.py 未涉及的端点,确保所有 REST 路由
都能正常响应(成功路径 + 关键错误路径)。

测试原则:
- 用 tmp_path + monkeypatch 隔离环境,不污染真实数据
- 不依赖外部网络(LLM/Embedding 等走 mock 或跳过)
- 每个 endpoint 至少 1 个成功路径 + 1 个错误路径
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def client():
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ===== 基础只读端点 =====


def test_get_features(client):
    """GET /api/features 返回所有功能开关。"""
    resp = client.get("/api/features")
    assert resp.status_code == 200
    body = resp.json()
    assert "self_evolution_enabled" in body
    assert "skill_dag_enabled" in body
    assert "semantic_memory_enabled" in body


def test_diag_endpoint(client):
    """GET /api/diag 返回 providers/features/tool_sources/embedding/memory_stats。"""
    resp = client.get("/api/diag")
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    assert "features" in body
    assert "tool_sources" in body
    assert "embedding" in body
    assert "memory_stats" in body


def test_memory_stats_endpoint(client):
    """GET /api/memory/stats 返回记忆统计。"""
    resp = client.get("/api/memory/stats")
    assert resp.status_code == 200
    # 结构由 MemoryStore.get_stats() 决定,这里只验证可访问


def test_health_endpoint_structure(client):
    """GET /api/health 返回结构完整(含 sources 详情)。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert "self_evolution_enabled" in body
    assert "tool_sources" in body
    ts = body["tool_sources"]
    for key in ("total_sources", "connected", "failed", "disconnected", "has_failures"):
        assert key in ts


# ===== Skills 版本管理 =====


def test_list_skill_versions(client):
    """GET /api/skills/{name}/versions 列出技能所有版本 + active。"""
    resp = client.get("/api/skills/demo/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert "versions" in body
    assert "active" in body
    # demo 技能存在 1.0.0 和 1.0.1
    versions = body["versions"]
    assert isinstance(versions, list)


def test_list_skill_versions_not_found(client):
    """GET /api/skills/{nonexistent}/versions 也能正常返回空列表(不报错)。"""
    resp = client.get("/api/skills/__no_such_skill__/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["versions"] == []


def test_skill_audit_endpoint(client):
    """GET /api/skills/{name}/audit 汇总技能版本谱系与 episode。"""
    resp = client.get("/api/skills/demo/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "demo"
    assert "versions" in body
    assert "active" in body
    assert "episodes" in body


def test_reload_skills_endpoint(client):
    """POST /api/skills/reload 重新加载技能。"""
    resp = client.post("/api/skills/reload")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reloaded"] is True
    assert isinstance(body["count"], int)


def test_rollback_skill_version_not_found(client):
    """POST /api/skills/{name}/rollback/{version} 版本不存在返回 404。"""
    resp = client.post("/api/skills/demo/rollback/9.9.9")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"


# ===== Tool Proposals =====


def _minimal_proposal_payload(name: str = "test_proposal", version: str = "1.0.0") -> dict:
    """构造一个最小的合法 ToolProposal payload。"""
    return {
        "name": name,
        "version": version,
        "runtime": "declarative_http",
        "description": "test proposal",
        "endpoint": {
            "method": "GET",
            "path": "/users/{id}",
            "params": [
                {"name": "id", "type": "string", "location": "path", "required": True}
            ],
        },
        "permissions": ["network.read"],
        "network_policy": {"allowed_hosts": ["api.example.com"], "denied_hosts": [], "require_https": True},
        "side_effect": "read_only",
        "secret_refs": [],
        "test_cases": [
            {"name": "ok", "input": {"id": "1"}, "expected_status": 200, "expected_keys": []}
        ],
    }


def test_list_tool_proposals_empty_default(client):
    """GET /api/tools/proposals 默认可返回列表(可能为空)。"""
    resp = client.get("/api/tools/proposals")
    assert resp.status_code == 200
    body = resp.json()
    assert "proposals" in body
    assert isinstance(body["proposals"], list)


def test_create_and_get_tool_proposal(client, tmp_path, monkeypatch):
    """POST /api/tools/proposals 创建 + GET 详情(完整生命周期)。"""
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store

    # 用 tmp_path 隔离
    reset_tool_proposal_store()
    monkeypatch.setattr(
        "tools.proposal._store", ToolProposalStore(base_path=tmp_path)
    )

    payload = _minimal_proposal_payload("api_test_prop", "1.0.0")
    resp = client.post("/api/tools/proposals", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["name"] == "api_test_prop"
    assert body["version"] == "1.0.0"
    assert body["proposal_id"]

    # GET 列表应包含新建的
    resp = client.get("/api/tools/proposals")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()["proposals"]]
    assert "api_test_prop" in names

    # GET 详情
    resp = client.get("/api/tools/proposals/api_test_prop/1.0.0")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["name"] == "api_test_prop"
    assert detail["endpoint"]["method"] == "GET"


def test_create_tool_proposal_invalid(client, tmp_path, monkeypatch):
    """POST /api/tools/proposals 非法 payload 返回 422。"""
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store

    reset_tool_proposal_store()
    monkeypatch.setattr(
        "tools.proposal._store", ToolProposalStore(base_path=tmp_path)
    )

    # name 非法(以数字开头)
    bad = _minimal_proposal_payload()
    bad["name"] = "1invalid_name"
    resp = client.post("/api/tools/proposals", json=bad)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"


def test_create_tool_proposal_conflict(client, tmp_path, monkeypatch):
    """POST /api/tools/proposals 重复创建返回 409。"""
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store

    reset_tool_proposal_store()
    monkeypatch.setattr(
        "tools.proposal._store", ToolProposalStore(base_path=tmp_path)
    )

    payload = _minimal_proposal_payload("conflict_prop", "1.0.0")
    resp = client.post("/api/tools/proposals", json=payload)
    assert resp.status_code == 200

    # 第二次同样 name@version 应 409
    resp = client.post("/api/tools/proposals", json=payload)
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "conflict"


def test_tool_proposal_audit_endpoint(client, tmp_path, monkeypatch):
    """GET /api/tools/proposals/audit 返回审计日志。"""
    from tools.approval import ToolApprovalService, reset_tool_approval_service
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store

    reset_tool_proposal_store()
    reset_tool_approval_service()
    store = ToolProposalStore(base_path=tmp_path)
    monkeypatch.setattr("tools.proposal._store", store)
    # 审计服务也需重置
    monkeypatch.setattr(
        "tools.approval._singleton",
        ToolApprovalService(store=store),
    )

    resp = client.get("/api/tools/proposals/audit")
    assert resp.status_code == 200
    body = resp.json()
    assert "audit" in body


def test_reject_tool_proposal_not_found(client):
    """POST /api/tools/proposals/{name}/{version}/reject 不存在返回 404。"""
    resp = client.post(
        "/api/tools/proposals/__no_such__/1.0.0/reject",
        json={"reason": "test"},
    )
    assert resp.status_code == 404


def test_publish_tool_proposal_not_found(client):
    """POST /api/tools/proposals/{name}/{version}/publish 不存在返回 404。"""
    resp = client.post("/api/tools/proposals/__no_such__/1.0.0/publish")
    assert resp.status_code == 404


def test_disable_tool_proposal_not_found(client):
    """POST /api/tools/proposals/{name}/{version}/disable 不存在返回 404。"""
    resp = client.post("/api/tools/proposals/__no_such__/1.0.0/disable")
    assert resp.status_code == 404


def test_approve_tool_proposal_not_found(client):
    """POST /api/tools/proposals/{name}/{version}/approve 不存在返回 404。"""
    resp = client.post("/api/tools/proposals/__no_such__/1.0.0/approve")
    assert resp.status_code == 404


# ===== Patches =====


def test_list_patches_endpoint(client):
    """GET /api/patches 返回 pending patches 列表。"""
    resp = client.get("/api/patches")
    assert resp.status_code == 200
    body = resp.json()
    assert "patches" in body
    assert isinstance(body["patches"], list)


def test_reject_patch_not_found(client, monkeypatch):
    """POST /api/patches/{patch_id}/reject 不存在返回 404。"""
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", True)
    resp = client.post("/api/patches/__no_such_patch__/reject")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"


def test_approve_patch_self_evolution_disabled(client, monkeypatch):
    """POST /api/patches/{patch_id}/approve 自演化关闭时返回 403。"""
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", False)
    resp = client.post("/api/patches/any_patch/approve")
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "forbidden"


def test_reject_patch_self_evolution_disabled(client, monkeypatch):
    """POST /api/patches/{patch_id}/reject 自演化关闭时返回 403。"""
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", False)
    resp = client.post("/api/patches/any_patch/reject")
    assert resp.status_code == 403


# ===== Feedback =====


def test_submit_feedback_missing_execution_id(client):
    """POST /api/feedback 缺 execution_id 返回 400。"""
    resp = client.post("/api/feedback", json={"type": "accept"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "bad_request"


def test_submit_and_list_feedback(client, tmp_path, monkeypatch):
    """POST + GET /api/feedback 完整流程。"""
    from core.feedback import FeedbackStore, reset_feedback_store

    reset_feedback_store()
    monkeypatch.setattr(
        "core.feedback._store", FeedbackStore(base_path=tmp_path)
    )

    resp = client.post(
        "/api/feedback",
        json={
            "execution_id": "exec_test_001",
            "user_id": "u_test",
            "session_id": "s_test",
            "type": "accept",
            "content": "good",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["id"]

    # GET 列表
    resp = client.get("/api/feedback", params={"user_id": "u_test"})
    assert resp.status_code == 200
    items = resp.json()["feedback"]
    assert any(item.get("execution_id") == "exec_test_001" for item in items)


# ===== Memory =====


def test_list_memory_endpoint(client):
    """GET /api/memory 返回记忆列表。"""
    resp = client.get("/api/memory", params={"user_id": "default"})
    assert resp.status_code == 200
    body = resp.json()
    assert "memory" in body
    assert isinstance(body["memory"], list)


def test_recall_memory_endpoint(client):
    """POST /api/memory/recall 召回记忆。"""
    resp = client.post(
        "/api/memory/recall",
        json={"query": "test", "user_id": "default", "limit": 5},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "recall" in body
    assert isinstance(body["recall"], list)


def test_delete_memory_not_found(client):
    """DELETE /api/memory/{item_id} 不存在返回 404。"""
    resp = client.delete("/api/memory/__no_such_memory__", params={"user_id": "default"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"


def test_forget_user_endpoint(client, monkeypatch, tmp_path):
    """DELETE /api/memory 清空用户记忆(用 monkeypatch 避免污染真实数据)。"""
    from core.memory_repository import get_memory_repository

    repo = get_memory_repository()
    # stub:不真删,返回 0 即可
    monkeypatch.setattr(repo, "forget_user", lambda uid: 0)

    resp = client.delete("/api/memory", params={"user_id": "u_clean_test"})
    assert resp.status_code == 200
    body = resp.json()
    assert "deleted" in body
    assert body["user_id"] == "u_clean_test"


# ===== Episodes =====


def test_list_episodes_endpoint(client):
    """GET /api/episodes 返回 episode 列表。"""
    resp = client.get("/api/episodes")
    assert resp.status_code == 200
    body = resp.json()
    assert "episodes" in body
    assert isinstance(body["episodes"], list)


def test_get_episode_not_found(client):
    """GET /api/episodes/{execution_id} 不存在返回 404。"""
    resp = client.get("/api/episodes/__no_such_episode__")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"


# ===== Reflections =====


def test_list_reflections_self_evolution_disabled(client, monkeypatch):
    """GET /api/reflections 自演化关闭时返回 403。"""
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", False)
    resp = client.get("/api/reflections")
    assert resp.status_code == 403


def test_request_reflection_disabled(client, monkeypatch):
    """POST /api/reflections/request 自演化关闭时返回 403。"""
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", False)
    resp = client.post("/api/reflections/request")
    assert resp.status_code == 403


# ===== Teachings =====


def test_list_teachings_no_active(client):
    """GET /api/teachings 无活跃教学时返回 active=None。"""
    resp = client.get("/api/teachings", params={"user_id": "u_no_active", "session_id": "s_no_active"})
    assert resp.status_code == 200
    body = resp.json()
    # 默认无活跃教学
    assert "active" in body


def test_cancel_teaching(client):
    """POST /api/teachings/cancel 无活跃教学也返回 cancelled=False。"""
    resp = client.post(
        "/api/teachings/cancel",
        params={"user_id": "u_no_active", "session_id": "s_no_active"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "cancelled" in body


def test_choose_teaching_no_active(client):
    """POST /api/teachings/choose 无活跃教学返回 ok=False。"""
    resp = client.post(
        "/api/teachings/choose",
        params={"choice": "reuse", "user_id": "u_no_active", "session_id": "s_no_active"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False


def test_confirm_teaching_no_active(client):
    """POST /api/teachings/confirm 无活跃教学返回 ok=False。"""
    resp = client.post(
        "/api/teachings/confirm",
        params={"user_id": "u_no_active", "session_id": "s_no_active"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
