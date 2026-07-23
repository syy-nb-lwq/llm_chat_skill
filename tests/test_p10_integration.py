"""P10 集成补全测试:Manager 动态工具列表 + 工具提案 REST 路由 + Heartbeat 配置。

覆盖本轮补全的 4 处缺口:
1. Manager _build_plan_schema() / system_prompt() 从 ToolHub 动态读取工具名
2. /api/tools/proposals 系列 REST 端点
3. MCP 源注册(startup 集成在 backend 中,这里验证 config 与 import)
4. Heartbeat config 字段 + 启动/停止
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===== 1. Manager 动态工具列表 =====


def test_manager_builtin_tools_fallback():
    """ToolHub 为空时,回退到内置工具列表。"""
    from tools.hub import reset_tool_hub
    reset_tool_hub()
    from agents.manager import ManagerAgent
    mgr = ManagerAgent()
    names = mgr._get_available_tool_names()
    assert "weather_query" in names
    assert "web_search" in names


def test_manager_dynamic_schema_uses_hub_tools():
    """ToolHub 注册了新工具后,schema enum 应包含该工具名。"""
    from tools.hub import get_tool_hub, reset_tool_hub
    from tools.base import Tool, ToolResult, ToolSchema
    reset_tool_hub()
    hub = get_tool_hub()

    class FakeTool(Tool):
        name = "custom_api_lookup"
        description = "fake tool"
        def schema(self):
            return ToolSchema(name=self.name, description=self.description, params=[])
        async def execute(self, **kwargs):
            return ToolResult(success=True, data={})

    hub.register_python_tool(FakeTool())
    from agents.manager import ManagerAgent
    mgr = ManagerAgent()
    schema = mgr._build_plan_schema()
    enum_vals = schema["properties"]["tool_tasks"]["items"]["properties"]["type"]["enum"]
    assert "custom_api_lookup" in enum_vals
    reset_tool_hub()


def test_manager_system_prompt_lists_dynamic_tools():
    """system_prompt 中应包含 ToolHub 中的工具名。"""
    from tools.hub import get_tool_hub, reset_tool_hub
    from tools.base import Tool, ToolResult, ToolSchema
    reset_tool_hub()
    hub = get_tool_hub()

    class FakeTool(Tool):
        name = "my_dynamic_tool"
        description = "dynamic"
        def schema(self):
            return ToolSchema(name=self.name, description=self.description, params=[])
        async def execute(self, **kwargs):
            return ToolResult(success=True, data={})

    hub.register_python_tool(FakeTool())
    from agents.manager import ManagerAgent
    mgr = ManagerAgent()
    prompt = mgr.system_prompt()
    assert "my_dynamic_tool" in prompt
    reset_tool_hub()


# ===== 2. 工具提案 REST 路由 =====


@pytest.fixture
def client():
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def _make_proposal_payload(name="test_api_lookup", version="1.0.0"):
    return {
        "name": name,
        "version": version,
        "runtime": "declarative_http",
        "description": "test proposal",
        "endpoint": {
            "method": "GET",
            "path": "/users/{username}",
            "params": [{"name": "username", "type": "string", "location": "path", "required": True}],
        },
        "permissions": ["network.read"],
        "network_policy": {"allowed_hosts": ["api.example.com"], "require_https": True},
        "side_effect": "read_only",
        "secret_refs": [],
        "test_cases": [],
    }


def test_list_tool_proposals_empty(client):
    resp = client.get("/api/tools/proposals")
    assert resp.status_code == 200
    assert "proposals" in resp.json()


def test_create_and_get_tool_proposal(client, tmp_path, monkeypatch):
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store
    reset_tool_proposal_store()
    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr("tools.proposal._store", store)

    payload = _make_proposal_payload()
    resp = client.post("/api/tools/proposals", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] is True
    assert data["name"] == "test_api_lookup"

    # GET detail
    resp2 = client.get("/api/tools/proposals/test_api_lookup/1.0.0")
    assert resp2.status_code == 200
    assert resp2.json()["name"] == "test_api_lookup"

    # LIST
    resp3 = client.get("/api/tools/proposals")
    assert resp3.status_code == 200
    names = [p["name"] for p in resp3.json()["proposals"]]
    assert "test_api_lookup" in names


def test_create_proposal_validation_error(client, tmp_path, monkeypatch):
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store
    reset_tool_proposal_store()
    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr("tools.proposal._store", store)

    bad_payload = _make_proposal_payload()
    bad_payload["name"] = "123_invalid"  # 非法 name
    resp = client.post("/api/tools/proposals", json=bad_payload)
    assert resp.status_code == 422


def test_create_proposal_duplicate(client, tmp_path, monkeypatch):
    from tools.proposal import ToolProposalStore, reset_tool_proposal_store
    reset_tool_proposal_store()
    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr("tools.proposal._store", store)

    payload = _make_proposal_payload()
    client.post("/api/tools/proposals", json=payload)
    resp = client.post("/api/tools/proposals", json=payload)
    assert resp.status_code == 409


def test_get_proposal_not_found(client):
    resp = client.get("/api/tools/proposals/__nope__/9.9.9")
    assert resp.status_code == 404


def test_proposal_audit_endpoint(client):
    resp = client.get("/api/tools/proposals/audit")
    assert resp.status_code == 200
    assert "audit" in resp.json()


def test_disable_nonexistent_proposal(client):
    resp = client.post("/api/tools/proposals/__nope__/1.0.0/disable")
    assert resp.status_code == 404


# ===== 3. Heartbeat 配置 =====


def test_heartbeat_config_fields():
    """Heartbeat config 字段已存在且有默认值。"""
    from infra.config import config
    assert hasattr(config, "heartbeat_enabled")
    assert hasattr(config, "heartbeat_interval_seconds")
    assert hasattr(config, "heartbeat_path")
    assert config.heartbeat_interval_seconds == 1800


def test_diag_includes_heartbeat_and_mcp():
    """/api/diag 应暴露 heartbeat_enabled 和 mcp_enabled。"""
    from backend.main import app
    with TestClient(app) as c:
        resp = c.get("/api/diag")
        assert resp.status_code == 200
        features = resp.json()["features"]
        assert "heartbeat_enabled" in features
        assert "mcp_enabled" in features


# ===== 4. Manager 不再硬编码 PLAN_SCHEMA 类属性 =====


def test_manager_no_plan_schema_class_attr():
    """PLAN_SCHEMA 不再是类属性(改为 _build_plan_schema 方法)。"""
    from agents.manager import ManagerAgent
    assert not hasattr(ManagerAgent, "PLAN_SCHEMA")
