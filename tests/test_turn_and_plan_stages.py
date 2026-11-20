"""`TurnContext` 与两个"上帝函数"的分层边界。

## 为什么需要这组测试

`docs/19` §05 / §06 的验收条件是"每个 stage 有独立测试"和
"`handle` 主体降到 60 行内"。这两条都不是靠肉眼保证的 ——
重构完谁都能再往里塞十行，一年后又变回 500 行。

所以这里锁三件事：

1. **结构棘轮**：`handle` 的行数有上限，且必须真的调那几个 stage 方法。
   超了或者绕过去直接内联，测试就红。
2. **每一层自己能被单测**：`_guard_plan` / `_l1_plan` 不必跑完整 `plan()`；
   `TurnContext` 不必起 Agent。
3. **`TurnContext` 不是共享可变默认值** —— `pending_async` 用
   `default_factory`，否则两轮会把事件塞进同一个 list，事件顺序全乱。

第 3 条没有测试就极难发现：症状是"偶发事件乱序"，本地复现不了。
"""

import ast
import pathlib

import pytest

from core.intent_labels import IntentLabel
from core.llm_judge import IntentDecision
from core.turn_context import TurnContext

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ============================================================
# 1. TurnContext 本身
# ============================================================

def test_pending_async_is_per_instance():
    """两个 tc 不能共享同一个队列 —— 共享了事件会串轮。"""
    a = TurnContext(user_input="a", start=0.0, turn_start=0.0)
    b = TurnContext(user_input="b", start=0.0, turn_start=0.0)
    a.pending_async.append("x")
    assert b.pending_async == []


def test_default_emit_is_noop():
    tc = TurnContext(user_input="x", start=0.0, turn_start=0.0)
    tc.emit("thinking", {"stage": "x"})  # 不抛即可


def test_skill_accessors_are_safe_without_plan():
    tc = TurnContext(user_input="x", start=0.0, turn_start=0.0)
    assert tc.skill is None
    assert tc.skill_name is None

    class _Plan:
        selected_skill = type("S", (), {"name": "demo"})()

    tc.plan = _Plan()
    assert tc.skill_name == "demo"


# ============================================================
# 2. 结构棘轮：handle 必须又小又只做编排
# ============================================================

def _agent_method(name):
    src = (ROOT / "core" / "agent.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"core/agent.py 里找不到 {name}")


def test_handle_stays_small():
    """`handle` 只表达"这一轮怎么走"。超了就说明又有逻辑内联进来了。"""
    node = _agent_method("handle")
    lines = node.end_lineno - node.lineno + 1
    assert lines <= 60, f"handle 回到 {lines} 行了，把新逻辑拆成 stage 方法"


def test_handle_delegates_to_stages():
    """必须真的调用那几个 stage —— 否则行数达标也只是"挪走了注释"。"""
    node = _agent_method("handle")
    called = {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for stage in (
        "_begin_turn",
        "_plan_turn",
        "_handle_quick_intents",
        "_execute_goal_loop",
        "_synthesize",
        "_persist_turn",
        "_end_turn",
    ):
        assert stage in called, f"handle 没有调用 {stage}"


def test_every_stage_is_its_own_method():
    """每个 stage 都要是独立方法且不超过 80 行 —— 否则只是换了个大函数。"""
    for name in (
        "_plan_turn",
        "_execute_goal_loop",
        "_run_plan_tasks",
        "_replan",
        "_synthesize",
        "_persist_turn",
        "_end_turn",
    ):
        node = _agent_method(name)
        lines = node.end_lineno - node.lineno + 1
        assert lines <= 80, f"{name} 有 {lines} 行，再拆"


def test_manager_plan_stays_small():
    src = (ROOT / "agents" / "manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "plan":
            lines = node.end_lineno - node.lineno + 1
            assert lines <= 70, f"ManagerAgent.plan 回到 {lines} 行了"
            return
    raise AssertionError("找不到 ManagerAgent.plan")


# ============================================================
# 3. L0 / L1 各层可单独测
# ============================================================

@pytest.mark.asyncio
async def test_guard_stage_handles_empty_input():
    """L0 只认结构事实：空输入 → 澄清，不需要 LLM。"""
    from agents.manager import IntentType, ManagerAgent

    decided = ManagerAgent()._guard_plan("   ", None)
    assert decided is not None
    assert decided.intent == IntentType.CLARIFY
    assert decided.intent_layer == "guard"


@pytest.mark.asyncio
async def test_guard_stage_leaves_normal_input_to_l1():
    """正常输入 L0 不插手 —— 这条是防"又加回猜话的规则"。"""
    from agents.manager import ManagerAgent

    assert ManagerAgent()._guard_plan("帮我查一下厦门今天的天气", None) is None


@pytest.mark.asyncio
async def test_l1_low_confidence_escalates_to_l2(monkeypatch):
    """低置信不定案，但要把判断带下去当 L2 的强约束。"""
    from agents import manager as mgr_mod
    from agents.manager import ManagerAgent

    decision = IntentDecision(label=IntentLabel.CHITCHAT, confidence=0.3, reason="含糊")

    async def _fake(self, user_input, context=None):
        return decision

    monkeypatch.setattr(mgr_mod.ManagerAgent, "_l1_classify", _fake)
    got_decision, decided = await ManagerAgent()._l1_plan("嗯", None)
    assert got_decision is decision
    assert decided is None


@pytest.mark.asyncio
async def test_l1_high_confidence_non_task_settles(monkeypatch):
    from agents import manager as mgr_mod
    from agents.manager import IntentType, ManagerAgent

    decision = IntentDecision(
        label=IntentLabel.CHITCHAT, confidence=0.95, reason="在打招呼"
    )

    async def _fake(self, user_input, context=None):
        return decision

    monkeypatch.setattr(mgr_mod.ManagerAgent, "_l1_classify", _fake)
    _, decided = await ManagerAgent()._l1_plan("你好呀", None)
    assert decided is not None
    assert decided.intent == IntentType.CHITCHAT
    assert decided.intent_layer == "L1"


@pytest.mark.asyncio
async def test_l1_high_confidence_task_goes_to_l2(monkeypatch):
    """task 高置信也不在 L1 定案 —— 意图归 L1，动作归 L2。"""
    from agents import manager as mgr_mod
    from agents.manager import ManagerAgent

    decision = IntentDecision(label=IntentLabel.TASK, confidence=0.95)

    async def _fake(self, user_input, context=None):
        return decision

    monkeypatch.setattr(mgr_mod.ManagerAgent, "_l1_classify", _fake)
    _, decided = await ManagerAgent()._l1_plan("帮我把这份表格转成 csv", None)
    assert decided is None


def test_fallback_plan_marks_uncertainty():
    """L2 也没产出时，兜底要如实标"不确定"，不能假装很确定。"""
    from agents.manager import IntentType, ManagerAgent

    decision = IntentDecision(label=IntentLabel.TASK, confidence=0.4)
    plan = ManagerAgent._fallback_plan(decision)
    assert plan.intent == IntentType.TASK
    assert plan.tool_tasks == []
    assert plan.intent_uncertain is True

    plan = ManagerAgent._fallback_plan(None)
    assert plan.intent_uncertain is False
