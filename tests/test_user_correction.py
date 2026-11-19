"""用户纠正信号 —— 让「回答不对」真正进得去(docs/14 §2.3)

背景:`core/critic.py` 里有一行 `user_corrected = context.user_feedback is not None`,
但全仓从来没有写过 `user_feedback` —— 「从纠正中学习」这条链是死的。

这里覆盖四段:
1. 检测器 `core/correction.py` —— 规则是否准(误判代价不对称,宁可漏判)
2. critic —— 被纠正的轮次要记成失败,不能记成成功
3. Agent 端到端 —— 上一轮被纠正 → 重评 → 撤掉成功记录 → 采纳率下降
4. WS 分发层 —— `feedback/` topic 的 conn_id 切片必须正确
   (踩过坑:"feedback/" 是 **9** 个字符,写成 topic[8:] 会切出 "/conn-id",
    令牌校验必然失败,反馈永远到不了)
"""
import pytest
from unittest.mock import patch

from core.correction import CorrectionDetector
from core.memory import MemoryStore


# ============================================================
# 1. 检测器
# ============================================================

@pytest.fixture
def detector():
    return CorrectionDetector()


@pytest.mark.parametrize(
    "text,kind",
    [
        ("不对", "negation"),
        ("错了", "negation"),
        ("你搞错了吧", "negation"),
        ("答非所问", "negation"),
        ("不是这个", "negation"),
        ("不是这个，我要的是天气", "replacement"),
        ("我问的是明天的天气", "replacement"),
        ("应该是上海", "replacement"),
        ("重来", "redo"),
        ("重新做一遍", "redo"),
        ("再改一下", "redo"),
    ],
)
def test_detects_corrections(detector, text, kind):
    sig = detector.detect(text, previous_answer="北京今天晴,25度")
    assert sig is not None, f"没检测到纠正: {text!r}"
    assert sig.kind == kind
    assert sig.confidence >= 0.7
    assert sig.text == text


@pytest.mark.parametrize(
    "text",
    [
        "北京今天天气怎么样",
        "帮我查一下上海天气",
        "继续",
        "好的",
        "对",
        "是的",
        "谢谢",
        "收到",
        "不错啊,这个我满意",
        "不是吧?这么贵",
        "这个方案可以",
        "总结一下刚才的内容",
    ],
)
def test_does_not_false_positive(detector, text):
    """正常输入不能被当成纠正 —— 误判会污染记忆库"""
    sig = detector.detect(text, previous_answer="北京今天晴,25度")
    assert sig is None, f"误判为纠正: {text!r} → {sig}"


def test_no_previous_answer_is_not_correction(detector):
    """没有"被纠正的对象"就不构成纠正"""
    assert detector.detect("不对", previous_answer="") is None
    assert detector.detect("不对", previous_answer="   ") is None
    # 显式声明有上一条回答时才算
    sig = detector.detect("不对", previous_answer="", has_previous_answer=True)
    assert sig is not None


def test_long_input_downgrades_confidence(detector):
    """长输入 + 纠正词 更可能是"新任务顺带吐槽",要降权"""
    short = detector.detect("不对", previous_answer="答")
    long_text = "不对，而且我还想问一下，" + "其它内容" * 30
    long_sig = detector.detect(long_text, previous_answer="答")
    assert short is not None
    if long_sig is not None:
        assert long_sig.confidence < short.confidence


def test_signal_to_dict(detector):
    sig = detector.detect("不对，应该是上海", previous_answer="北京")
    d = sig.to_dict()
    assert d["kind"] == "replacement"
    assert d["confidence"] > 0
    assert isinstance(d["matched"], list)


# ============================================================
# 2. critic:被纠正 ≠ 成功
# ============================================================

