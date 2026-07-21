"""M3-03 Result Validator 测试。

覆盖:
- ResultValidator 对空输出/短输出/完整输出的判断
- capability / method / examples 约束检查
- ExecutionCritic 对无工具任务不再无条件得 100%
"""
import pytest
from unittest.mock import patch

from core.result_validator import ResultValidator, ResultValidation
from core.critic import ExecutionCritic, ExecutionContext, TaskExecutionSummary
from skills.models import Skill


@pytest.fixture
def validator():
    return ResultValidator()


@pytest.fixture
def daily_skill():
    return Skill(
        name="DailyReport",
        version="1.0.0",
        capability="按三块生成日报:今日完成、问题、明日计划",
        method="1) 今日完成 2) 问题 3) 明日计划",
        patterns=["日报"],
        tags=["work"],
        steps=[],
        examples=["今日完成:xxx\n问题:无\n明日计划:yyy"],
    )


def test_empty_output_fails(validator):
    result = validator.validate(None, "")
    assert result.passed is False
    assert result.score == 0.0
    assert "输出为空" in result.issues[0]


def test_no_skill_short_output(validator):
    result = validator.validate(None, "ok")
    assert result.passed is False
    assert any("过短" in i for i in result.issues)


def test_no_skill_normal_output(validator):
    result = validator.validate(None, "这是一段足够长的回复内容用于通过最小长度检查")
    assert result.passed is True
    assert result.score > 0.5


def test_full_output_passes(validator, daily_skill):
    output = "今日完成:完成了 M3-03\n问题:暂无\n明日计划:继续 M3-04"
    result = validator.validate(daily_skill, output)
    assert result.passed is True
    assert result.score == 1.0


def test_missing_capability_keyword_fails(validator, daily_skill):
    output = "这是一段回复,但没有包含任何日报栏目关键词"
    result = validator.validate(daily_skill, output)
    assert result.passed is False
    assert any("capability" in i or "能力" in i for i in result.issues)


def test_method_steps_low_coverage_fails(validator, daily_skill):
    # 只覆盖 1/3 步骤
    output = "今日完成:做了点事\n其他内容完全无关"
    result = validator.validate(daily_skill, output)
    assert result.passed is False
    assert any("method" in i or "步骤" in i for i in result.issues)


def test_examples_style_mismatch_fails(validator, daily_skill):
    # 输出与 example 无关键词重叠
    output = "zzzzzzzz qqqqqqqq wwwwwwww"
    result = validator.validate(daily_skill, output)
    assert result.passed is False


# ===== ExecutionCritic 集成 =====


@pytest.mark.asyncio
async def test_critic_no_tool_task_not_full_score(tmp_path):
    """无工具任务不再无条件得 100%。"""
    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        with patch("core.memory.get_self_evolution_enabled", return_value=True):
            from core.memory import MemoryStore
            critic = ExecutionCritic(memory_store=MemoryStore(base_path=tmp_path))
            skill = Skill(
                name="DailyReport",
                version="1.0.0",
                capability="按三块生成日报:今日完成、问题、明日计划",
                method="1) 今日完成 2) 问题 3) 明日计划",
                patterns=["日报"],
                tags=["work"],
                steps=[],
                examples=["今日完成:xxx"],
            )
            context = ExecutionContext(
                trace_id="m3_03_001",
                scenario="daily",
                intent="帮我写日报",
                selected_skill="DailyReport",
                tasks=[],  # 无工具任务
                latency_ms=100.0,
                final_output="随便写点完全无关的内容",
                selected_skill_obj=skill,
            )
            result = await critic.evaluate(context)
            assert result.success_rate < 1.0
            assert "failure_record" in result.records_generated


@pytest.mark.asyncio
async def test_critic_no_tool_task_passes_with_good_output(tmp_path):
    """无工具任务输出符合约束时仍可得高分。"""
    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        with patch("core.memory.get_self_evolution_enabled", return_value=True):
            from core.memory import MemoryStore
            critic = ExecutionCritic(memory_store=MemoryStore(base_path=tmp_path))
            skill = Skill(
                name="DailyReport",
                version="1.0.0",
                capability="按三块生成日报:今日完成、问题、明日计划",
                method="1) 今日完成 2) 问题 3) 明日计划",
                patterns=["日报"],
                tags=["work"],
                steps=[],
                examples=["今日完成:xxx"],
            )
            context = ExecutionContext(
                trace_id="m3_03_002",
                scenario="daily",
                intent="帮我写日报",
                selected_skill="DailyReport",
                tasks=[],
                latency_ms=100.0,
                final_output="今日完成:完成测试\n问题:无\n明日计划:继续开发",
                selected_skill_obj=skill,
            )
            result = await critic.evaluate(context)
            assert result.success_rate == 1.0
            assert "success_record" in result.records_generated
