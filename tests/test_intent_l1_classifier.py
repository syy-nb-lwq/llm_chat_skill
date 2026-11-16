"""L1 闭集判别器测试。

覆盖:
  - 闭集定义与 schema 的自洽性
  - 输出的解析与校验(非法 label / 缺 confidence / 非法 JSON)
  - 失败降级(LLM 挂掉不崩)
  - L0 守卫层与 L1 的分工(L0 命中不调 LLM)
  - Manager 四层编排(L0 定案 / L1 定案 / L1 低置信升级 L2)
"""
import json

import pytest

from core.intent_labels import (
    INTENT_RESULT_SCHEMA,
    LABEL_DEFINITIONS,
    IntentLabel,
    TaskKind,
    build_intent_prompt,
    build_system_prompt,
    parse_label,
)
from core.llm_judge import IntentDecision, L1Classifier


# ===== 闭集自洽性 =====

def test_closed_set_has_six_labels():
    assert len(IntentLabel) == 6
    assert {l.value for l in IntentLabel} == {
        "chitchat", "teach", "manage", "retry", "task", "clarify",
    }


def test_task_kinds_are_separate_from_intents():
    """天气/搜索等是任务子类,不是意图。"""
    assert len(TaskKind) == 6
    assert not set(l.value for l in IntentLabel) & set(k.value for k in TaskKind)


def test_schema_enum_matches_closed_set():
    assert INTENT_RESULT_SCHEMA["properties"]["label"]["enum"] == [
        l.value for l in IntentLabel
    ]
    assert set(INTENT_RESULT_SCHEMA["required"]) == {"label", "confidence", "reason"}


def test_every_label_has_definition_and_examples():
    labels = {d["label"] for d in LABEL_DEFINITIONS}
    assert labels == {l.value for l in IntentLabel}
    for spec in LABEL_DEFINITIONS:
        assert spec["definition"].strip()
        assert spec["examples"], f"{spec['label']} 缺正例"
        assert spec["confusable"], f"{spec['label']} 缺易混反例"


def test_parse_label():
    assert parse_label("task") is IntentLabel.TASK
    assert parse_label("TASK") is IntentLabel.TASK
    assert parse_label("  chitchat ") is IntentLabel.CHITCHAT
    assert parse_label("weather") is None
    assert parse_label(None) is None
    assert parse_label(123) is None


def test_prompt_contains_definitions_and_input():
    """判据在系统提示词里,待判内容在用户提示词里。"""
    system = build_system_prompt()
    for label in IntentLabel:
        assert label.value in system
    # 易混反例也要进提示词
    assert "查看北京天气" in system
    assert "只输出一个 JSON 对象" in system

    prompt = build_intent_prompt("查一下厦门天气")
    assert "查一下厦门天气" in prompt
    # 用户提示词里不重复系统提示词
    assert "标签定义" not in prompt


def test_prompt_includes_recent_turns_and_profile():
    prompt = build_intent_prompt(
        "那它呢",
        recent_turns=[{"role": "user", "content": "查厦门天气"}],
        profile="用户偏好简短回答",
    )
    assert "查厦门天气" in prompt
    assert "用户偏好简短回答" in prompt


# ===== 解析与校验 =====

class _FakeLLM:
    """最小 LLM 替身。"""

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
    # BaseAgent.__init__ 里 `from infra.llm import get_llm_client`,
    # 该名字绑定在 core.agent_base 模块里。要同时 patch 两边。
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    from infra import llm as _llm_mod
    _llm_mod._llm_client = None
    return fake


@pytest.mark.asyncio
async def test_classify_valid(monkeypatch):
    fake = _patch_llm(monkeypatch, [json.dumps({
        "label": "task",
        "confidence": 0.93,
        "slots": {"city": "厦门"},
        "needs_clarify": False,
        "reason": "查询天气",
    })])
    c = L1Classifier()
    d = await c.classify("查一下厦门天气")
    assert d is not None
    assert d.label is IntentLabel.TASK
    assert d.confidence == pytest.approx(0.93)
    assert d.slots == {"city": "厦门"}
    assert d.layer == "L1"
    # temperature 必须为 0(分类要确定性)
    assert fake.calls[0]["kw"]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_classify_rejects_invalid_label(monkeypatch):
    _patch_llm(monkeypatch, [json.dumps({
        "label": "weather", "confidence": 0.9, "reason": "x",
    })])
    c = L1Classifier()
    assert await c.classify("查天气") is None


