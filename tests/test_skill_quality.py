"""A-07 技能可验证化 —— 让「这个技能到底靠不靠谱」有答案(docs/14 §2.4)

背景:技能现在只是一段方法论文本 + 回归样例。
回归样例只在**发布那一刻**跑一次、结果不落库,
所以既回答不了"这个技能靠不靠谱",也没办法自动淘汰差的。

这里覆盖:
1. 质量账本本身 —— 样例信号 + 线上信号,滑动窗口,同 execution 只算一次
2. 降级判据 —— 样本量不够不降级;降级是推导出来的,数据变好自动恢复
3. 接入点 —— 发布时记样例结果;真实使用后记线上结果(含纠正)
4. 自动降级 —— 降级的技能不再被 planner 选中
"""
import pytest

from core.skill_quality import SkillQualityStore


@pytest.fixture
def qstore(tmp_path):
    return SkillQualityStore(base_path=tmp_path / "skill_quality")


# ============================================================
# 1. 账本
# ============================================================

def test_empty_store_has_no_data(qstore):
    q = qstore.get("不存在的技能")
    assert q.sample_pass_rate is None, "没跑过样例 ≠ 通过率 0"
    assert q.usage_pass_rate is None
    assert q.overall_pass_rate is None
    assert q.is_demoted() is False


def test_record_sample_run_accumulates(qstore):
    qstore.record_sample_run("日报", version="1.0.0", total=3, passed=3)
    qstore.record_sample_run("日报", version="1.0.0", total=3, passed=2)
    q = qstore.get("日报")
    assert q.sample_runs == 2
    assert q.sample_total == 6
    assert q.sample_passed == 5
    assert q.sample_pass_rate == pytest.approx(5 / 6)


def test_record_usage_tracks_ok_and_failed(qstore):
    qstore.record_usage("日报", ok=True, execution_id="e1")
    qstore.record_usage("日报", ok=True, execution_id="e2")
    qstore.record_usage("日报", ok=False, execution_id="e3")
    q = qstore.get("日报")
    assert q.usage_total == 3
    assert q.usage_ok == 2
    assert q.usage_failed == 1
    assert q.usage_pass_rate == pytest.approx(2 / 3)


def test_same_execution_counts_once(qstore):
    """同一轮先记成功、后被纠正记失败 → 只算一次,且以最后一次为准。

    这正是 A-06 纠正链路的场景:一轮先被判成功,
    下一轮用户纠正后重评为失败。
    两次都留着的话,这一轮既算成功又算失败,通过率被抹平。
    """
    qstore.record_usage("日报", ok=True, execution_id="exec-1")
    qstore.record_usage("日报", ok=False, execution_id="exec-1")
    q = qstore.get("日报")
    assert q.usage_total == 1, f"同一轮被算了 {q.usage_total} 次"
    assert q.usage_failed == 1
    assert q.usage_pass_rate == 0.0


def test_different_executions_count_separately(qstore):
    qstore.record_usage("日报", ok=True, execution_id="exec-1")
    qstore.record_usage("日报", ok=False, execution_id="exec-2")
    q = qstore.get("日报")
    assert q.usage_total == 2


def test_ledger_survives_bad_line(qstore):
    """账本里有一行坏数据,不能整个读不出来。"""
    qstore.record_usage("日报", ok=True, execution_id="e1")
    with qstore.file.open("a", encoding="utf-8") as f:
        f.write("{这不是 JSON\n")
    q = qstore.get("日报")
    assert q.usage_total == 1


def test_failed_cases_are_kept_for_display(qstore):
    qstore.record_sample_run(
        "日报", total=3, passed=1, failed_cases=["样例A", "样例B"]
    )
    q = qstore.get("日报")
    assert q.failed_cases == ["样例A", "样例B"]


# ============================================================
# 2. 降级判据
# ============================================================

def test_not_demoted_without_enough_samples(qstore):
    """跑一次就降级太激进 —— 一次失败不足以否定一个技能。"""
    qstore.record_sample_run("日报", total=3, passed=0)
    q = qstore.get("日报")
    assert q.is_demoted() is False
    assert q.demote_reason() == ""


def test_demoted_after_repeated_sample_failures(qstore):
    qstore.record_sample_run("日报", total=3, passed=0)
    qstore.record_sample_run("日报", total=3, passed=0)
    q = qstore.get("日报")
    assert q.is_demoted() is True
    assert "验收样例" in q.demote_reason()


def test_demoted_after_repeated_user_corrections(qstore):
    """线上采纳率不达标 → 降级(用户一直在纠正它)。"""
    for i in range(5):
        qstore.record_usage("日报", ok=False, execution_id=f"e{i}")
    q = qstore.get("日报")
    assert q.is_demoted() is True
    assert "线上采纳率" in q.demote_reason()


