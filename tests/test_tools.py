"""工具系统测试"""
import pytest
from tools.base import ToolRegistry, ToolResult, get_tool_registry


def test_registry_builtins_registered():
    reg = get_tool_registry()
    names = reg.names()
    assert "weather_query" in names
    assert "web_search" in names


def test_tool_schemas_have_required_fields():
    reg = get_tool_registry()
    for schema in reg.schemas():
        assert schema["name"]
        assert schema["description"]
        assert isinstance(schema["params"], list)
        # 至少有一个 param
        assert len(schema["params"]) >= 1


def test_validate_params_missing_required():
    reg = get_tool_registry()
    ok, err = reg.validate_params("weather_query", {})  # 缺 city
    assert not ok
    assert "city" in err


def test_validate_params_ok():
    reg = get_tool_registry()
    ok, err = reg.validate_params("weather_query", {"city": "厦门"})
    assert ok, err


def test_validate_unknown_tool():
    reg = get_tool_registry()
    ok, err = reg.validate_params("nope_tool", {})
    assert not ok
    assert "未知工具" in err


def test_tool_result_to_content():
    r = ToolResult(success=True, data={"k": "v"})
    assert '"k"' in r.to_content()
    r2 = ToolResult(success=False, error="oops")
    assert "[错误]" in r2.to_content()


def test_register_duplicate_raises():
    reg = ToolRegistry()
    from tools.weather import WeatherTool
    reg.register(WeatherTool())
    with pytest.raises(ValueError):
        reg.register(WeatherTool())


@pytest.mark.asyncio
async def test_weather_tool_uses_httpx_async(monkeypatch):
    """WeatherTool.execute 必须 await 异步客户端,而不是用同步 requests 阻塞事件循环"""
    from tools.weather import WeatherTool

    tool = WeatherTool()

    class _FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return {
                "current_condition": [{
                    "weatherDesc": [{"value": "晴"}],
                    "temp_C": "25",
                    "humidity": "60",
                    "windspeedKmph": "10",
                }]
            }

    class _FakeClient:
        def __init__(self, *a, **kw): self.calls = 0
        async def get(self, url, params=None):
            self.calls += 1
            return _FakeResp()
        async def aclose(self): pass

    fake = _FakeClient()
    # 替换 _get_client 直接返回 fake,跳过真实 httpx 构造
    async def _fake_get_client():
        return fake
    monkeypatch.setattr(tool, "_get_client", _fake_get_client)

    result = await tool.execute("北京")
    assert result.success, result.error
    assert result.data["city"] == "北京"
    assert result.data["weather"] == "晴"
    assert result.data["temp"] == "25"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_weather_tool_handles_httpx_timeout(monkeypatch):
    """网络超时应被捕获并返回 success=False(而不是抛异常)"""
    import httpx
    from tools.weather import WeatherTool

    tool = WeatherTool()

    class _TimeoutClient:
        async def get(self, url, params=None):
            raise httpx.TimeoutException("simulated")
        async def aclose(self): pass

    async def _fake_get_client():
        return _TimeoutClient()
    monkeypatch.setattr(tool, "_get_client", _fake_get_client)

    result = await tool.execute("上海")
    assert not result.success
    assert "超时" in result.error