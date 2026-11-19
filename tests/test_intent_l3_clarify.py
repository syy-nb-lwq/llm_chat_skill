"""L3 澄清层测试。

覆盖:
  - 澄清问题的生成与校验(非法 JSON / 空问题 / 选项清洗)
  - LLM 不可用时的模板兜底
  - 澄清轮次上限(连续澄清只允许 1 轮)
  - Manager 的 L2 → L3 触发条件
  - Agent 的澄清分支(事件、上下文记账、不进 critic)
"""
import json

import pytest

from core.clarifier import (
    ClarificationRequest,
    L3Clarifier,
    detect_missing_slot,
    get_clarifier,
    reset_clarifier,
)


# ===== 工具函数 =====

def test_detect_missing_slot():
    assert detect_missing_slot({"city": "", "date": "明天"}) == "city"
    assert detect_missing_slot({"city": "厦门", "date": "明天"}) == ""
    assert detect_missing_slot({}) == ""
    assert detect_missing_slot(None) == ""


def test_clarification_request_to_text():
    req = ClarificationRequest(question="查哪个城市?", options=["厦门", "北京"])
    text = req.to_text()
    assert "查哪个城市?" in text
    assert "1. 厦门" in text
    assert "2. 北京" in text


def test_clarification_request_to_text_without_options():
    req = ClarificationRequest(question="再说具体点?")
    assert req.to_text() == "再说具体点?"


# ===== 生成 =====

class _FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat_with_retry(self, messages, **kw):
        self.calls.append({"messages": messages, "kw": kw})
        if not self._responses:
            return "{}"
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_llm(monkeypatch, responses):
    fake = _FakeLLM(responses)
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    from infra import llm as llm_mod
    llm_mod._llm_client = None
    return fake


@pytest.mark.asyncio
async def test_clarify_generates_question(monkeypatch):
    fake = _patch_llm(monkeypatch, [json.dumps({
        "question": "你想查哪个城市的天气?",
        "options": ["厦门", "北京", "上海"],
    })])
    c = L3Clarifier()
    req = await c.clarify("查一下天气", reason="缺少城市")
    assert req is not None
    assert req.question == "你想查哪个城市的天气?"
    assert req.options == ["厦门", "北京", "上海"]
    assert req.source == "llm"
    assert req.reason == "缺少城市"
    # 提示词里要有上下文
    prompt = fake.calls[0]["messages"][-1]["content"]
    assert "查一下天气" in prompt
    assert "缺少城市" in prompt


@pytest.mark.asyncio
async def test_clarify_rejects_empty_question(monkeypatch):
    _patch_llm(monkeypatch, [json.dumps({"question": "", "options": []})])
    c = L3Clarifier()
    req = await c.clarify("查一下天气", reason="缺少城市", slot="city")
    # 空问题 → 退回模板
    assert req is not None
    assert req.source == "template"
    assert req.question


@pytest.mark.asyncio
async def test_clarify_rejects_garbage(monkeypatch):
    _patch_llm(monkeypatch, ["不是 JSON"])
    c = L3Clarifier()
    req = await c.clarify("查一下天气", reason="缺少城市", slot="city")
    assert req is not None
    assert req.source == "template"


@pytest.mark.asyncio
async def test_clarify_falls_back_on_llm_error(monkeypatch):
    _patch_llm(monkeypatch, [RuntimeError("boom")])
    c = L3Clarifier()
    req = await c.clarify("查一下天气", reason="缺少城市", slot="city")
    assert req is not None
    assert req.source == "template"
    assert req.question == "你想查哪个城市?"


@pytest.mark.asyncio
async def test_clarify_normalizes_options(monkeypatch):
    _patch_llm(monkeypatch, [json.dumps({
        "question": "查哪个城市?",
        "options": ["厦门", "厦门", "", "  ", "北京", "上海", "广州"],
    })])
    c = L3Clarifier()
    req = await c.clarify("查天气")
    assert req.options == ["厦门", "北京", "上海"]  # 去重 + 最多 3 个


