"""意图判别层三组件的独立测试 —— 不需要构造 ManagerAgent。

这是 §06 拆分的意义：判别逻辑可以单独喂输入、断言输出，
不用带上 skill_store / memory_store / contextvar 这 5 个依赖。
"""
from __future__ import annotations


from agents.intents import plan_from_label, recent_turns
from agents.manager_types import IntentType, PlanResult
from core.context import Context
from core.intent_labels import IntentLabel


def test_plan_from_label_chitchat():
    r = plan_from_label(IntentLabel.CHITCHAT, detail="打招呼", layer="L1", confidence=0.9)
    assert isinstance(r, PlanResult)
    assert r.intent == IntentType.CHITCHAT
    assert r.intent_layer == "L1"
    assert r.intent_confidence == 0.9
    assert r.tool_tasks == []


def test_plan_from_label_task_returns_none_for_l2():
    """TASK 不能直接定案，必须交给 L2 规划。"""
    assert plan_from_label(IntentLabel.TASK, detail="查天气") is None


def test_plan_from_label_retry_needs_context():
    no_ctx = plan_from_label(IntentLabel.RETRY, detail="重试")
    assert no_ctx.intent == IntentType.TASK
    assert no_ctx.is_retry is True
    assert "没有历史记录" in no_ctx.intent_detail

    ctx = Context()
    ctx.add_user_message("上一轮的请求")
    with_ctx = plan_from_label(
        IntentLabel.RETRY, detail="重试", context=ctx
    )
    assert with_ctx.intent == IntentType.RETRY
    assert with_ctx.need_llm is True


def test_plan_from_label_manage_and_clarify():
    m = plan_from_label(IntentLabel.MANAGE)
    assert m.intent == IntentType.MANAGER
    assert m.need_llm is False

    c = plan_from_label(IntentLabel.CLARIFY)
    assert c.intent == IntentType.CLARIFY
    assert c.need_llm is False


def test_recent_turns_handles_empty_and_none():
    assert recent_turns(None) == []
    assert recent_turns(Context()) == []


def test_recent_turns_keeps_only_user_assistant():
    ctx = Context()
    ctx.add_user_message("第一句")
    ctx.add_assistant_message("回答")
    rows = recent_turns(ctx, limit=1)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert all(set(r) == {"role", "content"} for r in rows)