@pytest.mark.asyncio
async def test_classify_rejects_missing_confidence(monkeypatch):
    _patch_llm(monkeypatch, [json.dumps({
        "label": "task", "reason": "x",
    })])
    c = L1Classifier()
    d = await c.classify("查天气")
    assert d is not None
    assert d.confidence == 0.0


@pytest.mark.asyncio
async def test_classify_clamps_confidence(monkeypatch):
    _patch_llm(monkeypatch, [json.dumps({
        "label": "task", "confidence": 5, "reason": "x",
    })])
    c = L1Classifier()
    d = await c.classify("查天气")
    assert d.confidence == 1.0


@pytest.mark.asyncio
async def test_classify_handles_garbage(monkeypatch):
    _patch_llm(monkeypatch, ["这不是 JSON"])
    c = L1Classifier()
    assert await c.classify("查天气") is None


@pytest.mark.asyncio
async def test_classify_degrades_on_llm_error(monkeypatch):
    """LLM 挂掉 → 返回 None,不能抛异常。"""
    _patch_llm(monkeypatch, [RuntimeError("boom")])
    c = L1Classifier(max_retries=1)
    assert await c.classify("查天气") is None


@pytest.mark.asyncio
async def test_classify_empty_input(monkeypatch):
    _patch_llm(monkeypatch, ["{}"])
    c = L1Classifier()
    assert await c.classify("   ") is None


def test_is_confident():
    c = L1Classifier(threshold=0.75)
    assert c.is_confident(IntentDecision(IntentLabel.TASK, 0.8)) is True
    assert c.is_confident(IntentDecision(IntentLabel.TASK, 0.5)) is False
    assert c.is_confident(None) is False


# ===== 结构守卫 / L1 分工 =====
#
# 旧版这里断言"你好/列出所有技能/以后按三段式来" 要由 L0 规则定案。
# **那套规则已删除**——真机基线: 规则+LLM 94.7% vs 只有 LLM 97.7%。
# 现在这些输入全部走 L1。

def test_guard_does_not_decide_any_intent():
    """守卫对非空输入一律不定案——包括"你好"这种看似明显的。

    "看起来明显"不是判据:真机基线里规则正是靠"看起来明显"
    把 10 条判错的。
    """
    from core.intent_detector import IntentDetector

    d = IntentDetector()
    for text in ("你好", "列出所有技能", "以后做日报就按三段式来",
                 "重新来一次", "查看北京天气",
                 "遇到雨天要带伞", "教我怎么用这个工具"):
        r = d.detect(text)
        assert r.label is None, f"{text!r} 被守卫判成了 {r.label}"
        assert r.need_llm is True


def test_guard_only_decides_empty_input():
    from core.intent_detector import IntentDetector

    r = IntentDetector().detect("   ")
    assert r.label is IntentLabel.CLARIFY
    assert r.need_llm is False


@pytest.mark.asyncio
async def test_manager_chitchat_goes_through_l1(monkeypatch):
    """你好 → 也要问 L1(不再由规则抢走)。"""
    fake = _patch_llm(monkeypatch, [json.dumps({
        "label": "chitchat", "confidence": 0.95, "reason": "问候",
    })])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("你好")
    assert plan.intent == IntentType.CHITCHAT
    assert plan.intent_layer == "L1"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_manager_l1_decides_chitchat(monkeypatch):
    """L1 高置信判为 chitchat → 不进 L2。"""
    fake = _patch_llm(monkeypatch, [json.dumps({
        "label": "chitchat", "confidence": 0.95, "reason": "只是打个招呼",
    })])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("帮我看下今天的情况呗")
    assert plan.intent == IntentType.CHITCHAT
    assert plan.intent_layer == "L1"
    # 只调了 1 次 LLM(L1),没进 L2
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_manager_l1_low_confidence_escalates_to_l2(monkeypatch):
    """低置信 → 带着 L1 的判断升级 L2 兜底,不放弃。"""
    fake = _patch_llm(monkeypatch, [
        json.dumps({
            "label": "chitchat", "confidence": 0.4, "reason": "不太确定",
        }),
        json.dumps({
            "plan_detail": "查天气",
            "selected_skill": "",
            "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
        }),
    ])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("帮我查一下厦门今天的天气怎么样")
    assert plan.intent == IntentType.TASK
    assert plan.intent_uncertain is True
    assert len(plan.tool_tasks) == 1
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_manager_l1_clarify_goes_to_l3(monkeypatch):
    """高置信 clarify → 直接进 L3 澄清,不再调 L2(I-03)。"""
    fake = _patch_llm(monkeypatch, [
        json.dumps({
            "label": "clarify", "confidence": 0.9, "reason": "信息不足",
        }),
    ])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("那个事你懂的")
    assert plan.intent == IntentType.CLARIFY
    assert len(fake.calls) == 1  # 只调了 L1,没进 L2


