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