"""Tool system tests."""
import pytest

from tools.base import Tool, ToolParam, ToolRegistry, ToolResult, ToolSchema, get_tool_registry


def test_registry_builtins_registered():
    registry = get_tool_registry()
    names = registry.names()
    assert "weather_query" in names
    assert "web_search" in names


def test_tool_schemas_have_required_fields():
    registry = get_tool_registry()
    for schema in registry.schemas():
        assert schema["name"]
        assert schema["description"]
        assert isinstance(schema["params"], list)
        assert len(schema["params"]) >= 1


def test_validate_params_missing_required():
    registry = get_tool_registry()
    ok, err = registry.validate_params("weather_query", {})
    assert not ok
    assert "city" in err


def test_validate_params_ok():
    registry = get_tool_registry()
    ok, err = registry.validate_params("weather_query", {"city": "厦门"})
    assert ok, err


def test_validate_unknown_tool():
    registry = get_tool_registry()
    ok, err = registry.validate_params("nope_tool", {})
    assert not ok
    assert "未知工具" in err


def test_tool_result_to_content():
    result = ToolResult(success=True, data={"k": "v"})
    assert '"k"' in result.to_content()
    error = ToolResult(success=False, error="oops")
    assert "[错误]" in error.to_content()


def test_register_duplicate_raises():
    registry = ToolRegistry()
    from tools.weather import WeatherTool

    registry.register(WeatherTool())
    with pytest.raises(ValueError):
        registry.register(WeatherTool())


@pytest.mark.asyncio
async def test_weather_tool_uses_httpx_async(monkeypatch):
    from tools.weather import WeatherTool

    tool = WeatherTool()

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "current_condition": [
                    {
                        "weatherDesc": [{"value": "晴"}],
                        "temp_C": "25",
                        "humidity": "60",
                        "windspeedKmph": "10",
                    }
                ]
            }

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url, params=None):
            self.calls += 1
            return FakeResp()

        async def aclose(self):
            return None

    fake = FakeClient()

    async def fake_get_client():
        return fake

    monkeypatch.setattr(tool, "_get_client", fake_get_client)

    result = await tool.execute("北京")
    assert result.success, result.error
    assert result.data["city"] == "北京"
    assert result.data["weather"] == "晴"
    assert result.data["temp"] == "25"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_weather_tool_handles_httpx_timeout(monkeypatch):
    import httpx
    from tools.weather import WeatherTool

    tool = WeatherTool()

    class TimeoutClient:
        async def get(self, url, params=None):
            raise httpx.TimeoutException("simulated")

        async def aclose(self):
            return None

    async def fake_get_client():
        return TimeoutClient()

    monkeypatch.setattr(tool, "_get_client", fake_get_client)

    result = await tool.execute("上海")
    assert not result.success
    assert "超时" in result.error


@pytest.mark.asyncio
async def test_search_tool_uses_async_client(monkeypatch):
    from tools.search import SearchTool

    tool = SearchTool()

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, params=None):
            self.calls.append((url, params))
            return FakeResponse(
                {
                    "results": [
                        {
                            "title": "Example",
                            "url": "https://example.com",
                            "content": "summary",
                        }
                    ]
                }
            )

        async def aclose(self):
            return None

    fake = FakeClient()

    async def fake_get_client():
        return fake

    monkeypatch.setattr(tool, "_get_client", fake_get_client)

    result = await tool.execute("test query")
    assert result.success
    assert "Example" in result.data["text"]
    assert fake.calls


@pytest.mark.asyncio
async def test_tool_hub_disconnect_closes_tools():
    from tools.hub import get_tool_hub

    closed = {"value": False}

    class ClosableTool(Tool):
        name = "closable_tool"
        description = "Closable"

        def schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                params=[ToolParam(name="x", type="string", required=False)],
            )

        async def execute(self, **kwargs) -> ToolResult:
            return ToolResult(success=True, data={"ok": True})

        async def aclose(self):
            closed["value"] = True

    hub = get_tool_hub()
    hub.register_python_tool(ClosableTool())
    await hub.disconnect_all()
    assert closed["value"] is True