@pytest.mark.asyncio
async def test_manager_low_confidence_clarify_escalates_to_l2(monkeypatch):
    """低置信 clarify → 宁可多调一次工具,也不要漏答(升级 L2 兜底)。"""
    fake = _patch_llm(monkeypatch, [
        json.dumps({
            "label": "clarify", "confidence": 0.4, "reason": "不太确定",
        }),
        json.dumps({
            "plan_detail": "直接回答",
            "selected_skill": "",
            "tool_tasks": [],
        }),
    ])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("那个事你懂的")
    # L2 也没规划出动作 + L1 低置信 → 仍然进 L3
    assert plan.intent == IntentType.CLARIFY
    assert len(fake.calls) == 2  # 进了 L2


@pytest.mark.asyncio
async def test_manager_l2_prompt_carries_intent_hint(monkeypatch):
    """L1 的标签要作为强约束注入 L2 的提示词。"""
    fake = _patch_llm(monkeypatch, [
        json.dumps({
            "label": "task", "confidence": 0.95, "reason": "查询天气",
            "slots": {"city": "厦门"},
        }),
        json.dumps({
            "plan_detail": "查天气",
            "selected_skill": "",
            "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
        }),
    ])
    from agents.manager import ManagerAgent

    m = ManagerAgent()
    await m.plan("帮我查一下厦门今天的天气")

    l2_prompt = fake.calls[1]["messages"][-1]["content"]
    assert "【已判定的意图】" in l2_prompt
    assert "task" in l2_prompt
    assert "厦门" in l2_prompt


@pytest.mark.asyncio
async def test_manager_l1_failure_degrades(monkeypatch):
    """L1 挂掉 → 退到 L2,链路不崩。"""
    fake = _patch_llm(monkeypatch, [
        # L1 内部会重试 2 次,两次都失败
        RuntimeError("boom"),
        RuntimeError("boom"),
        json.dumps({
            "plan_detail": "查天气",
            "selected_skill": "",
            "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
        }),
    ])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("帮我查一下厦门今天的天气")
    assert plan.intent == IntentType.TASK
    assert plan.intent_uncertain is True
    assert len(plan.tool_tasks) == 1
    assert len(fake.calls) == 3  # L1 两次 + L2 一次


@pytest.mark.asyncio
async def test_manager_l1_can_be_disabled(monkeypatch):
    """L1 可关闭(退回改造前的行为)。"""
    fake = _patch_llm(monkeypatch, [
        json.dumps({
            "plan_detail": "查天气",
            "selected_skill": "",
            "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
        }),
    ])
    from agents.manager import ManagerAgent

    m = ManagerAgent()
    m._l1_enabled = False
    plan = await m.plan("帮我查一下厦门今天的天气")
    assert len(fake.calls) == 1
    assert len(plan.tool_tasks) == 1


@pytest.mark.asyncio
async def test_manager_retry_without_context(monkeypatch):
    """重试但没有上下文 → 不能瞎重试。(意图现在由 L1 判)"""
    _patch_llm(monkeypatch, [json.dumps({
        "label": "retry", "confidence": 0.95, "reason": "要重做",
    })])
    from agents.manager import ManagerAgent, IntentType

    m = ManagerAgent()
    plan = await m.plan("重新来一次")
    assert plan.intent == IntentType.TASK
    assert plan.is_retry is True
    assert plan.tool_tasks == []
