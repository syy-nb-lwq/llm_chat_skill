"""自诊断 + 自修复 —— 让助手自己发现"我的工具不好用了"并尝试修。

## 这条回路补的是什么

`create_tool` 只解决了**造**。但工具跑坏了没人记账：
实机踩过 —— agent 想要拼视频，既没 ffmpeg、`subprocess` 又被禁，
于是它**把一段视频复制成"成片"交差**（5.6s vs 应有的 16.9s），
框架对此毫无反应，最后是用户自己发现的。

这不该由人来做。所以：

1. **记账**：每次工具调用落流水（`core/tool_health.py`）
2. **判定**：滑动窗口成功率 / 连续失败 → 不健康（**算出来的，会自恢复**）
3. **提示**：把坏工具注入规划提示词，让 agent 别往坏工具上撞
4. **申报**：`report_capability_gap` 让 agent 主动说"我缺什么"
5. **修复**：agent 自己造的工具反复失败 → 自动出 v2（内置源码绝不碰）
"""
import json

import pytest

from core import tool_health as th


@pytest.fixture
def store(tmp_path):
    return th.ToolHealthStore(base_path=tmp_path / "tools")


# ============================================================
# 1. 记账与指标
# ============================================================

def test_empty_store_is_clean(store):
    assert store.snapshot() == []
    assert store.unhealthy() == []
    assert store.health_block() == ""


def test_records_success_and_failure(store):
    for _ in range(3):
        store.record("t", True, duration_ms=10)
    store.record("t", False, error="boom")
    h = store.health("t")
    assert h.total == 4
    assert h.ok == 3 and h.failed == 1
    assert h.fail_rate == pytest.approx(0.25)


def test_consecutive_failures_counted_from_tail(store):
    store.record("t", True)
    store.record("t", False, error="e1")
    store.record("t", False, error="e2")
    h = store.health("t")
    assert h.consecutive_failures == 2


def test_recent_errors_deduped(store):
    for _ in range(4):
        store.record("t", False, error="same error")
    h = store.health("t")
    assert h.recent_errors == ["same error"]


# ============================================================
# 2. 判定（不能因为两次失败就下结论）
# ============================================================

def test_sample_threshold_protects_new_tool(store):
    """样本不足时不下结论 —— 新工具偶尔失败不该立刻被判死刑。"""
    store.record("new", False, error="e")
    store.record("new", False, error="e")
    h = store.health("new")
    assert h.healthy is True
    assert "样本不足" in h.reason


def test_consecutive_failures_make_unhealthy(store):
    for _ in range(th.CONSECUTIVE_FAIL_LIMIT):
        store.record("bad", False, error="connection refused")
    h = store.health("bad")
    assert h.healthy is False
    assert "连续失败" in h.reason


def test_high_fail_rate_makes_unhealthy(store):
    """失败率高但**不是**连续失败（交错出现）—— 也要判不健康。

    注意数据要避开"连续失败"规则，否则先命中的是那条，
    就测不到失败率这条了。
    """
    seq = [False, True, False, False, True, False]   # 4/6 失败，尾部只有 1 次连续
    for ok in seq:
        store.record("bad", ok, error="" if ok else "timeout")

    h = store.health("bad")
    assert h.consecutive_failures == 1
    assert h.healthy is False
    assert "失败率" in h.reason


def test_health_recovers_by_itself(store):
    """降级是**算出来的** —— 修好之后必须自动恢复，不能永久拉黑。"""
    for _ in range(3):
        store.record("t", False, error="e")
    assert store.health("t").healthy is False

    for _ in range(6):
        store.record("t", True)
    assert store.health("t").healthy is True, "恢复了却仍然被判不健康"


# ============================================================
# 3. 提示规划层
# ============================================================

def test_health_block_lists_only_unhealthy(store, monkeypatch):
    monkeypatch.setattr(th, "_store", store)
    for _ in range(3):
        store.record("broken_tool", False, error="boom")
    for _ in range(5):
        store.record("good_tool", True)

    block = store.health_block()
    assert "broken_tool" in block
    assert "good_tool" not in block


def test_health_block_gives_actionable_advice(store):
    for _ in range(3):
        store.record("broken", False, error="boom")
    block = store.health_block()
    # 不能只说"坏了"，要给出下一步
    assert "换一条路" in block
    assert "create_tool" in block or "report_capability_gap" in block


def test_health_block_empty_when_all_healthy(store):
    for _ in range(5):
        store.record("ok_tool", True)
    assert store.health_block() == ""


# ============================================================
# 4. 能力缺口
# ============================================================

def test_record_and_list_gaps(store):
    store.record_gap("拼接视频", tried="video_concat", blocked_by="没有 ffmpeg")
    gaps = store.list_gaps()
    assert len(gaps) == 1
    assert gaps[0]["need"] == "拼接视频"
    assert "ffmpeg" in gaps[0]["blocked_by"]


def test_unknown_tool_counts_as_gap_not_health(monkeypatch, store, tmp_path):
    """调不存在的工具 = 能力缺口，不该污染"工具健康度"。

    否则健康度里会混进一堆幽灵工具。
    """
    from tools.hub import ToolHub

    monkeypatch.setattr(th, "_store", store)
    hub = ToolHub()
    import asyncio

    asyncio.run(hub.call_tool("no_such_tool", {}))

    assert store.health("no_such_tool") is None
    assert len(store.list_gaps()) == 1
    assert "no_such_tool" in store.list_gaps()[0]["need"]


def test_call_tool_records_health(monkeypatch, store):
    """正常工具的每次调用都要进流水。"""
    from tools.base import Tool, ToolResult, ToolSchema
    from tools.hub import ToolHub

    monkeypatch.setattr(th, "_store", store)

    class T(Tool):
        name = "rec_tool"
        description = "x"

        def schema(self):
            return ToolSchema(name=self.name, description=self.description)

        async def execute(self, **kw):
            return ToolResult(success=True, data={})

    import asyncio

    hub = ToolHub()
    hub.register_python_tool(T())
    asyncio.run(hub.call_tool("rec_tool", {}))
    asyncio.run(hub.call_tool("rec_tool", {}))

    h = store.health("rec_tool")
    assert h is not None and h.total == 2 and h.ok == 2
