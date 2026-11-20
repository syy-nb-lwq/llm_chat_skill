"""MemoryStore 在**目录被删掉**之后还能不能正常工作。

## 为什么要有这个测试

实机踩过:跑测试时执行了 `Remove-Item memory -Recurse -Force`,
而**后端还在运行** —— 它的 `MemoryStore` 单例早就建好了,
手里攥着 `memory/failures` 这个路径。目录一被删,之后每次写/读都抛

    [WinError 3] 系统找不到指定的路径。: '...\\memory\\failures'

用户在界面上看到两次"错误:",自演化链路当场断掉。

**目录没了应该被当成"没有记录",而不是"崩溃"。**
日志清理、部署脚本、磁盘清理都可能删掉它 —— 不该要求运维知道这个内部结构。
"""

import pytest

from core.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(base_path=tmp_path / "memory")


def _wipe(store):
    """把 store 底下的目录全删了(模拟外部清理)。"""
    import shutil

    shutil.rmtree(store.base, ignore_errors=True)


# ============================================================
# 1. 写路径
# ============================================================

def test_record_failure_after_dir_wiped(store):
    """目录被删后记失败 —— 必须成功,目录自建。"""
    _wipe(store)
    rec = store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=10.0,
        diagnosis="boom",
    )
    assert rec is not None
    assert (store.failures_dir).exists()


def test_record_success_after_dir_wiped(store):
    """目录被删后记成功 —— 必须成功,目录自建。"""
    _wipe(store)
    store.record_success(trace_id="t1", scenario="x", matched_skill="s", latency_ms=10)
    assert store.successes_dir.exists()
    assert (store.successes_dir / "success_index.jsonl").exists()


def test_add_pending_patch_after_dir_wiped(store):
    from core.memory import SkillPatch

    _wipe(store)
    patch = SkillPatch(
        id="p1", trace_id="t1", timestamp="2026-01-01T00:00:00",
        target_skill="s", patch_type="method",
        diagnosis="d", suggestion="", confidence=0.5,
    )
    store.add_pending_patch(patch)
    assert store.patches_dir.exists()


def test_write_audit_after_dir_wiped(store):
    _wipe(store)
    path = store.write_audit("auto_publish", {"x": 1})
    assert path, "写审计应该返回路径"
    assert store.audit_dir.exists()


def test_record_failure_to_existing_dir_still_works(store):
    """正常情况不受影响(回归保护)。"""
    store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
    )
    months = list(store.failures_dir.iterdir())
    assert len(months) == 1
    assert len(list(months[0].glob("*.json"))) == 1


# ============================================================
# 2. 读路径
# ============================================================

def test_get_recent_failures_after_dir_wiped(store):
    """读路径:目录没了要返回空,不能抛。"""
    _wipe(store)
    assert store.get_recent_failures(top_k=10) == []


def test_get_pending_patches_after_dir_wiped(store):
    _wipe(store)
    assert store.get_pending_patches() == []


def test_get_stats_after_dir_wiped(store):
    """统计接口不能因为目录没了就 500。"""
    _wipe(store)
    stats = store.get_stats()
    assert stats["total_failures"] == 0
    assert stats["total_successes"] == 0
    assert stats["adoption_rate"] == 0.0


def test_get_adoption_rate_after_dir_wiped(store):
    _wipe(store)
    rate = store.get_adoption_rate()
    assert rate["adoption_rate"] == 0.0
    assert rate["evaluated"] == 0


def test_enforce_capacity_after_dir_wiped(store):
    _wipe(store)
    # 清理不该抛
    store.enforce_capacity()


def test_get_success_hints_after_dir_wiped(store):
    _wipe(store)
    assert store._get_success_hints("随便什么") == []


def test_remove_success_after_dir_wiped(store):
    """撤销成功记录:目录没了要返回 False,不能抛。"""
    _wipe(store)
    assert store.remove_success("no-such-trace") is False


# ============================================================
# 3. 端到端:删目录 → 继续用 → 数据还在
# ============================================================

def test_store_recovers_after_dir_wiped(store):
    """完整闭环:删目录 → 记一条 → 再删 → 还能记 → 读得到。"""
    store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
    )
    _wipe(store)
    store.record_failure(
        trace_id="t2", scenario="y", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
    )
    recent = store.get_recent_failures(top_k=10)
    assert len(recent) == 1
    scenario = recent[0].get("scenario") if isinstance(recent[0], dict) else recent[0].scenario
    assert scenario == "y"


def test_base_dir_itself_wiped(store):
    """连 memory/ 根目录都没了也一样。"""
    import shutil

    shutil.rmtree(store.base, ignore_errors=True)
    store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
    )
    assert store.get_recent_failures(top_k=5) is not None
