"""Agent 单元测试 - mock LLM,不依赖真实 API"""
import asyncio
import json
from typing import Any, Dict

import pytest

from infra.llm import LLMClient


# ===== Mock LLM =====

class FakeLLM(LLMClient):
    """替换真正的 LLM,返回预设响应"""
    def __init__(self, scripted: list):
        # 不调父类 __init__(避免 OpenAI client 实例化)
        self.model = "fake"
        self.default_temperature = 0.0
        self._scripted = list(scripted)

    async def chat_with_retry(self, messages, **kw):
        if not self._scripted:
            return "{}"
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def _complete(self, messages, temperature=None):
        return await self.chat_with_retry(messages)

    async def _stream(self, messages, temperature=None):
        for ch in "streamed":
            yield ch

    async def stream(self, messages, **kw):
        async for c in self._stream(messages):
            yield c


def _patch_llm(monkeypatch, scripted):
    fake = FakeLLM(scripted)
    # BaseAgent.__init__ 里 `from infra.llm import get_llm_client`,
    # 该名字绑定在 core.agent_base 模块里。要同时 patch 两边。
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    # 清掉可能已经缓存的实例
    from infra import llm as _llm_mod
    _llm_mod._llm_client = None
    return fake


# ===== Manager =====

@pytest.mark.asyncio
async def test_manager_plan_parses_valid_json(monkeypatch):
    valid = json.dumps({
        "intent": "查天气",
        "selected_skill": "",
        "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
    }, ensure_ascii=False)
    _patch_llm(monkeypatch, [valid])

    from agents.manager import ManagerAgent, IntentType
    m = ManagerAgent()
    # 使用更长的输入避免被闲聊检测拦截
    plan = await m.plan("帮我查一下厦门今天的天气")
    assert plan.intent == IntentType.SKILL
    assert len(plan.tool_tasks) == 1
    assert plan.tool_tasks[0]["type"] == "weather_query"


@pytest.mark.asyncio
async def test_manager_plan_invalid_then_valid(monkeypatch):
    """输入 "hi" 现在会被闲聊检测拦截,不调用 LLM"""
    from agents.manager import ManagerAgent, IntentType
    m = ManagerAgent()
    plan = await m.plan("hi")
    # "hi" 应该是闲聊,不需要调用 LLM
    assert plan.intent == IntentType.CHITCHAT
    assert plan.tool_tasks == []


@pytest.mark.asyncio
async def test_manager_plan_all_fail_degrades(monkeypatch):
    """输入 "hi" 现在会被闲聊检测拦截"""
    from agents.manager import ManagerAgent, IntentType
    m = ManagerAgent()
    plan = await m.plan("hi")
    # "hi" 应该是闲聊,不需要调用 LLM
    assert plan.intent == IntentType.CHITCHAT
    assert plan.tool_tasks == []


def test_manager_should_answer_directly():
    from agents.manager import ManagerAgent
    m = ManagerAgent()
    assert m.should_answer_directly("你好")
    assert m.should_answer_directly("hello")
    assert not m.should_answer_directly("查厦门天气")


def test_manager_should_learn_skill():
    from agents.manager import ManagerAgent
    m = ManagerAgent()
    assert m.should_learn_skill("以后应该先查天气")
    assert not m.should_learn_skill("查厦门天气")


# ===== Orchestrator =====

@pytest.mark.asyncio
async def test_orchestrator_stream(monkeypatch):
    _patch_llm(monkeypatch, ["ok"])
    from agents.orchestrator import OrchestratorAgent
    from tools.base import ToolResult
    o = OrchestratorAgent()
    chunks = []
    async for c in o.stream("hello", {}, None):
        chunks.append(c)
    assert chunks == ["s", "t", "r", "e", "a", "m", "e", "d"]


# ===== SkillTrainer =====

@pytest.mark.asyncio
async def test_trainer_heuristic_no_trigger():
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok, conf, reason = await t.detect("查厦门天气")
    assert ok is False


@pytest.mark.asyncio
async def test_trainer_extract_returns_skill(monkeypatch, tmp_path):
    from skills.manager import reset_skill_store
    reset_skill_store()

    extract = json.dumps({
        "name": "demo",
        "method": "do it",
        "capability": "can do",
        "patterns": ["demo"],
        "tags": ["t"],
        "steps": [{"id": "s1", "name": "do", "description": "do"}],
    }, ensure_ascii=False)
    _patch_llm(monkeypatch, [
        json.dumps({"is_teaching": True, "confidence": 0.9, "reason": "ok"}),
        extract,
    ])

    # 把 user 目录指到 tmp_path
    import skills.manager as mgr
    mgr._store = mgr.SkillStore(str(tmp_path / "skills"))

    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    t.skill_store = mgr.get_skill_store(str(tmp_path / "skills"))

    ok, msg, skill = await t.teach("以后做 demo,应该这样")
    assert ok is True
    assert skill.name == "demo"
    assert skill.source == "taught"
    # 写入文件
    files = list((tmp_path / "skills" / "user").glob("demo@*.yaml"))
    assert len(files) == 1


# ===== 多轮对话 Context =====

