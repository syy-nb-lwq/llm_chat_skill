"""ToolHub 参数校验（tools/validation.py）的契约测试。

背景：`hub._call_instance` 过去直接 `execute(**params)`，`**kwargs` 派的工具
会把拼错的参数当成"没传"，甚至给默认值后返回成功 —— 模型据此继续推理，
比报错危险得多。这些测试锁住"宁可失败，也不能带着错参数成功"。
"""
from __future__ import annotations

import pytest

from tools.base import Tool, ToolParam, ToolResult, ToolSchema
from tools.validation import validate_params


def _schema(*params: ToolParam) -> ToolSchema:
    return ToolSchema(name="demo", description="demo", params=list(params))


# ===== 纯函数层 =====


def test_unknown_param_is_rejected_with_suggestion():
    schema = _schema(ToolParam(name="path", type="string", required=True))
    ok, _, err = validate_params(schema, {"file_path": "/tmp/a.txt"})
    assert not ok
    assert "未知参数" in err
    assert "file_path" in err
    # 关键：给出最接近的候选，模型才知道改哪个字
    assert "path" in err


def test_missing_required_is_rejected():
    schema = _schema(ToolParam(name="path", type="string", required=True))
    ok, _, err = validate_params(schema, {})
    assert not ok
    assert "缺少必填参数 path" in err


def test_required_none_is_rejected():
    schema = _schema(ToolParam(name="path", type="string", required=True))
    ok, _, err = validate_params(schema, {"path": None})
    assert not ok
    assert "不能为空" in err


def test_optional_none_is_dropped_so_default_applies():
    schema = _schema(
        ToolParam(name="path", type="string", required=True),
        ToolParam(name="max_chars", type="integer", required=False),
    )
    ok, params, err = validate_params(schema, {"path": "a", "max_chars": None})
    assert ok, err
    assert params == {"path": "a"}


@pytest.mark.parametrize(
    "raw,expected",
    [("5", 5), (5.0, 5), (5, 5)],
)
def test_integer_coercion(raw, expected):
    schema = _schema(ToolParam(name="n", type="integer"))
    ok, params, err = validate_params(schema, {"n": raw})
    assert ok, err
    assert params["n"] == expected
    assert isinstance(params["n"], int)


def test_bad_integer_is_rejected_not_defaulted():
    schema = _schema(ToolParam(name="n", type="integer"))
    ok, _, err = validate_params(schema, {"n": "abc"})
    assert not ok
    assert "需要整数" in err


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), (1, True), (0, False)])
def test_boolean_coercion(raw, expected):
    schema = _schema(ToolParam(name="flag", type="boolean"))
    ok, params, err = validate_params(schema, {"flag": raw})
    assert ok, err
    assert params["flag"] is expected


def test_array_from_json_string():
    schema = _schema(ToolParam(name="items", type="array"))
    ok, params, err = validate_params(schema, {"items": '["a", "b"]'})
    assert ok, err
    assert params["items"] == ["a", "b"]


def test_object_from_json_string():
    schema = _schema(ToolParam(name="payload", type="object"))
    ok, params, err = validate_params(schema, {"payload": '{"k": 1}'})
    assert ok, err
    assert params["payload"] == {"k": 1}


def test_enum_is_checked_after_coercion():
    schema = _schema(ToolParam(name="mode", type="string", enum=["fast", "slow"]))
    ok, _, err = validate_params(schema, {"mode": "medium"})
    assert not ok
    assert "取值非法" in err


def test_unknown_schema_means_no_validation():
    """拿不到契约就放行 —— 拦下来只会把"能用"变成"不能用"。"""
    ok, params, err = validate_params(None, {"anything": 1})
    assert ok and not err
    assert params == {"anything": 1}


def test_input_dict_is_not_mutated():
    schema = _schema(ToolParam(name="n", type="integer"))
    original = {"n": "5"}
    validate_params(schema, original)
    assert original == {"n": "5"}


def test_json_schema_style_is_supported():
    schema = {
        "properties": {
            "q": {"type": "string", "description": "query"},
            "limit": {"type": "integer"},
        },
        "required": ["q"],
    }
    ok, params, err = validate_params(schema, {"q": "x", "limit": "3"})
    assert ok, err
    assert params == {"q": "x", "limit": 3}


# ===== Hub 集成层 =====


class _SilentKwargsTool(Tool):
    """模拟"静默忽略参数"的工具 —— 正是这次要治的那一类。"""

    name = "kw_tool"
    description = "demo"

    def schema(self) -> ToolSchema:
        return _schema(
            ToolParam(name="path", type="string", required=True),
            ToolParam(name="max_chars", type="integer", required=False),
        )

    async def execute(self, **kwargs) -> ToolResult:
        # 过去这里会 kwargs.get("path") or "" → 空字符串 → 返回成功
        return ToolResult(success=True, data={"got": kwargs.get("path") or ""})


@pytest.mark.asyncio
async def test_hub_rejects_typo_instead_of_silent_success():
    from tools.hub import ToolHub

    hub = ToolHub()
    hub.register_python_tool(_SilentKwargsTool())

    result = await hub.call_tool("kw_tool", {"file_path": "/tmp/a.txt"})
    assert result.success is False
    assert "参数错误" in result.error
    assert "file_path" in result.error
    assert result.meta.get("invalid_params") is True


@pytest.mark.asyncio
async def test_hub_passes_coerced_params_through():
    from tools.hub import ToolHub

    captured = {}

    class _Capture(Tool):
        name = "cap_tool"
        description = "demo"

        def schema(self) -> ToolSchema:
            return _schema(
                ToolParam(name="path", type="string"),
                ToolParam(name="max_chars", type="integer"),
            )

        async def execute(self, **kwargs) -> ToolResult:
            captured.update(kwargs)
            return ToolResult(success=True, data={})

    hub = ToolHub()
    hub.register_python_tool(_Capture())
    result = await hub.call_tool("cap_tool", {"path": "a.txt", "max_chars": "100"})

    assert result.success is True
    assert captured["max_chars"] == 100
    assert isinstance(captured["max_chars"], int)


@pytest.mark.asyncio
async def test_hub_still_emits_events_when_params_invalid():
    """参数错误也要走 tool_call/tool_result —— 否则前端看不到这次失败。"""
    from tools.hub import ToolHub

    hub = ToolHub()
    hub.register_python_tool(_SilentKwargsTool())

    events = []

    async def on_event(name, payload):
        events.append((name, payload))

    await hub.call_tool("kw_tool", {"nope": 1}, on_event=on_event)

    assert [e[0] for e in events] == ["tool_call", "tool_result"]
    assert events[1][1]["error"]