@pytest.mark.asyncio
async def test_corrected_turn_records_failure_not_success(mem_store):
    """工具全调通但用户说不对 → 必须记成失败,不能记成成功"""
    from core.critic import ExecutionContext, ExecutionCritic, TaskExecutionSummary

    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        critic = ExecutionCritic(memory_store=mem_store)
        ctx = ExecutionContext(
            trace_id="corr_001",
            scenario="weather",
            intent="查天气",
            selected_skill=None,
            tasks=[TaskExecutionSummary(task_id="t1", tool="weather_query", success=True)],
            latency_ms=300.0,
            user_feedback="不对，应该是上海",
        )
        result = await critic.evaluate(ctx)

    assert result.user_corrected is True
    assert "failure_record" in result.records_generated
    assert "success_record" not in result.records_generated
    assert "patch_correction" in result.records_generated

    # 落盘的失败记录带 user_corrected 标记
    failures = mem_store.get_recent_failures(top_k=10)
    assert any(f.user_corrected for f in failures)


@pytest.mark.asyncio
async def test_corrected_turn_does_not_record_success(mem_store):
    """被纠正的轮次不该在成功索引里留一条"""
    from core.critic import ExecutionContext, ExecutionCritic, TaskExecutionSummary

    with patch("core.critic.get_self_evolution_enabled", return_value=True):
        critic = ExecutionCritic(memory_store=mem_store)
        ctx = ExecutionContext(
            trace_id="corr_002",
            scenario="weather",
            intent="查天气",
            selected_skill=None,
            tasks=[TaskExecutionSummary(task_id="t1", tool="weather_query", success=True)],
            latency_ms=300.0,
            user_feedback="错了",
        )
        await critic.evaluate(ctx)

    success_path = mem_store.successes_dir / "success_index.jsonl"
    content = success_path.read_text(encoding="utf-8") if success_path.exists() else ""
    assert "corr_002" not in content


# ============================================================
# 3. MemoryStore:撤销成功记录 + 采纳率
# ============================================================

def test_remove_success(mem_store):
    mem_store.record_success(trace_id="s1", scenario="x", matched_skill="", latency_ms=1.0)
    mem_store.record_success(trace_id="s2", scenario="x", matched_skill="", latency_ms=1.0)
    assert mem_store.remove_success("s1") is True
    assert mem_store.remove_success("s1") is False   # 幂等

    content = (mem_store.successes_dir / "success_index.jsonl").read_text(encoding="utf-8")
    assert "s1" not in content
    assert "s2" in content


def test_adoption_rate_counts_corrections(mem_store):
    mem_store.record_success(trace_id="ok1", scenario="x", matched_skill="", latency_ms=1.0)
    mem_store.record_success(trace_id="ok2", scenario="x", matched_skill="", latency_ms=1.0)
    mem_store.record_failure(
        trace_id="bad1",
        scenario="x",
        intent="i",
        selected_skill="",
        success_rate=1.0,
        fallback_count=0,
        latency_ms=1.0,
        diagnosis="d",
        user_corrected=True,
        execution_id="bad1",
    )
    stats = mem_store.get_adoption_rate()
    assert stats["evaluated"] == 3
    assert stats["successes"] == 2
    assert stats["failures"] == 1
    assert stats["corrected"] == 1
    assert stats["adoption_rate"] == round(2 / 3, 4)


def test_adoption_rate_empty_is_zero(mem_store):
    stats = mem_store.get_adoption_rate()
    assert stats["evaluated"] == 0
    assert stats["adoption_rate"] == 0.0


def test_stats_include_adoption_rate(mem_store):
    stats = mem_store.get_stats()
    assert "adoption_rate" in stats
    assert "corrected" in stats


# ============================================================
# 4. Agent 端到端:上一轮被纠正 → 重评
# ============================================================

class _FakeLLM:
    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self.calls = []

    async def chat_with_retry(self, messages, **kw):
        self.calls.append(list(messages))
        if self._responses:
            return self._responses.pop(0)
        return "好的"

    async def chat(self, messages, **kw):
        return await self.chat_with_retry(messages, **kw)

    async def stream(self, messages, **kw):
        self.calls.append(list(messages))
        yield "好的"


def _patch_llm(monkeypatch, fake):
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    from infra import llm as llm_mod

    llm_mod._llm_client = None
    return fake


