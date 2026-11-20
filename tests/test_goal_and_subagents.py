"""目标循环 + 子智能体。

## 这条链路解决什么问题

原来 `handle()` 是直线：规划一次 → 执行一次 → 回答 → 结束。
所以「造工具 → 用它 → 再算」这种任务，第一次规划没想全就**没有第二次机会**。

## 这里钉住的四道闸（缺一个就出事故）

1. 默认只跑一轮（`goal_done` 默认 True）—— 简单问答不能变慢
2. 轮次上限 / 时长预算 —— 不能无休止烧钱
3. 无进展检测 —— 同一动作 + 无新结果，立刻停，别把预算烧在重复上
4. 失败/未完成必须**出现在给模型和用户的文字里**，不能被吞掉
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

import pytest

from core.goal_runner import (
    STOP_BUDGET,
    STOP_CLARIFY,
    STOP_DISABLED,
    STOP_GOAL_DONE,
    STOP_MAX_ROUNDS,
    STOP_NO_PROGRESS,
    action_signature,
    merge_results,
    run_goal_loop,
    summarize_results,
)
from core.subagents import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    SubAgentPool,
    SubAgentReport,
    SubAgentTask,
)


# ============================================================
# 测试替身
# ============================================================

@dataclass
class FakePlan:
    tool_tasks: List[Dict[str, Any]] = field(default_factory=list)
    goal_done: bool = True
    remaining_note: str = ""
    intent_detail: str = "plan"
    intent: Any = None


@dataclass
class FakeResult:
    success: bool = True
    data: Any = None
    error: str = ""


def _plan(done=True, tasks=None, note=""):
    return FakePlan(
        tool_tasks=tasks if tasks is not None else [{"id": "t1", "type": "x", "params": {}}],
        goal_done=done,
        remaining_note=note,
    )


# ============================================================
# 1. 默认行为：只跑一轮
# ============================================================

@pytest.mark.asyncio
async def test_default_runs_single_round():
    """`goal_done=True` 是默认 —— 简单问答行为和旧版完全一样。"""
    calls = []

    async def execute(plan):
        calls.append(plan)
        return {"t1": FakeResult(data={"v": 1})}

    result = await run_goal_loop(
        first_plan=_plan(done=True),
        plan_fn=lambda prior: None,
        execute_fn=execute,
    )
    assert result.rounds == 1
    assert result.stop_reason == STOP_GOAL_DONE
    assert result.completed is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_loop_disabled_runs_single_round_even_if_not_done():
    """关掉循环 → 即使规划层说没完也停在第一轮（可回退）。"""
    async def execute(plan):
        return {"t1": FakeResult()}

    result = await run_goal_loop(
        first_plan=_plan(done=False),
        plan_fn=lambda prior: _plan(done=True),
        execute_fn=execute,
        enabled=False,
    )
    assert result.rounds == 1
    assert result.stop_reason == STOP_DISABLED


# ============================================================
# 2. 多轮
# ============================================================

@pytest.mark.asyncio
async def test_runs_multiple_rounds_when_not_done():
    """规划层说没完 → 带着已知结果再规划一轮，直到它说完了。"""
    rounds = []
    priors = []

    async def execute(plan):
        rounds.append(plan)
        return {f"t{len(rounds)}": FakeResult(data={"round": len(rounds)})}

    async def replan(prior: str):
        priors.append(prior)
        # 第 2 轮之后说完成
        return _plan(done=len(priors) >= 2, tasks=[{"id": "x", "type": "y", "params": {}}])

    result = await run_goal_loop(
        first_plan=_plan(done=False),
        plan_fn=replan,
        execute_fn=execute,
    )
    assert result.rounds == 3          # 第1轮 + 2次重规划
    assert result.stop_reason == STOP_GOAL_DONE
    # 已知结果要传给下一轮（否则会重复查同样的东西）
    assert priors[0] and "round" in priors[0]


@pytest.mark.asyncio
async def test_max_rounds_stops():
    """轮次上限必须硬停 —— 这是防烧钱的第一道闸。"""
    async def execute(plan):
        return {"t": FakeResult(data={"x": 1})}

    async def replan(prior):
        return _plan(done=False)

    result = await run_goal_loop(
        first_plan=_plan(done=False),
        plan_fn=replan,
        execute_fn=execute,
        max_rounds=3,
    )
    assert result.rounds == 3
    assert result.stop_reason == STOP_MAX_ROUNDS
    assert result.completed is False
    assert "轮次" in result.explanation


@pytest.mark.asyncio
async def test_budget_stops():
    """总时长预算 —— 第二道闸。"""
    async def execute(plan):
        await asyncio.sleep(0.12)
        return {"t": FakeResult(data={"x": 1})}

    async def replan(prior):
        return _plan(done=False)

    result = await run_goal_loop(
        first_plan=_plan(done=False),
        plan_fn=replan,
        execute_fn=execute,
        max_rounds=100,
        budget_s=0.2,
    )
    assert result.stop_reason in (STOP_BUDGET, STOP_MAX_ROUNDS)
    assert result.rounds < 100


# ============================================================
# 3. 无进展检测（最容易出事的地方）
# ============================================================

@pytest.mark.asyncio
async def test_no_progress_detected():
    """连续两轮同一动作 + 没有任何结果 → 立刻停，不烧完预算。"""
    same = [{"id": "t1", "type": "search", "params": {"q": "x"}}]

    async def execute(plan):
        return {}          # 什么也没拿到

    async def replan(prior):
        return _plan(done=False, tasks=same)

    result = await run_goal_loop(
        first_plan=_plan(done=False, tasks=same),
        plan_fn=replan,
        execute_fn=execute,
        max_rounds=10,
    )
    assert result.stop_reason == STOP_NO_PROGRESS
    assert result.rounds < 10
    assert "打转" in result.explanation or "同一件" in result.explanation


def test_action_signature_stable():
    a = [{"type": "x", "params": {"b": 2, "a": 1}}]
    b = [{"type": "x", "params": {"a": 1, "b": 2}}]
    assert action_signature(a) == action_signature(b)
    assert action_signature(a) != action_signature([{"type": "x", "params": {"a": 2}}])


# ============================================================
# 4. 结果合并与摘要
# ============================================================

def test_merge_results_keeps_all():
    target = {"t1": "a"}
    merge_results(target, {"t2": "b"})
    assert set(target) == {"t1", "t2"}


def test_merge_results_handles_id_collision():
    """多轮里 task id 会重复（都是 t1），不能互相覆盖丢数据。"""
    r1, r2 = FakeResult(data={"n": 1}), FakeResult(data={"n": 2})
    target = {}
    merge_results(target, {"t1": r1})
    merge_results(target, {"t1": r2})
    assert len(target) == 2
    assert {id(v) for v in target.values()} == {id(r1), id(r2)}


def test_summarize_includes_failures():
    """失败也要写进"已知信息" —— "这条路不通"本身就是有价值的信息。"""
    text = summarize_results({
        "t1": FakeResult(success=False, error="接口 404"),
        "t2": FakeResult(success=True, data={"ok": 1}),
    })
    assert "失败" in text and "404" in text
    assert "成功" in text


def test_summarize_truncates():
    big = {"blob": "x" * 5000}
    text = summarize_results({"t1": FakeResult(data=big)})
    assert len(text) < 1200


# ============================================================
# 5. 澄清中断
# ============================================================

@pytest.mark.asyncio
async def test_clarify_stops_loop():
    """中途发现缺关键信息 → 停下来问，不要继续瞎跑。"""
    async def execute(plan):
        return {"t": FakeResult(data={"x": 1})}

    async def replan(prior):
        return FakePlan(tool_tasks=[], goal_done=False, intent="clarify")

    result = await run_goal_loop(
        first_plan=_plan(done=False),
        plan_fn=replan,
        execute_fn=execute,
        is_clarify=lambda p: p.intent == "clarify",
    )
    assert result.stop_reason == STOP_CLARIFY


# ============================================================
# 6. 子智能体：状态与报告
# ============================================================

def test_report_counts_failures():
    """**失败必须计数** —— 不允许"三个子任务两个失败但说得像全成了"。"""
    report = SubAgentReport(tasks=[
        SubAgentTask(goal="a", status=STATUS_DONE),
        SubAgentTask(goal="b", status=STATUS_FAILED),
        SubAgentTask(goal="c", status=STATUS_TIMEOUT),
    ])
    assert (report.done, report.failed, report.timeout) == (1, 1, 1)
    assert report.all_ok is False
    line = report.summary_line()
    assert "成功 1" in line and "失败 1" in line and "超时 1" in line


def test_report_text_warns_about_failures():
    report = SubAgentReport(tasks=[
        SubAgentTask(goal="a", status=STATUS_DONE, result="结论A"),
        SubAgentTask(goal="b", status=STATUS_FAILED, error="接口挂了"),
    ])
    text = report.as_text()
    assert "结论A" in text
    assert "接口挂了" in text
    # 必须明确要求"如实说明"，否则模型会说成全成了
    assert "不要把失败说成成功" in text


def test_task_defaults():
    t = SubAgentTask(goal="查一下 2026 年的政策")
    assert t.status == "pending"
    assert t.id.startswith("sub_")
    assert t.name          # 自动取目标前几个字当名字
    assert t.to_dict()["goal"].startswith("查一下")


# ============================================================
# 7. 子智能体：边界
# ============================================================

@pytest.mark.asyncio
async def test_depth_limit_blocks_spawning():
    """深度到顶 → 明确拒绝，而不是悄悄降级。"""
    pool = SubAgentPool(max_depth=1)
    report = await pool.run_many([{"goal": "x"}], depth=2)
    assert report.tasks[0].status == STATUS_FAILED
    assert "深度上限" in report.tasks[0].error


@pytest.mark.asyncio
async def test_timeout_marks_task(monkeypatch):
    """单个子任务超时要标记 timeout 并回收，不能一直挂着。"""
    pool = SubAgentPool(timeout_s=0.05)

    async def _slow(task, on_event=None):
        await asyncio.sleep(10)

    monkeypatch.setattr(pool, "_execute", _slow)
    report = await pool.run_many([{"goal": "慢任务"}], depth=1)
    assert report.tasks[0].status == STATUS_TIMEOUT
    assert "超时" in report.tasks[0].error or "未完成" in report.tasks[0].error


@pytest.mark.asyncio
async def test_concurrency_respects_limit(monkeypatch):
    """并发上限要真的生效（否则一次派 20 个会把机器打满）。"""
    pool = SubAgentPool(max_concurrent=2, timeout_s=5)
    running = 0
    peak = 0

    async def _fake(task, on_event=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        task.status = STATUS_DONE
        task.result = "ok"

    monkeypatch.setattr(pool, "_execute", _fake)
    await pool.run_many([{"goal": f"t{i}"} for i in range(6)], depth=1)
    assert peak <= 2, f"并发峰值 {peak} 超过上限 2"


@pytest.mark.asyncio
async def test_empty_specs_returns_empty_report():
    pool = SubAgentPool()
    report = await pool.run_many([], depth=1)
    assert report.tasks == []


@pytest.mark.asyncio
async def test_status_is_queryable_while_running(monkeypatch):
    """主智能体要能"探测"子智能体的状态 —— 运行中也能查到。"""
    pool = SubAgentPool(timeout_s=5)
    started = asyncio.Event()

    async def _fake(task, on_event=None):
        started.set()
        task.progress = "干活中"
        await asyncio.sleep(0.15)
        task.status = STATUS_DONE
        task.result = "完成"

    monkeypatch.setattr(pool, "_execute", _fake)
    fut = asyncio.ensure_future(pool.run_many([{"goal": "x", "name": "X"}], depth=1))
    await started.wait()
    snapshot = pool.status()
    assert snapshot and snapshot[0]["status"] in ("running", "done")
    assert pool.active_count() >= 1
    await fut
    assert pool.status()[0]["status"] == STATUS_DONE


@pytest.mark.asyncio
async def test_events_are_emitted(monkeypatch):
    """spawned / progress / done 三类事件要发给前端。"""
    pool = SubAgentPool(timeout_s=5)

    async def _fake(task, on_event=None):
        await pool._emit(on_event, "subagent_progress", task)
        task.status = STATUS_DONE
        task.result = "ok"

    monkeypatch.setattr(pool, "_execute", _fake)

    events = []

    async def on_event(name, payload):
        events.append(name)

    await pool.run_many([{"goal": "x"}], depth=1, on_event=on_event)
    assert "subagent_spawned" in events
    assert "subagent_done" in events


# ============================================================
# 8. 「准备」不等于「完成」—— 实机踩过的坑
# ============================================================

def test_is_only_preparatory():
    from core.goal_runner import is_only_preparatory

    assert is_only_preparatory([{"type": "create_tool"}]) is True
    assert is_only_preparatory([{"type": "create_tool"}, {"type": "write_script"}]) is True
    # 有实质动作就不算"只做了准备"
    assert is_only_preparatory([{"type": "create_tool"}, {"type": "bmi_calc"}]) is False
    assert is_only_preparatory([]) is False


@pytest.mark.asyncio
async def test_preparatory_round_forces_continue():
    """**回归**：只造了工具就宣称完成 → 必须强制再跑一轮。

    实机症状：用户说「先造一个算 BMI 的工具，然后**用它**算 1.75/70」，
    规划层造完工具就 `goal_done=true`，于是只跑一轮 ——
    用户永远拿不到计算结果。

    模型对"完成"的判断在准备型动作上系统性偏乐观，
    所以不能只靠它的判断，要有确定性兜底。
    """
    executed = []

    async def execute(plan):
        executed.append([t["type"] for t in plan.tool_tasks])
        return {f"t{len(executed)}": FakeResult(data={"ok": 1})}

    async def replan(prior):
        # 第 2 轮才真正"用它算"
        return _plan(done=True, tasks=[{"id": "u1", "type": "bmi_calc", "params": {}}])

    result = await run_goal_loop(
        first_plan=_plan(done=True, tasks=[{"id": "t1", "type": "create_tool", "params": {}}]),
        plan_fn=replan,
        execute_fn=execute,
    )
    assert result.rounds == 2, f"只跑了 {result.rounds} 轮，准备型动作没触发续跑"
    assert executed[0] == ["create_tool"]
    assert executed[1] == ["bmi_calc"]


@pytest.mark.asyncio
async def test_preparatory_guard_respects_max_rounds():
    """兜底不能变成死循环：轮次用尽仍然要停。"""
    async def execute(plan):
        return {f"t{r}": FakeResult(data={"r": r}) for r in [1]}

    async def replan(prior):
        return _plan(done=True, tasks=[{"id": "t1", "type": "create_tool", "params": {}}])

    result = await run_goal_loop(
        first_plan=_plan(done=True, tasks=[{"id": "t1", "type": "create_tool", "params": {}}]),
        plan_fn=replan,
        execute_fn=execute,
        max_rounds=3,
    )
    # 关键：**不能超过上限**（不死循环）。
    # 最后一轮时 `round_no < max_rounds` 不成立，兜底放行让它收尾 ——
    # 所以 stop_reason 可能是 goal_done，这没问题。
    assert result.rounds == 3
    assert result.rounds <= 3


@pytest.mark.asyncio
async def test_preparatory_guard_emits_event():
    """兜底触发时要发事件，让前端/日志看得见"为什么又跑了一轮"。"""
    events = []

    async def execute(plan):
        return {"t": FakeResult(data={"x": 1})}

    async def replan(prior):
        return _plan(done=True, tasks=[{"id": "u", "type": "real_tool", "params": {}}])

    async def emit(name, payload):
        events.append(name)

    await run_goal_loop(
        first_plan=_plan(done=True, tasks=[{"id": "t1", "type": "create_tool", "params": {}}]),
        plan_fn=replan,
        execute_fn=execute,
        emit=emit,
    )
    assert "goal_prep_only" in events
    assert "goal_continue" in events


# ============================================================
# 9. 子智能体内部：dict → ToolTask（实机踩过）
# ============================================================

@pytest.mark.asyncio
async def test_subagent_converts_plan_dicts_to_tooltask(monkeypatch):
    """**回归**：`plan.tool_tasks` 是 dict，而 `execute_dag()` 要 ToolTask 对象。

    实机症状：所有子任务都 failed，error 是
    `'dict' object has no attribute 'id'` —— 而且因为异常被任务状态吞掉，
    表面上只看到"失败"，看不到原因。
    """
    import agents.learning as learning_mod
    import agents.manager as manager_mod

    captured = {}

    async def fake_plan(self, goal, *a, **kw):
        return FakePlan(
            tool_tasks=[{"id": "s1", "type": "web_search", "params": {"query": "x"}}],
            goal_done=True,
        )

    async def fake_execute_dag(self, tasks, *a, **kw):
        captured["tasks"] = tasks
        return {"s1": FakeResult(success=True, data={"text": "结果"})}

    monkeypatch.setattr(manager_mod.ManagerAgent, "plan", fake_plan)
    monkeypatch.setattr(learning_mod.LearningAgent, "execute_dag", fake_execute_dag)

    pool = SubAgentPool(timeout_s=10)
    task = SubAgentTask(goal="查东西", depth=1)
    await pool._execute(task)

    assert task.status == STATUS_DONE, f"应成功,实际 {task.status}: {task.error}"
    # 传给 execute_dag 的必须是对象，且有 id 属性
    sent = captured["tasks"]
    assert sent and hasattr(sent[0], "id"), "还是把 dict 传进去了"
    assert sent[0].id == "s1"
    assert sent[0].type == "web_search"
    assert task.tool_calls == ["web_search"]


@pytest.mark.asyncio
async def test_subagent_blocks_recursive_tools(monkeypatch):
    """子智能体不能再用 run_subagents —— 否则会递归爆炸。"""
    import agents.learning as learning_mod
    import agents.manager as manager_mod

    captured = {}

    async def fake_plan(self, goal, *a, **kw):
        return FakePlan(
            tool_tasks=[
                {"id": "r", "type": "run_subagents", "params": {"tasks": []}},
                {"id": "w", "type": "web_search", "params": {"query": "x"}},
            ],
            goal_done=True,
        )

    async def fake_execute_dag(self, tasks, *a, **kw):
        captured["types"] = [t.type for t in tasks]
        return {"w": FakeResult(success=True, data={"ok": 1})}

    monkeypatch.setattr(manager_mod.ManagerAgent, "plan", fake_plan)
    monkeypatch.setattr(learning_mod.LearningAgent, "execute_dag", fake_execute_dag)

    pool = SubAgentPool(timeout_s=10)
    # depth=1 == max_depth → 递归工具要被摘掉
    task = SubAgentTask(goal="x", depth=1)
    await pool._execute(task)

    assert "run_subagents" not in captured.get("types", []), "递归工具没被拦住"
    assert "web_search" in captured["types"]


# ============================================================
# 10. 子智能体必须共享主对话上下文（实机踩过）
# ============================================================

@pytest.mark.asyncio
async def test_subagent_receives_main_context(monkeypatch):
    """**回归**：子智能体规划时要能看到主对话上下文。

    实机症状：用户说「发」，主智能体知道指的是刚才那个视频任务；
    子智能体只拿到「提交视频生成任务」一句，既不知道是哪个视频，
    也不知道前面建了哪些文件、试过哪些接口 —— 只能从头猜。
    """
    import agents.manager as manager_mod
    import core.subagents as sa

    captured = {}

    async def fake_plan(self, goal, context=None, **kw):
        captured["goal"] = goal
        captured["context"] = context
        return FakePlan(tool_tasks=[], goal_done=True)

    monkeypatch.setattr(manager_mod.ManagerAgent, "plan", fake_plan)

    sentinel = object()          # 冒充主对话 Context
    sa.set_current_context(sentinel)
    try:
        pool = SubAgentPool(timeout_s=10)
        task = SubAgentTask(goal="提交视频生成任务", depth=1)
        await pool._execute(task)
    finally:
        sa.set_current_context(None)

    assert captured.get("context") is sentinel, (
        "子智能体没拿到主对话上下文 —— 它不知道\"发\"指的是什么"
    )


@pytest.mark.asyncio
async def test_subagent_context_defaults_to_none(monkeypatch):
    """没有主上下文时也不能崩（独立调用场景）。"""
    import agents.manager as manager_mod
    import core.subagents as sa

    captured = {}

    async def fake_plan(self, goal, context=None, **kw):
        captured["context"] = context
        return FakePlan(tool_tasks=[], goal_done=True)

    monkeypatch.setattr(manager_mod.ManagerAgent, "plan", fake_plan)
    sa.set_current_context(None)

    pool = SubAgentPool(timeout_s=10)
    await pool._execute(SubAgentTask(goal="x", depth=1))
    assert captured["context"] is None
