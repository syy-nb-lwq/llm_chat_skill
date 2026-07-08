"""补充测试:覆盖本次 review 发现的问题"""
import asyncio
import pytest

from agents.learning import (
    LearningAgent, ToolTask, resolve_params,
)
from tools.base import Tool, ToolResult, ToolSchema, ToolParam


# ===== resolve_params =====

def test_resolve_user_input_string():
    out = resolve_params(
        {"city": "${user_input.city}", "raw": "${user_input.text}"},
        results={},
        user_input={"city": "厦门", "text": "厦门明天怎么玩"},
    )
    assert out == {"city": "厦门", "raw": "厦门明天怎么玩"}


def test_resolve_user_input_missing_yields_empty():
    out = resolve_params("hi ${user_input.missing}", {}, {"x": 1})
    assert out == "hi "


def test_resolve_no_user_input_yields_empty():
    out = resolve_params("${user_input.city}", {}, user_input=None)
    assert out == ""


# ===== execute_dag 边界 =====

def _stub(tool_name, ret):
    class Stub(Tool):
        name = tool_name
        description = ""
        def schema(self):
            return ToolSchema(name=tool_name, description="",
                              params=[ToolParam("x", "string")])
        async def execute(self, x=""):
            return ret
    return Stub()


@pytest.mark.asyncio
async def test_execute_dag_missing_dep_no_deadlock():
    """依赖了不存在的 task,不会死循环,被丢弃"""
    la = LearningAgent()
    la.registry.register(_stub("a", ToolResult(success=True, data={"x": 1})))

    tasks = [
        ToolTask(id="a", type="a", params={"x": "1"}),
        ToolTask(id="b", type="a", params={"x": "2"}, depends_on=["ghost"]),
    ]
    results = await la.execute_dag(tasks)
    assert results["a"].success
    # b 因为依赖不存在的 ghost 而被丢弃,不在 results 中
    assert "b" not in results


@pytest.mark.asyncio
async def test_execute_dag_upstream_fail_no_fallback_skipped():
    la = LearningAgent()
    la.registry.register(_stub("a", ToolResult(success=False, error="boom")))

    tasks = [
        ToolTask(id="a", type="a", params={"x": "1"}),
        ToolTask(id="b", type="a", params={"x": "2"}, depends_on=["a"]),
    ]
    results = await la.execute_dag(tasks)
    assert not results["a"].success
    assert not results["b"].success
    assert "skipped" in results["b"].error


@pytest.mark.asyncio
async def test_execute_dag_fallback_allows_downstream():
    la = LearningAgent()
    # a 失败但 fallback 到 b, b 应该继续执行
    la.registry.register(_stub("a", ToolResult(success=False, error="boom")))
    la.registry.register(_stub("b", ToolResult(success=True, data={"ok": True})))

    tasks = [
        ToolTask(id="a", type="a", params={"x": "1"}, fallback_to="b"),
        ToolTask(id="b", type="b", params={"x": "2"}),
    ]
    results = await la.execute_dag(tasks)
    assert not results["a"].success
    assert results["b"].success


@pytest.mark.asyncio
async def test_execute_dag_user_input_passed():
    la = LearningAgent()
    la.registry.register(_stub("a", ToolResult(success=True, data={"city": "厦门"})))

    tasks = [
        ToolTask(id="a", type="a", params={"x": "${user_input.city}"})
    ]
    results = await la.execute_dag(
        tasks, user_input={"city": "厦门"}
    )
    assert results["a"].success


@pytest.mark.asyncio
async def test_execute_dag_retry_eventually_succeeds():
    """失败重试直到成功"""
    la = LearningAgent()
    # 用一个会失败的 stub,然后注入一个成功 stub
    class FlakyFirst(Tool):
        name = "flaky"
        attempts = 0
        def schema(self):
            return ToolSchema(name="flaky", description="",
                              params=[ToolParam("x", "string")])
        async def execute(self, x=""):
            FlakyFirst.attempts += 1
            if FlakyFirst.attempts < 2:
                return ToolResult(success=False, error="transient")
            return ToolResult(success=True, data={"ok": True})

    la.registry.register(FlakyFirst())
    tasks = [ToolTask(id="a", type="flaky", params={"x": "1"}, retry=2)]
    results = await la.execute_dag(tasks)
    assert results["a"].success
    assert FlakyFirst.attempts == 2


@pytest.mark.asyncio
async def test_execute_dag_timeout_event():
    """超时返回失败"""
    la = LearningAgent()

    class SlowTool(Tool):
        name = "slow"
        def schema(self):
            return ToolSchema(name="slow", description="",
                              params=[ToolParam("x", "string")])
        async def execute(self, x=""):
            await asyncio.sleep(2)
            return ToolResult(success=True, data={})

    la.registry.register(SlowTool())
    tasks = [ToolTask(id="a", type="slow", params={"x": "1"}, timeout_s=1)]
    results = await la.execute_dag(tasks)
    assert not results["a"].success
    assert "超时" in results["a"].error