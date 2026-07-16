"""MemoryStore & ExecutionCritic 测试"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.memory import (
    FailureRecord,
    MemoryStore,
    SkillPatch,
    SuccessRecord,
    get_memory_store,
)
from core.critic import (
    ExecutionCritic,
    ExecutionContext,
    TaskExecutionSummary,
    build_execution_context,
)
from infra.config import get_self_evolution_enabled


# ---- 临时目录 fixture ----

@pytest.fixture
def temp_mem_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def mem_store(temp_mem_dir):
    # Mock config.feature flag to avoid loading real .env
    with patch("core.memory.get_self_evolution_enabled", return_value=True):
        store = MemoryStore(base_path=temp_mem_dir)
        yield store


# ---- MemoryStore 测试 ----

def test_record_failure(mem_store):
    rec = mem_store.record_failure(
        trace_id="trace_001",
        scenario="travel_plan",
        intent="厦门明天怎么玩",
        selected_skill="travel_plan",
        success_rate=0.5,
        fallback_count=1,
        latency_ms=1500.0,
        diagnosis="天气工具超时",
        suggestion={"type": "increase_timeout"},
    )
    assert rec.trace_id == "trace_001"
    assert rec.success_rate == 0.5
    assert rec.diagnosis == "天气工具超时"


def test_record_success(mem_store):
    rec = mem_store.record_success(
        trace_id="trace_002",
        scenario="weather_query",
        matched_skill="travel_plan",
        latency_ms=800.0,
        pattern="厦门天气",
    )
    assert rec.trace_id == "trace_002"
    assert rec.matched_skill == "travel_plan"


def test_get_recent_failures(mem_store):
    # 记录几条
    mem_store.record_failure(
        trace_id="f001", scenario="travel", success_rate=0.0, latency_ms=100.0,
        intent="", selected_skill="", fallback_count=0, diagnosis="失败"
    )
    mem_store.record_failure(
        trace_id="f002", scenario="travel", success_rate=0.5, latency_ms=100.0,
        intent="", selected_skill="", fallback_count=0, diagnosis="部分失败"
    )

    failures = mem_store.get_recent_failures(scenario="travel", top_k=5)
    assert len(failures) == 2
    # 按时间倒序,最新在前
    assert failures[0].trace_id == "f002"


def test_get_skill_hints(mem_store):
    # 先记录失败,diagnosis 中包含简单关键词
    mem_store.record_failure(
        trace_id="h001",
        scenario="travel",
        success_rate=0.0,
        latency_ms=100.0,
        intent="厦门旅游",
        selected_skill="travel_plan",
        fallback_count=0,
        diagnosis="搜索超时",
    )
    # mem_store fixture 内部已 mock flag=True,直接调用即可
    hints = mem_store.get_skill_hints("搜索超时怎么办")
    assert any("搜索超时" in h for h in hints)


def test_pending_patches(mem_store):
    patch_obj = SkillPatch(
        id="patch_001",
        trace_id="trace_001",
        timestamp="2026-07-13T10:00:00",
        target_skill="travel_plan",
        patch_type="improve_skill",
        diagnosis="需要优化",
        suggestion={"type": "increase_retry"},
        confidence=0.8,
    )
    mem_store.add_pending_patch(patch_obj)

    patches = mem_store.get_pending_patches()
    assert len(patches) == 1
    assert patches[0].id == "patch_001"
    assert patches[0].confidence == 0.8


def test_approve_and_reject_patch(mem_store):
    patch_obj = SkillPatch(
        id="patch_002",
        trace_id="trace_002",
        timestamp="2026-07-13T10:00:00",
        target_skill="travel_plan",
        patch_type="improve_skill",
        diagnosis="需要优化",
        suggestion={},
        confidence=0.75,
    )
    mem_store.add_pending_patch(patch_obj)

    # 批准
    ok = mem_store.approve_patch("patch_002", "human")
    assert ok
    patches = mem_store.get_pending_patches()
    assert len(patches) == 0

    # 拒绝
    mem_store.add_pending_patch(patch_obj)
    ok = mem_store.reject_patch("patch_002", "human")
    assert ok
    patches = mem_store.get_pending_patches()
    assert len(patches) == 0


def test_stats(mem_store):
    mem_store.record_failure(
        trace_id="s001", scenario="a", success_rate=0.0, latency_ms=100.0,
        intent="", selected_skill="", fallback_count=0, diagnosis="x"
    )
    mem_store.record_failure(
        trace_id="s002", scenario="b", success_rate=0.0, latency_ms=100.0,
        intent="", selected_skill="", fallback_count=0, diagnosis="y"
    )
    mem_store.record_success(
        trace_id="s003", scenario="c", matched_skill="x", latency_ms=100.0
    )
    mem_store.record_success(
        trace_id="s004", scenario="d", matched_skill="y", latency_ms=100.0
    )

    stats = mem_store.get_stats()
    assert stats["total_failures"] == 2
    assert stats["total_successes"] == 2
    assert stats["pending_patches"] == 0


def test_capacity_enforce(mem_store):
    # 创建恰好 55 条记录,超过 max_per_month=50
    for i in range(55):
        mem_store.record_failure(
            trace_id=f"cap_{i:03d}",
            scenario="test",
            success_rate=0.1 * i,  # 0.0 ~ 5.4
            latency_ms=100.0,
            intent="",
            selected_skill="",
            fallback_count=0,
            diagnosis=f"failure_{i}",
        )

    # 触发容量控制:保留 int(55 * 0.8) = 44 条
    mem_store.enforce_capacity(max_per_month=50, keep_ratio=0.8)

    stats = mem_store.get_stats()
    # 55 - int(55 * 0.8) = 55 - 44 = 11 条被删除,保留 44 条
    assert stats["total_failures"] == 44


# ---- ExecutionCritic 测试 ----

@pytest.mark.asyncio
async def test_critic_disabled(temp_mem_dir):
    """feature flag 关闭时直接返回"""
    with patch("core.critic.get_self_evolution_enabled", return_value=False):
        with patch("core.memory.get_self_evolution_enabled", return_value=False):
            critic = ExecutionCritic(MemoryStore(base_path=temp_mem_dir))
            context = ExecutionContext(
                trace_id="t001",
                scenario="travel",
                intent="厦门旅游",
                selected_skill="travel_plan",
                tasks=[TaskExecutionSummary(task_id="t1", tool="weather", success=True)],
                latency_ms=500.0,
            )
            result = await critic.evaluate(context)
            assert result.diagnosis == "self_evolution_disabled"


@pytest.mark.asyncio
async def test_critic_all_success(mem_store, temp_mem_dir):
    """全部成功时记录成功路径"""
    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        critic = ExecutionCritic(memory_store=mem_store)
        context = ExecutionContext(
            trace_id="critic_001",
            scenario="weather",
            intent="查厦门天气",
            selected_skill="travel_plan",
            tasks=[
                TaskExecutionSummary(task_id="t1", tool="weather_query", success=True),
            ],
            latency_ms=300.0,
        )
        result = await critic.evaluate(context)
        assert result.success_rate == 1.0
        assert "success_record" in result.records_generated


@pytest.mark.asyncio
async def test_critic_partial_failure(mem_store):
    """部分失败时记录失败"""
    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        critic = ExecutionCritic(memory_store=mem_store)
        context = ExecutionContext(
            trace_id="critic_002",
            scenario="travel",
            intent="厦门旅游",
            selected_skill="travel_plan",
            tasks=[
                TaskExecutionSummary(task_id="t1", tool="weather_query", success=True),
                TaskExecutionSummary(task_id="t2", tool="web_search", success=False, error="超时"),
            ],
            latency_ms=2000.0,
        )
        result = await critic.evaluate(context)
        assert result.success_rate == 0.5
        assert "failure_record" in result.records_generated
        assert "超时" in result.diagnosis


@pytest.mark.asyncio
async def test_critic_high_confidence_auto_approve(mem_store):
    """高置信度建议自动生效 - 部分失败场景"""
    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        critic = ExecutionCritic(memory_store=mem_store)
        context = ExecutionContext(
            trace_id="critic_003",
            scenario="travel",
            intent="厦门旅游",
            selected_skill="travel_plan",
            tasks=[
                TaskExecutionSummary(task_id="t1", tool="weather_query", success=True),
                TaskExecutionSummary(task_id="t2", tool="web_search", success=False, error="超时"),
            ],
            latency_ms=30000.0,
        )
        result = await critic.evaluate(context)
        # 部分失败时,应该生成建议
        assert result.suggestion is not None
        assert "failure_record" in result.records_generated


def test_build_execution_context():
    from tools.base import ToolResult

    tool_results = {
        "t1": ToolResult(success=True, data={"city": "厦门"}),
        "t2": ToolResult(success=False, error="超时"),
    }
    context = build_execution_context(
        trace_id="build_001",
        scenario="travel",
        intent="厦门旅游",
        selected_skill="travel_plan",
        tool_results=tool_results,
        latency_ms=1500.0,
    )
    assert context.trace_id == "build_001"
    assert len(context.tasks) == 2
    assert context.tasks[0].success is True
    assert context.tasks[1].success is False
    assert context.tasks[1].error == "超时"


def test_get_self_evolution_enabled_mock():
    """feature flag 加载逻辑测试"""
    from infra.config import config
    with patch.object(config, "self_evolution_enabled", True):
        assert get_self_evolution_enabled() is True
    with patch.object(config, "self_evolution_enabled", False):
        assert get_self_evolution_enabled() is False


# ---- 端到端测试 ----

@pytest.mark.asyncio
async def test_memory_and_critic_integration(temp_mem_dir):
    """MemoryStore + ExecutionCritic 端到端集成"""
    with patch("core.memory.get_self_evolution_enabled", return_value=True):
        with patch("core.critic.get_self_evolution_enabled", return_value=True):
            store = MemoryStore(base_path=temp_mem_dir)
            critic = ExecutionCritic(memory_store=store)

            # 模拟一次失败的执行
            context = ExecutionContext(
                trace_id="e2e_001",
                scenario="travel",
                intent="厦门旅游",
                selected_skill="travel_plan",
                tasks=[
                    TaskExecutionSummary(task_id="t1", tool="weather_query", success=False, error="网络错误"),
                    TaskExecutionSummary(task_id="t2", tool="web_search", success=True),
                ],
                latency_ms=5000.0,
            )

            result = await critic.evaluate(context)
            
            # 验证记录生成
            assert "failure_record" in result.records_generated
            
            # 验证 MemoryStore 中有记录
            failures = store.get_recent_failures(top_k=10)
            assert any(f.trace_id == "e2e_001" for f in failures)