@pytest.mark.asyncio
async def test_agent_detects_correction_and_reevaluates(monkeypatch, tmp_path):
    """第 2 轮说"不对" → 第 1 轮被重评,成功记录被撤销"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    store = MemoryStore(base_path=tmp_path / "mem")
    _patch_llm(monkeypatch, _FakeLLM())
    # critic 默认是关的(self_evolution_enabled=False),不打开就没有记录
    monkeypatch.setattr("core.critic.get_self_evolution_enabled", lambda: True)

    agent = Agent(persist_session=False)
    agent.critic.memory = store
    agent.memory = store

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="任务",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    # 第 1 轮:正常回答
    await agent.handle("帮我查天气", on_event=None)

    # 第 1 轮被记成成功(工具全通 + 没被纠正)
    success_path = store.successes_dir / "success_index.jsonl"
    assert success_path.exists()
    assert agent._last_exec_ctx is not None
    exec_id = agent._last_exec_ctx.execution_id

    # 第 2 轮:用户纠正
    await agent.handle("不对，应该是上海", on_event=None)

    # 成功记录被撤销
    content = success_path.read_text(encoding="utf-8") if success_path.exists() else ""
    assert exec_id not in content

    # 产生了带 user_corrected 的失败记录
    failures = store.get_recent_failures(top_k=10)
    assert any(f.user_corrected for f in failures), "没有记录用户纠正"

    # 采纳率反映了这次纠正
    stats = store.get_adoption_rate()
    assert stats["corrected"] >= 1


@pytest.mark.asyncio
async def test_agent_normal_followup_does_not_trigger_correction(monkeypatch, tmp_path):
    """普通追问不能被当成纠正"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    store = MemoryStore(base_path=tmp_path / "mem")
    _patch_llm(monkeypatch, _FakeLLM())
    # critic 默认是关的(self_evolution_enabled=False),不打开就没有记录
    monkeypatch.setattr("core.critic.get_self_evolution_enabled", lambda: True)

    agent = Agent(persist_session=False)
    agent.critic.memory = store
    agent.memory = store

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="任务",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    await agent.handle("帮我查天气", on_event=None)
    exec_id = agent._last_exec_ctx.execution_id
    await agent.handle("那再帮我查一下上海", on_event=None)

    failures = store.get_recent_failures(top_k=10)
    assert not any(f.user_corrected for f in failures)
    success_path = store.successes_dir / "success_index.jsonl"
    content = success_path.read_text(encoding="utf-8") if success_path.exists() else ""
    assert exec_id in content


@pytest.mark.asyncio
async def test_agent_explicit_feedback_rejects_answer(monkeypatch, tmp_path):
    """前端 👎 → 同样触发重评"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    store = MemoryStore(base_path=tmp_path / "mem")
    _patch_llm(monkeypatch, _FakeLLM())
    # critic 默认是关的(self_evolution_enabled=False),不打开就没有记录
    monkeypatch.setattr("core.critic.get_self_evolution_enabled", lambda: True)

    agent = Agent(persist_session=False)
    agent.critic.memory = store
    agent.memory = store

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="任务",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    await agent.handle("帮我查天气", on_event=None)
    exec_id = agent._last_exec_ctx.execution_id

    ok = agent.apply_explicit_feedback(exec_id, accepted=False, comment="查错城市了")
    assert ok is True
    await agent.flush_feedback()

    failures = store.get_recent_failures(top_k=10)
    assert any(f.user_corrected for f in failures)


@pytest.mark.asyncio
async def test_agent_explicit_feedback_unknown_execution(monkeypatch, tmp_path):
    """评价一个不存在的执行 → 不报错,返回 False"""
    from core.agent import Agent

    store = MemoryStore(base_path=tmp_path / "mem")
    _patch_llm(monkeypatch, _FakeLLM())
    # critic 默认是关的(self_evolution_enabled=False),不打开就没有记录
    monkeypatch.setattr("core.critic.get_self_evolution_enabled", lambda: True)
    agent = Agent(persist_session=False)
    agent.critic.memory = store
    agent.memory = store

    assert agent.apply_explicit_feedback("no-such-id", accepted=False) is False


@pytest.mark.asyncio
async def test_agent_explicit_feedback_accepted_is_noop(monkeypatch, tmp_path):
    """👍 只是确认,不产生失败记录"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    store = MemoryStore(base_path=tmp_path / "mem")
    _patch_llm(monkeypatch, _FakeLLM())
    # critic 默认是关的(self_evolution_enabled=False),不打开就没有记录
    monkeypatch.setattr("core.critic.get_self_evolution_enabled", lambda: True)

    agent = Agent(persist_session=False)
    agent.critic.memory = store
    agent.memory = store

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="任务",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    await agent.handle("帮我查天气", on_event=None)
    exec_id = agent._last_exec_ctx.execution_id
    assert agent.apply_explicit_feedback(exec_id, accepted=True) is True
    await agent.flush_feedback()

    failures = store.get_recent_failures(top_k=10)
    assert not any(f.user_corrected for f in failures)