@pytest.mark.asyncio
async def test_clarify_generic_fallback(monkeypatch):
    """没槽位信息 → 泛问一句,但仍然只问一句。"""
    _patch_llm(monkeypatch, [RuntimeError("boom")])
    c = L3Clarifier()
    req = await c.clarify("那个事你懂的")
    assert req is not None
    assert req.source == "template"
    assert "?" in req.question or "?" in req.question


@pytest.mark.asyncio
async def test_clarify_respects_round_limit(monkeypatch):
    """超过轮次上限 → 不再追问,返回 None。"""
    fake = _patch_llm(monkeypatch, [json.dumps({"question": "?", "options": []})])
    c = L3Clarifier(max_rounds=1)
    assert await c.clarify("那个事", round_index=1) is not None
    assert await c.clarify("还是那个事", round_index=2) is None
    assert len(fake.calls) == 1  # 第二轮没再调 LLM


@pytest.mark.asyncio
async def test_clarify_includes_context_in_prompt(monkeypatch):
    fake = _patch_llm(monkeypatch, [json.dumps({"question": "?", "options": []})])
    c = L3Clarifier()
    await c.clarify(
        "那它呢",
        reason="指代不明",
        slots={"city": "厦门"},
        recent_turns=[{"role": "user", "content": "查厦门天气"}],
        skill_names=["DailyReport"],
    )
    prompt = fake.calls[0]["messages"][-1]["content"]
    assert "查厦门天气" in prompt
    assert "DailyReport" in prompt
    assert "city=厦门" in prompt


# ===== Manager 触发条件 =====

class _PlanLLM:
    """只回规划结果的 LLM。"""

    def __init__(self, plan_json):
        self.plan_json = plan_json
        self.calls = 0

    async def chat_with_retry(self, messages, **kw):
        self.calls += 1
        return self.plan_json


def _patch_plan_llm(monkeypatch, plan_json):
    fake = _PlanLLM(plan_json)
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    from infra import llm as llm_mod
    llm_mod._llm_client = None
    return fake


@pytest.mark.asyncio
async def test_manager_l1_clarify_goes_to_l3(monkeypatch):
    """L1 判 clarify(高置信)→ 直接进 L3,不再调 L2。"""
    fake = _patch_plan_llm(monkeypatch, "{}")
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    m.l1_classifier.classify = _fake_classify("clarify", 0.95)
    plan = await m.plan("那个事你懂的")
    assert plan.intent == IntentType.CLARIFY
    assert plan.intent_layer == "L1"
    assert fake.calls == 0  # L2 根本没被调用


@pytest.mark.asyncio
async def test_manager_l2_empty_plan_with_uncertain_l1_goes_to_l3(monkeypatch):
    """L1 低置信 + L2 没规划出任何动作 → 进 L3。"""
    _patch_plan_llm(monkeypatch, json.dumps({
        "plan_detail": "", "selected_skill": "", "tool_tasks": [],
    }))
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    m.l1_classifier.classify = _fake_classify("clarify", 0.4)
    plan = await m.plan("那个事你懂的")
    assert plan.intent == IntentType.CLARIFY
    assert plan.intent_uncertain is True


@pytest.mark.asyncio
async def test_manager_empty_plan_with_confident_task_does_not_clarify(monkeypatch):
    """L1 高置信 task + L2 没动作 → 不追问,直接答。"""
    _patch_plan_llm(monkeypatch, json.dumps({
        "plan_detail": "", "selected_skill": "", "tool_tasks": [],
    }))
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    m.l1_classifier.classify = _fake_classify("task", 0.95)
    plan = await m.plan("那个事你懂的")
    assert plan.intent == IntentType.TASK


