"""自演化链路的**故障隔离**:记录失败不能影响用户拿答案。

## 为什么要有这个测试

实机踩过:跑测试时 `Remove-Item memory -Recurse -Force` 把 `memory/`
整个删了,而**后端还在运行**。它的 `MemoryStore` 单例早就建好,
手里攥着 `memory/failures` 这个路径 —— 目录一被删,之后每次写都抛

    [WinError 3] \u7cfb\u7edf\u627e\u4e0d\u5230\u6307\u5b9a\u7684\u8def\u5f84\u3002: '...\\memory\\failures'

\u7528\u6237\u5728\u754c\u9762\u4e0a\u770b\u5230\u4e24\u6b21"\u9519\u8bef:",\u800c\u4e0d\u662f\u4ed6\u7684\u56de\u7b54\u3002

**\u81ea\u6f14\u5316\u662f"\u9526\u4e0a\u6dfb\u82b1",\u5b83\u6302\u4e86\u4e0d\u80fd\u5f71\u54cd\u7528\u6237\u62ff\u7b54\u6848\u3002**
"""
import pytest

from core.critic import ExecutionContext, TaskExecutionSummary


def _ctx(**kw):
    base = dict(
        trace_id="t1",
        scenario="x",
        intent="task",
        selected_skill="s",
        tasks=[],
        latency_ms=10.0,
    )
    base.update(kw)
    return ExecutionContext(**base)


@pytest.fixture
def critic(tmp_path):
    from core.critic import ExecutionCritic
    from core.memory import MemoryStore

    store = MemoryStore(base_path=tmp_path / "memory")
    return ExecutionCritic(store)


def _break_store(critic):
    """让 store 的所有写操作都炸 —— 模拟磁盘满/权限问题。"""
    def _boom(*args, **kwargs):
        raise OSError("磁盘满了")

    critic.memory.record_failure = _boom
    critic.memory.record_success = _boom
    critic.memory.add_pending_patch = _boom
    return _boom


# ============================================================
# 1. 存储层自愈
# ============================================================

def test_store_heals_after_dir_deleted(tmp_path):
    """库被删 → 存储层自己建回来，且能继续写。"""
    import shutil

    from core.memory import MemoryStore

    store = MemoryStore(base_path=tmp_path / "memory")
    shutil.rmtree(store.base, ignore_errors=True)

    store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
    )
    assert store.db_path.exists()
    assert len(store.get_recent_failures(top_k=5)) == 1


# ============================================================
# 2. critic 隔离 —— 核心
# ============================================================

@pytest.mark.asyncio
async def test_evaluate_survives_broken_store(critic):
    """存储全炸时,evaluate() **不能抛** —— 用户照样拿答案。"""
    _break_store(critic)

    ctx = _ctx(
        tasks=[TaskExecutionSummary(task_id="s1", tool="t", success=False)],
    )
    result = await critic.evaluate(ctx)

    assert result is not None
    assert result.success_rate < 1.0
    # 记录没成功,但评估结果照常产出
    assert "failure_record" not in result.records_generated


@pytest.mark.asyncio
async def test_evaluate_survives_broken_store_on_success(critic):
    """成功路径同理。"""
    _break_store(critic)

    ctx = _ctx(
        tasks=[TaskExecutionSummary(task_id="s1", tool="t", success=True)],
    )
    result = await critic.evaluate(ctx)
    assert result is not None
    assert result.success_rate == 1.0


@pytest.mark.asyncio
async def test_evaluate_survives_user_corrected(critic):
    """被用户纠正的轮次:记失败 + 生成纠正补丁,两处写都炸也要扛住。"""
    _break_store(critic)

    ctx = _ctx(
        user_feedback="不对,应该是 task",
        tasks=[TaskExecutionSummary(task_id="s1", tool="t", success=True)],
    )
    result = await critic.evaluate(ctx)
    assert result is not None
    assert result.user_corrected is True


@pytest.mark.asyncio
async def test_evaluate_still_records_when_store_works(critic):
    """回归:存储正常时,记录照常产生(别把功能改没了)。"""
    ctx = _ctx(
        tasks=[TaskExecutionSummary(task_id="s1", tool="t", success=False)],
    )
    result = await critic.evaluate(ctx)
    assert "failure_record" in result.records_generated