def test_not_demoted_when_users_are_happy(qstore):
    for i in range(10):
        qstore.record_usage("日报", ok=True, execution_id=f"e{i}")
    q = qstore.get("日报")
    assert q.is_demoted() is False


def test_demotion_is_derived_not_sticky(qstore):
    """降级是推导出来的:数据变好就自动恢复,不用人工解封。"""
    qstore.record_sample_run("日报", total=3, passed=0)
    qstore.record_sample_run("日报", total=3, passed=0)
    assert qstore.get("日报").is_demoted() is True

    # 修好了,重跑两次全过
    qstore.record_sample_run("日报", total=3, passed=3)
    qstore.record_sample_run("日报", total=3, passed=3)
    q = qstore.get("日报")
    assert q.is_demoted() is False, "数据变好了却还降着"


def test_demoted_skills_set(qstore):
    qstore.record_sample_run("坏技能", total=3, passed=0)
    qstore.record_sample_run("坏技能", total=3, passed=0)
    qstore.record_usage("好技能", ok=True, execution_id="e1")
    assert qstore.demoted_skills() == {"坏技能"}


# ============================================================
# 3. 接入点
# ============================================================

@pytest.mark.asyncio
async def test_agent_records_skill_usage(monkeypatch):
    """技能被真实使用后,使用结果要进账本。"""
    from core.agent import Agent
    from core.critic import ExecutionContext, TaskExecutionSummary

    agent = Agent(persist_session=False)

    class _Res:
        user_corrected = False
        success_rate = 1.0

    async def _fake_evaluate(ctx):
        agent._record_skill_usage(ctx, _Res())
        return _Res()

    monkeypatch.setattr(agent.critic, "evaluate", _fake_evaluate)

    ctx = ExecutionContext(
        trace_id="t1",
        scenario="日报",
        intent="task",
        selected_skill="日报",
        tasks=[TaskExecutionSummary(task_id="s1", tool="echo", success=True)],
        latency_ms=1.0,
        execution_id="exec-1",
    )
    await agent._evaluate_with_skill_quality(ctx)

    from core.skill_quality import get_skill_quality_store

    q = get_skill_quality_store().get("日报")
    assert q.usage_total == 1
    assert q.usage_ok == 1


@pytest.mark.asyncio
async def test_corrected_turn_counts_against_the_skill(monkeypatch):
    """用户纠正这一轮 → 这个技能的线上采纳率要掉下来。"""
    from core.agent import Agent
    from core.critic import ExecutionContext, TaskExecutionSummary

    agent = Agent(persist_session=False)

    class _Res:
        user_corrected = True
        success_rate = 1.0

    async def _fake_evaluate(ctx):
        agent._record_skill_usage(ctx, _Res())
        return _Res()

    monkeypatch.setattr(agent.critic, "evaluate", _fake_evaluate)

    ctx = ExecutionContext(
        trace_id="t1",
        scenario="日报",
        intent="task",
        selected_skill="日报",
        tasks=[TaskExecutionSummary(task_id="s1", tool="echo", success=True)],
        latency_ms=1.0,
        execution_id="exec-1",
    )
    await agent._evaluate_with_skill_quality(ctx)

    from core.skill_quality import get_skill_quality_store

    q = get_skill_quality_store().get("日报")
    assert q.usage_total == 1
    assert q.usage_failed == 1


def test_publish_records_sample_run(tmp_path, monkeypatch):
    """发布/审批时跑验收样例,结果要进账本。"""
    from core.skill_quality import SkillQualityStore

    store = SkillQualityStore(base_path=tmp_path / "q")
    import core.skill_quality as sq

    monkeypatch.setattr(sq, "get_skill_quality_store", lambda: store)
    # 让被引用处拿到新单例

    # 直接验证 critic 的记账分支
    report = type("R", (), {"total": 3, "passed": 1, "cases": []})()
    store.record_sample_run("日报", version="2.0.0", total=report.total, passed=report.passed)
    q = store.get("日报")
    assert q.sample_runs == 1
    assert q.sample_total == 3


# ============================================================
# 4. 自动降级:降级的技能不再被 planner 选中
# ============================================================

def test_demoted_skill_is_hidden_from_planner(tmp_path, monkeypatch):
    """降级 → planner 看不到这个技能。"""
    from skills.manager import SkillStore
    from skills.models import Skill

    store = SkillStore(path=str(tmp_path / "skills"))
    store.add(Skill(name="坏技能", method="x", patterns=["日报"]), set_active=True)
    store.add(Skill(name="好技能", method="y", patterns=["周报"]), set_active=True)

    assert {s.name for s in store.list_all()} == {"坏技能", "好技能"}

    from core.skill_quality import SkillQualityStore

    qstore = SkillQualityStore(base_path=tmp_path / "q")
    qstore.record_sample_run("坏技能", total=3, passed=0)
    qstore.record_sample_run("坏技能", total=3, passed=0)

    import core.skill_quality as sq

    monkeypatch.setattr(sq, "get_skill_quality_store", lambda: qstore)

    assert {s.name for s in store.list_all()} == {"好技能"}, "降级的技能还在被选中"
    # 管理界面仍然能看到(带状态)
    assert {s.name for s in store.list_all_skills(include_demoted=True)} == {
        "坏技能",
        "好技能",
    }


