"""M0-01/M0-02 单元测试。

覆盖:
- 每次 Agent.handle() 生成唯一的 execution_id
- 同 session 多次 handle 失败记录互不覆盖
- ToolResult.meta 携带 attempts / latency_ms / timeout_s / fallback_to
- build_execution_context 透传 identity
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from core.identity import IdentityContext, new_id
from core.critic import build_execution_context
from tools.base import ToolResult


def test_new_id_format():
    a = new_id("exec")
    b = new_id("exec")
    assert a != b
    assert a.startswith("exec-")


def test_identity_context_unique_per_construction():
    a = IdentityContext(user_id="u", session_id="s")
    b = IdentityContext(user_id="u", session_id="s")
    assert a.turn_id != b.turn_id
    assert a.execution_id != b.execution_id


def test_identity_child_preserves_turn():
    parent = IdentityContext(user_id="u", session_id="s")
    child = parent.child()
    assert child.turn_id == parent.turn_id
    assert child.execution_id != parent.execution_id
    assert child.parent_execution_id == parent.execution_id


def test_build_execution_context_uses_task_specs():
    res = ToolResult(success=True, data="x", meta={"attempts": 2, "latency_ms": 12})
    spec = {
        "t1": {"type": "weather_query", "retry": 1, "timeout_s": 10, "fallback_to": "t2"},
    }
    ctx = build_execution_context(
        trace_id="trace_xyz",
        scenario="weather",
        intent="skill",
        selected_skill=None,
        tool_results={"t1": res},
        latency_ms=100,
        task_specs=spec,
        execution_id="exec_1",
        turn_id="turn_1",
        user_id="u",
        session_id="s",
    )
    assert ctx.execution_id == "exec_1"
    assert ctx.turn_id == "turn_1"
    assert ctx.tasks[0].tool == "weather_query"
    # 2 次尝试意味着 retry_count = 1
    assert ctx.tasks[0].retry_count == 1
    # spec 中有 fallback_to
    assert ctx.tasks[0].used_fallback is True


def test_build_execution_context_fallback_when_meta_attempts_missing():
    res = ToolResult(success=True, data="x")
    ctx = build_execution_context(
        trace_id="t",
        scenario="",
        intent="",
        selected_skill=None,
        tool_results={"t1": res},
        latency_ms=0,
        task_specs={"t1": {"type": "weather_query"}},
    )
    # spec.retry=0 + meta 没 attempts → retry_count = 0
    assert ctx.tasks[0].retry_count == 0
    assert ctx.tasks[0].used_fallback is False


@pytest.mark.asyncio
async def test_agent_handle_unique_execution_id_per_call(monkeypatch):
    """同 session 多次 handle 必须生成不同 execution_id。"""
    from infra.llm import LLMClient
    from core.agent import Agent
    from agents import manager as mgr_mod

    monkeypatch.setattr(mgr_mod.ManagerAgent, "should_answer_directly", lambda self, t: True)

    class StubLLM(LLMClient):
        def __init__(self):
            self.model = "fake"
            self.default_temperature = 0.0

        async def chat_with_retry(self, messages, **kw):
            return "ok"

        async def _complete(self, messages, temperature=None):
            return "ok"

        async def _stream(self, messages, temperature=None):
            for ch in "ok":
                yield ch

        async def stream(self, messages, **kw):
            async for c in self._stream(messages):
                yield c

    monkeypatch.setattr("infra.llm.get_llm_client", lambda: StubLLM())
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: StubLLM())
    from infra import llm as _l
    _l._llm_client = None

    a = Agent(session_id="s1")
    await a.handle("hi")
    first = a.trace_id
    assert first
    await a.handle("hello again")
    second = a.trace_id
    assert second
    assert first != second


@pytest.mark.asyncio
async def test_learning_records_meta_attempts_and_latency(monkeypatch):
    """LearningAgent.execute_tool 必须在 result.meta 中写入 attempts/latency。"""
    from agents.learning import LearningAgent

    learn = LearningAgent()
    # 让 hub.get_tool 接受 "demo"
    monkeypatch.setattr(learn.hub, "get_tool", lambda name: object() if name == "demo" else None)

    async def fake_call(name, params):
        await asyncio.sleep(0.001)
        return ToolResult(success=True, data={"x": 1})
    monkeypatch.setattr(learn.hub, "call_tool", fake_call)

    result = await learn.execute_tool(
        "demo", {"a": 1}, retry=2, timeout_s=5, fallback_to="t2",
    )
    assert result.success
    assert result.meta.get("attempts") == 1
    assert "latency_ms" in result.meta
    assert result.meta.get("timeout_s") == 5
    assert result.meta.get("fallback_to") == "t2"
