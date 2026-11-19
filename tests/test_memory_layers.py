"""记忆三层的契约测试（`core/memorykit/`）。

这些用例守的不是"功能"，而是**分层规则**：一旦有人把工作记忆当短期用、
或让工具直写长期记忆，这里会先红。
"""
import json

import pytest

from core.memorykit import (
    MAX_SLOTS,
    SLOT_TOOL_RESULTS,
    WorkingMemory,
    get_current_working,
    get_memory,
    reset_memory,
)
from core.memorykit.working import MAX_VALUE_CHARS


# ============================================================
# 工作记忆：一轮一个，轮末必须清空
# ============================================================

def test_working_memory_is_turn_scoped():
    mem = get_memory()
    wm = mem.begin_turn(execution_id="exec-1", session_id="s-1")
    assert get_current_working() is wm

    wm.set_tool_results({"weather_query": {"ok": True}})
    assert wm.get_tool_results() == {"weather_query": {"ok": True}}

    mem.end_turn(wm)
    assert wm.keys() == []          # 清空
    assert get_current_working() is None


def test_working_memory_value_is_bounded():
    wm = WorkingMemory(execution_id="e", max_value_chars=10)
    entry = wm.write("big", "x" * 100)
    assert entry.truncated is True
    assert len(entry.value) < 100
    assert "截断" in entry.value


def test_working_memory_evicts_oldest_slot():
    wm = WorkingMemory(execution_id="e", max_slots=2)
    wm.write("a", 1)
    wm.write("b", 2)
    wm.write("c", 3)
    assert len(wm.keys()) == 2
    assert "a" not in wm.keys()     # 保新不保旧
    assert set(wm.keys()) == {"b", "c"}
    assert MAX_SLOTS > 2            # 常量本身没被测试改坏


def test_findings_accumulate_as_text_for_replan():
    wm = WorkingMemory(execution_id="e")
    wm.add_finding("查到 3 条政策")
    wm.add_finding("其中 1 条过期")
    text = wm.findings_text()
    assert "3 条政策" in text and "1 条过期" in text


def test_end_turn_dumps_only_on_failure(tmp_path):
    from core.memorykit import MemoryHub

    mem = MemoryHub(turns_dir=tmp_path)
    wm = mem.begin_turn(execution_id="exec-ok", session_id="s")
    wm.set_tool_results({"x": 1})
    assert mem.end_turn(wm, failed=False) is None

    wm = mem.begin_turn(execution_id="exec-bad", session_id="s")
    wm.set_tool_results({"x": 1})
    path = mem.end_turn(wm, failed=True, reason="boom")
    assert path is not None
    assert str(tmp_path) in path
    assert str(tmp_path) in path
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["reason"] == "boom"
    assert SLOT_TOOL_RESULTS in data["slots"]


# ============================================================
# 短期记忆：只是门面，写入口唯一
# ============================================================

def test_short_term_delegates_to_session_store(monkeypatch):
    from core.memorykit.short_term import ShortTermMemory

    calls = {}

    class _FakeStore:
        def append_message(self, **kw):
            calls.update(kw)
            return 7

    st = ShortTermMemory(store=_FakeStore())
    seq = st.append_message("s-1", "user", "你好", turn_id="t-1")
    assert seq == 7
    assert calls["session_id"] == "s-1"
    assert calls["role"] == "user"


def test_short_term_resolves_store_lazily(monkeypatch):
    """不缓存 store —— 否则测试隔离/切换库会串。"""
    from core.memorykit.short_term import ShortTermMemory
    import core.session_memory as sm

    fake = object()
    monkeypatch.setattr(sm, "get_session_memory_store", lambda: fake)
    st = ShortTermMemory()
    assert st.store is fake
    another = object()
    monkeypatch.setattr(sm, "get_session_memory_store", lambda: another)
    assert st.store is another


# ============================================================
# 长期记忆：读只走一个入口
# ============================================================

def test_long_term_profile_block_is_bounded(monkeypatch):
    from core.memorykit.long_term import LongTermMemory

    class _FakeProfile:
        def build_profile_block(self, limit=20, max_chars=400):
            assert max_chars == 400       # 常驻注入的预算不能被随手放大
            return "[用户画像]\n- [偏好] 用中文"

    lt = LongTermMemory()
    monkeypatch.setattr(type(lt), "profile_store", property(lambda self: _FakeProfile()))
    assert "用中文" in lt.profile_block()


def test_long_term_hints_failure_degrades(monkeypatch):
    from core.memorykit.long_term import LongTermMemory

    lt = LongTermMemory()

    class _Boom:
        def get_skill_hints(self, q):
            raise RuntimeError("json 坏了")

    monkeypatch.setattr(type(lt), "experience", property(lambda self: _Boom()))
    assert lt.hints("查天气") == []      # 读挂了不能影响规划


# ============================================================
# 主链集成：一轮结束后三层各就各位
# ============================================================

class _RecordingLLM:
    async def chat_with_retry(self, messages, **kw):
        return "好的"

    async def chat(self, messages, **kw):
        return "好的"

    async def stream(self, messages, **kw):
        yield "好的"


@pytest.mark.asyncio
async def test_handle_ends_working_memory(monkeypatch):
    """一轮结束后：工作记忆被清空 + 不残留当前轮引用。"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    fake = _RecordingLLM()
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    from infra import llm as llm_mod

    llm_mod._llm_client = None

    agent = Agent(persist_session=False)

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.CHITCHAT,
            intent_detail="闲聊",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    await agent.handle("在吗", on_event=None)

    assert agent.working is None
    assert get_current_working() is None


def test_reset_memory_clears_current_turn():
    mem = get_memory()
    mem.begin_turn(execution_id="e2", session_id="s")
    assert get_current_working() is not None
    reset_memory()
    assert get_current_working() is None