@pytest.mark.asyncio
async def test_manager_plan_passes_context_to_llm(monkeypatch):
    """传入 context 时,LLM 收到的 messages 应包含历史 user/assistant。"""
    captured = {}

    class CapturingLLM(LLMClient):
        def __init__(self):
            self.model = "fake"
            self.default_temperature = 0.0

        async def chat_with_retry(self, messages, **kw):
            captured["messages"] = messages
            return json.dumps({"intent": "x", "tool_tasks": []}, ensure_ascii=False)

        async def _complete(self, messages, temperature=None):
            return await self.chat_with_retry(messages)

        async def _stream(self, messages, temperature=None):
            yield "ok"

        async def stream(self, messages, **kw):
            async for c in self._stream(messages):
                yield c

    from core.agent_base import get_llm_client as gab_g
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: CapturingLLM())
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: CapturingLLM())
    from infra import llm as _l
    _l._llm_client = None

    from agents.manager import ManagerAgent
    from core.context import Context

    ctx = Context()
    ctx.add_user_message("第一句:我打算明天去厦门玩")
    ctx.add_assistant_message("好的,请问具体哪天走?")
    ctx.add_user_message("查一下厦门明天的天气")

    m = ManagerAgent()
    await m.plan("查一下厦门明天的天气", context=ctx)

    msgs = captured["messages"]
    assert msgs, "应捕获到 LLM messages"
    user_msgs = [x for x in msgs if x.get("role") == "user"]
    # user prompt 应包含历史对话块
    assert any("对话历史" in (m.get("content") or "") or "第一句" in (m.get("content") or "")
               for m in user_msgs), "user prompt 应拼接历史对话"


@pytest.mark.asyncio
async def test_manager_plan_without_context_keeps_old_behavior(monkeypatch):
    """不传 context 时,user prompt 应保持原 user_input。"""
    captured = {}

    class CapturingLLM(LLMClient):
        def __init__(self):
            self.model = "fake"
            self.default_temperature = 0.0

        async def chat_with_retry(self, messages, **kw):
            captured["messages"] = messages
            return json.dumps({"intent": "x", "tool_tasks": []}, ensure_ascii=False)

        async def _complete(self, messages, temperature=None):
            return await self.chat_with_retry(messages)

        async def _stream(self, messages, temperature=None):
            yield "ok"

        async def stream(self, messages, **kw):
            async for c in self._stream(messages):
                yield c

    monkeypatch.setattr("infra.llm.get_llm_client", lambda: CapturingLLM())
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: CapturingLLM())
    from infra import llm as _l
    _l._llm_client = None

    from agents.manager import ManagerAgent

    m = ManagerAgent()
    await m.plan("查一下厦门明天的天气")

    user_msgs = [x for x in captured["messages"] if x.get("role") == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "查一下厦门明天的天气"


# ===== emit 顺序(B3 修复) =====

@pytest.mark.asyncio
async def test_agent_handle_async_on_event_preserves_order(monkeypatch):
    """异步 on_event 时,事件必须按 emit 顺序完成(不依赖 create_task 调度顺序)。"""
    import asyncio
    from core.agent import Agent

    # 准备 LLM 剧本
    plan_json = json.dumps({"intent": "hi", "selected_skill": "", "tool_tasks": []}, ensure_ascii=False)
    _patch_llm(monkeypatch, [plan_json])

    # 让 manager.should_answer_directly 为 True,走直答分支(最快路径,无工具)
    from agents import manager as mgr_mod
    monkeypatch.setattr(mgr_mod.ManagerAgent, "should_answer_directly", lambda self, t: True)

    a = Agent(session_id="t")
    received: list = []

    async def slow_on_event(event, payload):
        # 模拟一个不确定完成的 async 回调
        await asyncio.sleep(0.01)
        received.append(event)

    # 至少要触发:thinking/message_delta(s) / message_final
    ans = await a.handle("hi", on_event=slow_on_event)
    assert ans  # 有返回

    # 关键断言:收到的所有 event 都在 handle() 完成前全部完成(否则 received 不应只到这里就完)
    assert "message_final" in received
    # 顺序检查:第一次出现的 event 应该是 thinking,最后一次是 message_final
    assert received[0] == "thinking"
    assert received[-1] == "message_final"


@pytest.mark.asyncio
async def test_agent_handle_drains_pending_async_on_exception(monkeypatch):
    """handle 异常时,pending 的异步 emit 也要被 drain(不能泄漏)"""
    from core.agent import Agent
    from agents import manager as mgr_mod
    from infra.llm import LLMClient

    # 走直答路径
    monkeypatch.setattr(mgr_mod.ManagerAgent, "should_answer_directly", lambda self, t: True)

    # 写一个会在 stream 时抛错的 LLM
    class BoomLLM(LLMClient):
        def __init__(self):
            self.model = "fake"
            self.default_temperature = 0.0
        async def chat_with_retry(self, messages, **kw):
            return "ok"
        async def _complete(self, messages, temperature=None):
            return "ok"
        async def _stream(self, messages, temperature=None):
            raise RuntimeError("boom")
            yield  # 让它成 generator(实际不会跑到)
        async def stream(self, messages, **kw):
            async for c in self._stream(messages):
                yield c

    monkeypatch.setattr("infra.llm.get_llm_client", lambda: BoomLLM())
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: BoomLLM())
    from infra import llm as _l
    _l._llm_client = None

    a = Agent(session_id="t")
    received: list = []

    async def on_event(event, payload):
        await asyncio.sleep(0.001)
        received.append(event)

    with pytest.raises(RuntimeError):
        await a.handle("hi", on_event=on_event)

    # 异常分支也 emit 了 error,必须被 drain
    assert "error" in received