@pytest.mark.asyncio
async def test_message_final_carries_execution_id(monkeypatch):
    """前端靠 message_final 里的 execution_id 关联反馈"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    _patch_llm(monkeypatch, _FakeLLM())
    # critic 默认是关的(self_evolution_enabled=False),不打开就没有记录
    monkeypatch.setattr("core.critic.get_self_evolution_enabled", lambda: True)
    agent = Agent(persist_session=False)

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="任务",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    events = []

    async def on_event(event, payload):
        events.append((event, payload))

    await agent.handle("帮我查天气", on_event=on_event)

    finals = [p for e, p in events if e == "message_final"]
    assert finals
    assert finals[0].get("execution_id")


# ============================================================
# 4. WS 分发层:feedback topic 的 conn_id 切片
# ============================================================

@pytest.mark.asyncio
async def test_feedback_topic_slices_conn_id_correctly():
    """"feedback/" 是 9 个字符 —— 切片写错一个字符,反馈就永远到不了。

    这不是假想:踩过一次(topic[8:] 切出 "/conn-id",令牌校验必然失败,
    前端表现为"点了👎没反应")。
    """
    from backend.session import sessions
    from backend.websocket_handler import dispatcher

    conn_id = "conn-feedback-test"
    identity = await sessions.issue_identity(conn_id)
    try:
        # 切片必须正好去掉 "feedback/" 前缀,不多不少
        assert f"feedback/{conn_id}"[9:] == conn_id
        assert f"feedback/{conn_id}"[8:] != conn_id   # 少切一位就错了

        # 走一遍真实分发:有效令牌 → 受理;并把 ack 事件抓下来
        pushed = []

        async def fake_push(client_id, event, payload):
            pushed.append((client_id, event, payload))

        import backend.websocket_handler as ws_mod

        original = ws_mod.push_event
        ws_mod.push_event = fake_push
        try:
            class _Sub:
                topic = f"feedback/{conn_id}"

            await dispatcher(
                _Sub(),
                {
                    "execution_id": "exec-not-exist",
                    "accepted": False,
                    "comment": "不对",
                },
            )
        finally:
            ws_mod.push_event = original

        # 推的 client_id 必须是原始 conn_id(不能带上前导 "/")
        assert pushed, "没有推送 feedback_ack"
        target, event, payload = pushed[-1]
        assert target == conn_id
        assert event == "feedback_ack"
        assert payload["ok"] is False   # 执行上下文不存在 → 不受理
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_feedback_topic_rejects_uninitialized_conn():
    """连接未初始化 → 推 error 事件,不能静默"""
    from backend.websocket_handler import dispatcher

    pushed = []

    async def fake_push(client_id, event, payload):
        pushed.append((client_id, event, payload))

    import backend.websocket_handler as ws_mod

    original = ws_mod.push_event
    ws_mod.push_event = fake_push
    try:
        class _Sub:
            topic = "feedback/never-initialized"

        await dispatcher(_Sub(), {"token": "forged", "execution_id": "x"})
    finally:
        ws_mod.push_event = original

    assert pushed
    assert pushed[-1][1] == "error"
