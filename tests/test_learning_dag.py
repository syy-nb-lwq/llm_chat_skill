"""Learning DAG 执行测试"""
import asyncio
import pytest

from agents.learning import LearningAgent, ToolTask, resolve_params
from tools.base import ToolResult


def test_resolve_simple_var():
    results = {"t1": ToolResult(success=True, data={"city": "厦门"})}
    out = resolve_params({"query": "${t1.data.city} 景点"}, results)
    assert out == {"query": "厦门 景点"}


def test_resolve_nested_dict():
    results = {"t1": ToolResult(success=True, data={"city": "厦门", "date": "2026-07-08"})}
    out = resolve_params({"a": "${t1.data.city}", "b": "${t1.data.date}"}, results)
    assert out == {"a": "厦门", "b": "2026-07-08"}


def test_resolve_failed_upstream_yields_empty():
    results = {"t1": ToolResult(success=False, error="oops")}
    out = resolve_params("城市=${t1.data.city}", results)
    assert out == "城市="


def test_resolve_missing_yields_empty():
    results = {}
    out = resolve_params("hi ${t9.data.x}", results)
    assert out == "hi "


def test_execute_tasks_sync_serial():
    """旧 API 兼容 - 现在走 execute_dag(单元素)"""
    import asyncio
    la = LearningAgent()

    async def _run():
        from agents.learning import ToolTask
        return await la.execute_dag([
            ToolTask(id="t1", type="weather_query", params={"city": "厦门"}),
        ])

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # pytest-asyncio 已经在运行 loop,直接 await
            import pytest
            pytest.skip("event loop 已在运行,改用 pytest-asyncio")
            return
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        res = loop.run_until_complete(_run())
    finally:
        loop.close()
    assert "t1" in res


@pytest.mark.asyncio
async def test_execute_dag_simple(monkeypatch):
    """注入伪工具,验证 DAG 调度"""
    la = LearningAgent()

    # 用 monkeypatch 替换 tools 中的 weather_query 为 stub
    from tools.base import Tool, ToolResult, ToolSchema, ToolParam

    class StubTool(Tool):
        name = "stub_1"
        description = "stub"
        def schema(self):
            return ToolSchema(name=self.name, description=self.description,
                              params=[ToolParam("x", "string")])
        async def execute(self, x):
            return ToolResult(success=True, data={"echo": x})

    la.registry.register(StubTool())

    tasks = [ToolTask(id="t1", type="stub_1", params={"x": "hello"})]
    results = await la.execute_dag(tasks)
    assert "t1" in results
    assert results["t1"].success
    assert results["t1"].data == {"echo": "hello"}


@pytest.mark.asyncio
async def test_execute_dag_with_dependency(monkeypatch):
    la = LearningAgent()
    from tools.base import Tool, ToolResult, ToolSchema, ToolParam

    class StubA(Tool):
        name = "stub_a"
        def schema(self):
            return ToolSchema(name=self.name, description="",
                              params=[ToolParam("x", "string")])
        async def execute(self, x):
            return ToolResult(success=True, data={"y": x + "!"})

    class StubB(Tool):
        name = "stub_b"
        def schema(self):
            return ToolSchema(name=self.name, description="",
                              params=[ToolParam("z", "string")])
        async def execute(self, z):
            return ToolResult(success=True, data={"final": z})

    la.registry.register(StubA())
    la.registry.register(StubB())

    tasks = [
        ToolTask(id="a", type="stub_a", params={"x": "hi"}),
        ToolTask(id="b", type="stub_b",
                 params={"z": "${a.data.y}"}, depends_on=["a"]),
    ]
    results = await la.execute_dag(tasks)
    assert results["b"].data == {"final": "hi!"}


@pytest.mark.asyncio
async def test_execute_dag_cycle_aborts():
    la = LearningAgent()
    tasks = [
        ToolTask(id="x", type="a", depends_on=["y"]),
        ToolTask(id="y", type="b", depends_on=["x"]),
    ]
    results = await la.execute_dag(tasks)
    assert results == {}  # 循环直接返回空