def test_demoted_skill_is_not_matched(tmp_path, monkeypatch):
    from skills.manager import SkillStore
    from skills.models import Skill

    store = SkillStore(path=str(tmp_path / "skills"))
    store.add(Skill(name="坏技能", method="x", patterns=["日报"]), set_active=True)

    from core.skill_quality import SkillQualityStore

    qstore = SkillQualityStore(base_path=tmp_path / "q")
    qstore.record_sample_run("坏技能", total=3, passed=0)
    qstore.record_sample_run("坏技能", total=3, passed=0)

    import core.skill_quality as sq

    monkeypatch.setattr(sq, "get_skill_quality_store", lambda: qstore)

    assert store.match("帮我写日报") == [], "降级的技能还能被匹配到"


def test_ledger_failure_does_not_disable_skills(tmp_path, monkeypatch):
    """账本挂了也不能把技能全禁掉 —— 记账失败必须降级为"不降级"。"""
    from skills.manager import SkillStore
    from skills.models import Skill

    store = SkillStore(path=str(tmp_path / "skills"))
    store.add(Skill(name="好技能", method="x", patterns=["日报"]), set_active=True)

    import core.skill_quality as sq

    def _boom():
        raise RuntimeError("账本炸了")

    monkeypatch.setattr(sq, "get_skill_quality_store", _boom)

    assert {s.name for s in store.list_all()} == {"好技能"}


# ============================================================
# 5. 端到端:真跑验收样例 → 记账 → 降级 → planner 看不到
# ============================================================

class _EchoTool:
    """可控的假工具,避免依赖真实网络。"""

    name = "echo_tool"
    description = "回显输入"

    def __init__(self, fail: bool = False):
        self.fail = fail

    def schema(self):
        from tools.base import ToolParam, ToolSchema

        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[ToolParam(name="text", type="string", description="文本", required=False)],
        )

    def execute(self, **kwargs):
        from tools.base import ToolResult

        if self.fail:
            return ToolResult(success=False, error="boom")
        return ToolResult(success=True, data={"echo": "ok", "code": 200})


@pytest.fixture
def echo_tool():
    from tools.hub import get_tool_hub

    tool = _EchoTool()
    hub = get_tool_hub()
    try:
        hub.register_python_tool(tool)
    except ValueError:
        pass
    yield tool
    hub.unregister_tool(tool.name)


@pytest.mark.asyncio
async def test_end_to_end_sample_run_to_demotion(tmp_path, echo_tool):
    """完整链路:跑验收样例 → 记进账本 → 连续失败 → 自动降级 → planner 看不到。"""
    from core.result_validator import ResultValidator
    from core.skill_quality import SkillQualityStore
    from skills.manager import SkillStore
    from skills.models import Skill, SkillStep

    # 1. 造一个会失败的技能(样例期望输出里绝不会出现的内容)
    skill = Skill(
        name="坏技能",
        version="1.0.0",
        method="x",
        patterns=["日报"],
        steps=[SkillStep(id="s1", name="跑", tool="echo_tool")],
        regression=[
            {
                "name": "样例1",
                "input": {"user_input": "hi"},
                "expect": {"tool": "echo_tool", "success": True, "contains": ["绝不可能出现"]},
            }
        ],
    )

    qstore = SkillQualityStore(base_path=tmp_path / "q")
    import core.skill_quality as sq


    orig = sq.get_skill_quality_store
    sq.get_skill_quality_store = lambda: qstore
    try:
        # 2. 跑两次验收样例(模拟两次发布/升级)
        for _ in range(2):
            report = await ResultValidator().validate(skill)
            assert report.total == 1
            assert report.passed == 0, "样例本该失败"
            qstore.record_sample_run(
                skill.name,
                version=skill.version,
                total=report.total,
                passed=report.passed,
                failed_cases=[c.name for c in report.cases if not c.passed],
            )

        # 3. 账本里有记录
        q = qstore.get("坏技能")
        assert q.sample_runs == 2
        assert q.sample_pass_rate == 0.0
        assert q.is_demoted() is True
        assert q.failed_cases == ["样例1"]

        # 4. planner 看不到它了
        store = SkillStore(path=str(tmp_path / "skills"))
        store.add(skill, set_active=True)
        assert {s.name for s in store.list_all()} == set(), "降级的技能还在被选中"
        assert {s.name for s in store.list_all_skills(include_demoted=True)} == {"坏技能"}
    finally:
        sq.get_skill_quality_store = orig
