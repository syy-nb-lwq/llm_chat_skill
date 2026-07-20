"""M0-06 ToolHub 启动可观测性单元测试。

覆盖:
- ToolHub 内部状态机:registered / connecting / connected / connect_failed / disconnected
- 区分「未注册」「存在但连接失败」「已连接」
- /api/health 端点暴露 tool_sources / sources 字段
- connect_all 异常被捕获并标记为 failed,不影响其他源
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from tools.base import Tool, ToolResult, ToolSchema
from tools.hub import ToolHub, get_tool_hub, reset_tool_hub
from tools.sources.base import SourceType, ToolSource, ToolSourceBase


# ===== Fake sources for controllable scenarios =====


class FakeSource(ToolSourceBase):
    """可控制 connect 行为的假源。"""

    def __init__(
        self,
        source: ToolSource,
        *,
        connect_returns: bool = True,
        connect_raises: Optional[BaseException] = None,
        tools: Optional[List[Tool]] = None,
    ):
        super().__init__(source)
        self._connect_returns = connect_returns
        self._connect_raises = connect_raises
        self._tools_seed = tools or []
        self._connected = False

    async def connect(self) -> bool:
        if self._connect_raises is not None:
            raise self._connect_raises
        if self._connect_returns:
            # 只有真正连接成功时才置位
            self._connected = True
            for t in self._tools_seed:
                self._tools[t.name] = t
        return self._connect_returns

    async def disconnect(self):
        self._tools.clear()
        self._connected = False

    async def list_tools(self) -> List[Any]:
        return list(self._tools.values())

    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        t = self._tools.get(tool_name)
        if not isinstance(t, Tool):
            return ToolResult(success=False, error=f"no tool: {tool_name}")
        return t.execute(**params)

    def get_tool_schema(self, tool_name: str) -> Optional[ToolSchema]:
        t = self._tools.get(tool_name)
        return t.schema() if isinstance(t, Tool) else None


class _EchoTool(Tool):
    name = "echo"
    description = "echo back"

    def schema(self):
        return ToolSchema(name=self.name, description=self.description)

    def execute(self, **kwargs):
        return ToolResult(success=True, data={"echo": kwargs})


# ===== Fixtures =====


@pytest.fixture(autouse=True)
def clean_hub():
    """每个测试前后清理 ToolHub 单例。"""
    reset_tool_hub()
    yield
    reset_tool_hub()


@pytest.fixture
def fresh_hub():
    """绕过 get_tool_hub 的内置工具注入,直接拿一个新 ToolHub。"""
    return ToolHub()


# ===== 状态机测试 =====


class TestSourceStateMachine:
    """ToolHub 内部工具源状态机。"""

    @pytest.mark.asyncio
    async def test_registered_state_after_register(self, fresh_hub):
        """刚 register 完应是 registered 状态,不是 connected。"""
        source = FakeSource(ToolSource(name="s1", type=SourceType.PYTHON))
        fresh_hub.register_source(source)

        status = fresh_hub.get_source_status()
        assert status["s1"]["state"] == ToolHub.SOURCE_STATE_REGISTERED
        assert status["s1"]["connected"] is False
        assert status["s1"]["error"] is None
        assert status["s1"]["tool_count"] == 0

    @pytest.mark.asyncio
    async def test_connected_state_after_connect(self, fresh_hub):
        """connect() 返回 True 后应为 connected。"""
        source = FakeSource(
            ToolSource(name="s_ok", type=SourceType.PYTHON, enabled=True),
            connect_returns=True,
            tools=[_EchoTool()],
        )
        fresh_hub.register_source(source)
        ok = await fresh_hub.connect_source("s_ok")

        assert ok is True
        status = fresh_hub.get_source_status()
        assert status["s_ok"]["state"] == ToolHub.SOURCE_STATE_CONNECTED
        assert status["s_ok"]["connected"] is True
        assert status["s_ok"]["error"] is None
        assert status["s_ok"]["tool_count"] == 1
        assert status["s_ok"]["connected_at"] is not None

    @pytest.mark.asyncio
    async def test_connect_failed_state_when_returns_false(self, fresh_hub):
        """connect() 返回 False 时应记 connect_failed + 错误信息。"""
        source = FakeSource(
            ToolSource(name="s_false", type=SourceType.PYTHON, enabled=True),
            connect_returns=False,
        )
        fresh_hub.register_source(source)
        ok = await fresh_hub.connect_source("s_false")

        assert ok is False
        status = fresh_hub.get_source_status()
        assert status["s_false"]["state"] == ToolHub.SOURCE_STATE_FAILED
        assert status["s_false"]["connected"] is False
        assert "connect() returned False" in status["s_false"]["error"]

    @pytest.mark.asyncio
    async def test_connect_failed_state_when_raises(self, fresh_hub):
        """connect() 抛异常时应记 connect_failed + 异常类型 + 消息。"""
        source = FakeSource(
            ToolSource(name="s_raise", type=SourceType.PYTHON, enabled=True),
            connect_raises=ConnectionError("refused"),
        )
        fresh_hub.register_source(source)

        with pytest.raises(ConnectionError):
            await fresh_hub.connect_source("s_raise")

        status = fresh_hub.get_source_status()
        assert status["s_raise"]["state"] == ToolHub.SOURCE_STATE_FAILED
        assert status["s_raise"]["connected"] is False
        assert "ConnectionError" in status["s_raise"]["error"]
        assert "refused" in status["s_raise"]["error"]

    @pytest.mark.asyncio
    async def test_disconnected_state(self, fresh_hub):
        """disconnect 后应记 disconnected + 清掉 connected_at。"""
        source = FakeSource(
            ToolSource(name="s_dc", type=SourceType.PYTHON, enabled=True),
            connect_returns=True,
            tools=[_EchoTool()],
        )
        fresh_hub.register_source(source)
        await fresh_hub.connect_source("s_dc")

        await fresh_hub.disconnect_source("s_dc")
        status = fresh_hub.get_source_status()
        assert status["s_dc"]["state"] == ToolHub.SOURCE_STATE_DISCONNECTED
        assert status["s_dc"]["connected"] is False
        assert status["s_dc"]["connected_at"] is None

    @pytest.mark.asyncio
    async def test_connect_all_skips_disabled_and_records_failures(self, fresh_hub):
        """connect_all 应跳过禁用源,失败源不影响其它源。"""
        ok_source = FakeSource(
            ToolSource(name="ok", type=SourceType.PYTHON, enabled=True),
            connect_returns=True,
            tools=[_EchoTool()],
        )
        bad_source = FakeSource(
            ToolSource(name="bad", type=SourceType.PYTHON, enabled=True),
            connect_raises=RuntimeError("boom"),
        )
        disabled_source = FakeSource(
            ToolSource(name="off", type=SourceType.PYTHON, enabled=False),
        )

        fresh_hub.register_source(ok_source)
        fresh_hub.register_source(bad_source)
        fresh_hub.register_source(disabled_source)

        await fresh_hub.connect_all()

        status = fresh_hub.get_source_status()
        # ok:已连接
        assert status["ok"]["state"] == ToolHub.SOURCE_STATE_CONNECTED
        # bad:连接失败
        assert status["bad"]["state"] == ToolHub.SOURCE_STATE_FAILED
        assert "RuntimeError" in status["bad"]["error"]
        # disabled:未尝试,仍为 registered
        assert status["off"]["state"] == ToolHub.SOURCE_STATE_REGISTERED


class TestHealthSummary:
    """health_summary() 聚合报告。"""

    @pytest.mark.asyncio
    async def test_summary_counts_each_state(self, fresh_hub):
        """健康汇总应正确统计各状态数量。"""
        fresh_hub.register_source(FakeSource(
            ToolSource(name="ok1", type=SourceType.PYTHON, enabled=True),
            connect_returns=True, tools=[_EchoTool()],
        ))
        fresh_hub.register_source(FakeSource(
            ToolSource(name="ok2", type=SourceType.PYTHON, enabled=True),
            connect_returns=True, tools=[_EchoTool()],
        ))
        fresh_hub.register_source(FakeSource(
            ToolSource(name="bad1", type=SourceType.PYTHON, enabled=True),
            connect_returns=False,
        ))
        fresh_hub.register_source(FakeSource(
            ToolSource(name="off", type=SourceType.PYTHON, enabled=False),
        ))

        await fresh_hub.connect_all()

        summary = fresh_hub.health_summary()
        assert summary["total_sources"] == 4
        assert summary["connected"] == 2
        assert summary["failed"] == 1
        assert summary["disconnected"] == 0
        assert summary["has_failures"] is True
        # 完整 sources 字典应包含每个源
        assert set(summary["sources"].keys()) == {"ok1", "ok2", "bad1", "off"}

    @pytest.mark.asyncio
    async def test_summary_no_failures(self, fresh_hub):
        """全连接成功时 has_failures 应为 False。"""
        fresh_hub.register_source(FakeSource(
            ToolSource(name="ok", type=SourceType.PYTHON, enabled=True),
            connect_returns=True, tools=[_EchoTool()],
        ))
        await fresh_hub.connect_all()

        summary = fresh_hub.health_summary()
        assert summary["has_failures"] is False
        assert summary["failed"] == 0
        assert summary["connected"] == 1

    def test_summary_empty_hub(self, fresh_hub):
        """空 hub 的 summary 应全 0。"""
        summary = fresh_hub.health_summary()
        assert summary["total_sources"] == 0
        assert summary["connected"] == 0
        assert summary["failed"] == 0
        assert summary["disconnected"] == 0
        assert summary["has_failures"] is False
        assert summary["sources"] == {}


# ===== /api/health 端点测试 =====


class TestHealthEndpoint:
    """/api/health 必须暴露工具源状态。"""

    @pytest.mark.asyncio
    async def test_health_endpoint_exposes_tool_sources(self, monkeypatch):
        """健康检查应包含 tool_sources 聚合字段和 sources 详情。"""
        # 替换 ToolHub 单例为可控版本
        controlled = ToolHub()
        ok_source = FakeSource(
            ToolSource(name="ok", type=SourceType.PYTHON, enabled=True),
            connect_returns=True, tools=[_EchoTool()],
        )
        bad_source = FakeSource(
            ToolSource(name="bad", type=SourceType.PYTHON, enabled=True),
            connect_returns=False,
        )
        controlled.register_source(ok_source)
        controlled.register_source(bad_source)

        # 真实跑一次 connect_all 让两个源分别进入 connected / failed 状态
        await controlled.connect_all()

        import tools.hub as hub_mod
        monkeypatch.setattr(hub_mod, "_hub", controlled)
        monkeypatch.setattr(hub_mod, "_builtin_tools_added", True)

        # 用一个最小 FastAPI app 避免触发完整 startup(config.validate 会要求 OPENAI_API_KEY)
        from fastapi import FastAPI
        from backend.main import health as health_endpoint

        app = FastAPI()
        app.get("/api/health")(health_endpoint)

        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()

        # 顶层字段
        assert body["status"] in ("ok", "degraded")
        assert "self_evolution_enabled" in body
        assert "tool_sources" in body
        assert "sources" in body

        # tool_sources 聚合
        ts = body["tool_sources"]
        assert ts["total_sources"] == 2
        assert ts["connected"] == 1
        assert ts["failed"] == 1
        assert ts["has_failures"] is True

        # sources 详情
        sources = body["sources"]
        assert "ok" in sources and "bad" in sources
        assert sources["ok"]["state"] == ToolHub.SOURCE_STATE_CONNECTED
        assert sources["ok"]["connected"] is True
        assert sources["bad"]["state"] == ToolHub.SOURCE_STATE_FAILED
        assert sources["bad"]["error"]

        # 因 failed > 0,顶层 status 应该是 degraded
        assert body["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_health_endpoint_all_connected_is_ok(self, monkeypatch):
        """全部连接成功时,顶层 status 应为 ok。"""
        controlled = ToolHub()
        controlled.register_source(FakeSource(
            ToolSource(name="ok", type=SourceType.PYTHON, enabled=True),
            connect_returns=True, tools=[_EchoTool()],
        ))
        await controlled.connect_all()

        import tools.hub as hub_mod
        monkeypatch.setattr(hub_mod, "_hub", controlled)
        monkeypatch.setattr(hub_mod, "_builtin_tools_added", True)

        from fastapi import FastAPI
        from backend.main import health as health_endpoint

        app = FastAPI()
        app.get("/api/health")(health_endpoint)

        resp = TestClient(app).get("/api/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["tool_sources"]["has_failures"] is False

    def test_health_endpoint_no_sources_is_ok(self, monkeypatch):
        """未注册任何源时应返回 ok(不影响基本健康)。"""
        controlled = ToolHub()

        import tools.hub as hub_mod
        monkeypatch.setattr(hub_mod, "_hub", controlled)
        monkeypatch.setattr(hub_mod, "_builtin_tools_added", True)

        from fastapi import FastAPI
        from backend.main import health as health_endpoint

        app = FastAPI()
        app.get("/api/health")(health_endpoint)

        resp = TestClient(app).get("/api/health")
        body = resp.json()
        assert body["status"] == "ok"
        assert body["tool_sources"]["total_sources"] == 0
        assert body["sources"] == {}

    def test_health_endpoint_hub_error_degrades_gracefully(self, monkeypatch):
        """ToolHub 自身异常时,健康检查不应 500,而是降级返回。"""
        from tools import hub as hub_mod

        def boom():
            raise RuntimeError("hub 坏了")

        monkeypatch.setattr(hub_mod, "get_tool_hub", boom)

        from fastapi import FastAPI
        from backend.main import health as health_endpoint

        app = FastAPI()
        app.get("/api/health")(health_endpoint)

        resp = TestClient(app).get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        # 仍然 ok,因为我们没有失败源(只是没法查)
        assert body["status"] == "ok"
        assert body["tool_sources"]["total_sources"] == 0