@pytest.mark.asyncio
async def test_manager_low_confidence_chitchat_does_not_clarify(monkeypatch):
    """低置信 chitchat 不该追问 —— 闲聊直接答更自然。"""
    _patch_plan_llm(monkeypatch, json.dumps({
        "plan_detail": "", "selected_skill": "", "tool_tasks": [],
    }))
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    m.l1_classifier.classify = _fake_classify("chitchat", 0.4)
    plan = await m.plan("帮我看下今天的情况呗")
    assert plan.intent == IntentType.CHITCHAT


def _fake_classify(label, confidence):
    """造一个 classify() 替身。"""
    from core.llm_judge import IntentDecision
    from core.intent_labels import IntentLabel

    async def _classify(user_input, recent_turns=None, profile=""):
        return IntentDecision(
            label=IntentLabel(label),
            confidence=confidence,
            reason="测试",
        )

    return _classify


# ===== Agent 澄清分支 =====

@pytest.mark.asyncio
async def test_agent_clarification_branch(monkeypatch):
    """意图不明 → 产出澄清事件 + 问题文本,不进 critic。"""
    from agents.manager import IntentType
    from core.agent import Agent

    monkeypatch.setattr(
        "core.agent_base.get_llm_client", lambda: _PlanLLM("{}")
    )
    from infra import llm as llm_mod
    llm_mod._llm_client = None

    agent = Agent(persist_session=False)
    agent.manager.plan = _fake_plan(IntentType.CLARIFY)

    # 澄清器走模板兜底(不依赖 LLM)
    monkeypatch.setattr(
        "core.clarifier.get_clarifier",
        lambda: L3Clarifier(max_rounds=1),
    )
    from infra import llm as llm_mod2
    llm_mod2._llm_client = None
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: _PlanLLM("{}"))

    events = []

    async def on_event(event, payload):
        events.append((event, payload))

    ans = await agent.handle("那个事你懂的", on_event)
    names = [e[0] for e in events]
    assert "clarification_request" in names
    assert "message_final" in names
    assert ans
    # 澄清轮记进了上下文
    assert agent.context.metadata["clarify_round"] == 1
    assert agent.context.metadata["clarify_question"]


@pytest.mark.asyncio
async def test_agent_clarification_round_limit(monkeypatch):
    """连续第二轮澄清 → 不再追问,直接回答。"""
    from agents.manager import IntentType
    from core.agent import Agent

    monkeypatch.setattr(
        "core.agent_base.get_llm_client", lambda: _PlanLLM("{}")
    )
    from infra import llm as llm_mod
    llm_mod._llm_client = None

    agent = Agent(persist_session=False)
    agent.manager.plan = _fake_plan(IntentType.CLARIFY)
    agent.context.metadata["clarify_round"] = 1

    monkeypatch.setattr(
        "core.clarifier.get_clarifier",
        lambda: L3Clarifier(max_rounds=1),
    )
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: _PlanLLM("{}"))

    events = []

    async def on_event(event, payload):
        events.append((event, payload))

    ans = await agent.handle("还是那个事", on_event)
    names = [e[0] for e in events]
    assert "clarification_request" not in names
    assert "message_final" in names
    assert ans


@pytest.mark.asyncio
async def test_agent_normal_turn_resets_clarify_round(monkeypatch):
    """正常推进的轮次 → 澄清计数归零。"""
    from agents.manager import IntentType
    from core.agent import Agent

    monkeypatch.setattr(
        "core.agent_base.get_llm_client", lambda: _PlanLLM("{}")
    )
    from infra import llm as llm_mod
    llm_mod._llm_client = None

    agent = Agent(persist_session=False)
    agent.manager.plan = _fake_plan(IntentType.CHITCHAT)
    agent.context.metadata["clarify_round"] = 1

    async def on_event(event, payload):
        pass

    with pytest.raises(Exception):
        # 直答分支要调 orchestrator(无 LLM 会失败),只关心计数是否归零
        await agent.handle("你好", on_event)

    assert agent.context.metadata["clarify_round"] == 0


def _fake_plan(intent):
    """造一个 plan() 替身。"""
    from agents.manager import PlanResult

    async def _plan(
        user_input, context=None, session_id="", profile_block=""
    ):
        return PlanResult(
            intent=intent,
            intent_detail="测试",
            selected_skill=None,
            tool_tasks=[],
        )

    return _plan


# ===== 单例 =====

def test_clarifier_singleton():
    reset_clarifier()
    c1 = get_clarifier()
    assert get_clarifier() is c1
    reset_clarifier()
    assert get_clarifier() is not c1


# ===== WebSocket 端到端 =====

@pytest.mark.asyncio
async def test_ws_clarification_event(monkeypatch):
    """意图不明 → WebSocket 上收到 clarification_request 事件。"""
    from unittest.mock import MagicMock

    from backend.websocket_handler import dispatcher
    from backend.session import sessions
    from backend import websocket_handler as wsh

    conn_id = "test-clarify-cid"

    # LLM 替身:第 1 次是 L1 分类,第 2 次是澄清问题
    class _StubLLM:
        def __init__(self):
            self.model = "fake"
            self.default_temperature = 0.0
            self.responses = [
                json.dumps({
                    "label": "clarify", "confidence": 0.95, "reason": "信息不足",
                }),
                json.dumps({
                    "question": "你想查哪个城市的天气?",
                    "options": ["厦门", "北京"],
                }),
            ]

        async def chat_with_retry(self, messages, **kw):
            return self.responses.pop(0) if self.responses else "{}"

        async def _complete(self, messages, temperature=None):
            return "ok"

        async def _stream(self, messages, temperature=None):
            for ch in "ok":
                yield ch

        async def stream(self, messages, **kw):
            async for c in self._stream(messages):
                yield c

    import infra.llm as llm_mod
    import core.agent_base as cab
    stub = _StubLLM()  # 共享同一个实例:第 1 次调用是 L1,第 2 次是澄清
    llm_mod._llm_client = None
    # 必须记住原函数并在 finally 里还原 ——
    # 直接赋值会**永久**改写模块全局,后面所有测试拿到的都是这个 stub,
    # 于是"真 LLM 通道"其实从来没被执行过,测试却全绿。
    llm_get_orig = llm_mod.get_llm_client
    cab_get_orig = cab.get_llm_client
    llm_mod.get_llm_client = lambda: stub
    cab.get_llm_client = lambda: stub

    captured: list = []
    original_push = wsh.push_event

    async def fake_push(cid, event, payload):
        captured.append({"event": event, "payload": payload})

    wsh.push_event = fake_push
    issued_client_id = None
    try:
        sub = MagicMock()
        sub.topic = f"init/{conn_id}"
        await dispatcher(sub, {"client_id": conn_id})
        connected = [e for e in captured if e["event"] == "connected"]
        assert connected, captured
        issued_client_id = connected[0]["payload"]["client_id"]

        captured.clear()
        sub.topic = f"chat/{conn_id}"
        await dispatcher(sub, {"content": "那个事你懂的"})

        clarification = [
            e for e in captured if e["event"] == "clarification_request"
        ]
        assert clarification, f"应有 clarification_request 事件: {captured}"
        payload = clarification[0]["payload"]
        assert payload["question"] == "你想查哪个城市的天气?"
        assert payload["options"] == ["厦门", "北京"]

        # 问题也要以最终消息的形式送达前端
        finals = [e for e in captured if e["event"] == "message_final"]
        assert finals, captured
        assert "哪个城市" in finals[0]["payload"]["content"]
    finally:
        wsh.push_event = original_push
        llm_mod.get_llm_client = llm_get_orig
        cab.get_llm_client = cab_get_orig
        llm_mod._llm_client = None
        if issued_client_id:
            await sessions.destroy(issued_client_id)
