"""上下文模块的回归测试。

**每一个用例都对应一个真实踩过的坑** —— 这些坑在修之前完全没有测试覆盖,
所以"测试全绿"并不代表上下文模块是对的。改这块代码前先看这个文件。

修复清单:
  1. ``_truncate`` 的 token 预算形同虚设(单条长消息直接击穿)
  2. ``tool_tasks=[]`` 被当成"没有记录" → 重试重跑上一轮的旧工具
  3. ``set_last_tool_tasks`` 把 tool_tasks 挂到用户消息上
  4. ``tool_tasks`` 不落库 → 会话恢复后重试失效
  5. ``get_recent(0)`` 返回全部消息
  6. 滚动摘要只增不减 + 内存与数据库不一致
  7. ``reset()`` 之后旧摘要复活
  8. ``clear()`` 把滚动摘要的记账基准一起清了
  9. 每条消息开关一次 SQLite 连接(最贵的一笔开销)
  10. sink 失败被静默吞掉
  11. 常驻 system 指令永远送不到 LLM
  12. 历史剔除靠"砍最后一条"(并发下会砍错)
"""
import pytest

from core.context import Context
from core.session_memory import SessionMemoryStore


@pytest.fixture
def patched_store(monkeypatch, tmp_path):
    """把会话存储单例指到临时目录,避免污染真实库。"""
    import core.session_memory as sm_mod

    store = SessionMemoryStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(sm_mod, "get_session_memory_store", lambda: store)
    return store


# ===== 1. token 预算必须真的守住 =====

def test_truncate_enforces_char_budget():
    """单条巨无霸消息不能把预算撑爆。"""
    ctx = Context()
    ctx.add_user_message("x" * 100000)
    ctx.add_assistant_message("y" * 100000)

    out = ctx.to_llm_messages(max_tokens=1000)
    total = sum(len(m.get("content") or "") for m in out)
    assert total <= 1000 * 2, f"预算没守住:{total} > 2000"


def test_truncate_keeps_recent_messages():
    """超预算时保最近的内容,砍最早的。"""
    ctx = Context()
    for i in range(50):
        ctx.add_user_message(f"第{i}条" + "内容" * 100)

    out = ctx.to_llm_messages(max_tokens=1000)
    contents = [m.get("content") or "" for m in out]
    assert any("第49条" in c for c in contents), "最近的对话必须留着"
    assert not any("第0条" in c for c in contents), "最早的对话应该被砍掉"


def test_truncate_keeps_system_messages():
    """system 是指令,裁剪时不能丢。"""
    ctx = Context()
    ctx.add_system_message("你是天气助手")
    for i in range(50):
        ctx.add_user_message("内容" * 200)

    out = ctx.to_llm_messages(max_tokens=1000)
    assert any(
        m.get("role") == "system" and "天气助手" in (m.get("content") or "")
        for m in out
    )


def test_truncate_with_empty_context():
    assert Context().to_llm_messages(max_tokens=100) == []


# ===== 2/3. tool_tasks 的归属与空列表 =====

def test_empty_tool_tasks_does_not_fall_back_to_stale():
    """本轮不需要工具([]) ≠ 没有记录(None)。

    以前用 ``if msg.tool_tasks:`` 判断,空列表被跳过,重试就会重跑
    更早一轮的旧工具。
    """
    ctx = Context()
    ctx.set_last_tool_tasks([{"id": "t1", "type": "weather"}])
    ctx.set_last_tool_tasks([])

    assert ctx.get_last_tool_tasks() == []
    assert ctx.get_last_tool_tasks() is not None


def test_no_tool_tasks_record_is_none():
    ctx = Context()
    assert ctx.get_last_tool_tasks() is None


def test_tool_tasks_not_attached_to_messages():
    """tool_tasks 不该挂在任何一条消息上。

    以前挂在 ``messages[-1]`` 上,而写进去的那一刻最后一条还是**用户**
    消息 —— 本轮的工具计划被记在了用户发言上。
    """
    ctx = Context()
    ctx.add_user_message("你好")
    ctx.set_last_tool_tasks([{"id": "t1", "type": "weather"}])

    assert all(m.tool_tasks is None for m in ctx.messages)
    assert ctx.get_last_tool_tasks() == [{"id": "t1", "type": "weather"}]


def test_assistant_message_does_not_inherit_last_tool_tasks():
    """闲聊轮不能把上一轮的工具计划当成自己的。"""
    ctx = Context()
    ctx.set_last_tool_tasks([{"id": "t1", "type": "weather"}])

    msg = ctx.add_assistant_message("你好呀")
    assert msg.tool_tasks is None


# ===== 4. tool_tasks 落库 + 恢复 =====

def test_tool_tasks_roundtrip_through_db(tmp_path):
    store = SessionMemoryStore(db_path=tmp_path / "s.db")
    store.append_message(
        "s1", "u1", "assistant", "回答",
        tool_tasks=[{"id": "t1", "type": "weather"}],
    )
    store.append_message("s1", "u1", "assistant", "不用工具", tool_tasks=[])

    msgs = store.get_messages("s1")
    assert msgs[0].tool_tasks == [{"id": "t1", "type": "weather"}]
    assert msgs[1].tool_tasks == [], "空列表要能区分于 None(不知道)"


def test_tool_tasks_absent_is_none(tmp_path):
    store = SessionMemoryStore(db_path=tmp_path / "s.db")
    store.append_message("s1", "u1", "user", "hi")
    assert store.get_messages("s1")[0].tool_tasks is None


@pytest.mark.asyncio
async def test_agent_restores_last_tool_tasks(patched_store):
    """会话恢复后仍知道上一轮调了哪些工具(重试要用)。"""
    from core.agent import Agent

    a1 = Agent(session_id="s-rt", user_id="u1", persist_session=True)
    a1.context.add_user_message("查天气")
    a1.context.add_assistant_message(
        "好的", tool_tasks=[{"id": "t1", "type": "weather"}]
    )

    a2 = Agent(session_id="s-rt", user_id="u1", persist_session=True)
    assert a2.context.get_last_tool_tasks() == [{"id": "t1", "type": "weather"}]


# ===== 5. get_recent(0) =====

def test_get_recent_zero_returns_empty():
    ctx = Context()
    for i in range(5):
        ctx.add_user_message(f"m{i}")

    assert ctx.get_recent(0) == []
    assert len(ctx.get_recent(2)) == 2
    assert len(ctx.get_recent(99)) == 5


# ===== 6. 摘要封顶 + 内存/库一致 =====

@pytest.mark.asyncio
async def test_rollup_summary_is_capped(patched_store, monkeypatch):
    """摘要不能无限增长(每轮都注入 prompt,越滚越胖)。"""
    import core.session_rollup as rollup_mod

    async def fake_summary(messages):
        return "摘" * 400

    monkeypatch.setattr(rollup_mod, "summarize_messages", fake_summary)

    ctx = Context()
    for i in range(60):
        m = ctx.add_message("user", f"msg{i}")
        m.seq = i + 1
        await rollup_mod.maybe_rollup(
            ctx, patched_store, session_id="s-cap", user_id="u1",
            threshold=10, keep_recent=5,
        )

    assert len(ctx.get_summary()) <= Context.MAX_SUMMARY_CHARS + 40


@pytest.mark.asyncio
async def test_rollup_summary_in_db_matches_memory(patched_store, monkeypatch):
    """库里存的摘要必须和内存里的一致。

    以前落库只存"最新一段",内存里是"所有段拼起来" —— 重启后摘要会缩水,
    已压缩掉的内容从 LLM 视角消失。
    """
    import core.session_rollup as rollup_mod

    async def fake_summary(messages):
        return "摘" * 300

    monkeypatch.setattr(rollup_mod, "summarize_messages", fake_summary)

    ctx = Context()
    for i in range(60):
        m = ctx.add_message("user", f"msg{i}")
        m.seq = i + 1
        await rollup_mod.maybe_rollup(
            ctx, patched_store, session_id="s-sync", user_id="u1",
            threshold=10, keep_recent=5,
        )

    stored = patched_store.get_latest_summary("s-sync")
    assert stored is not None
    assert stored.summary == ctx.get_summary()


@pytest.mark.asyncio
async def test_summary_survives_session_restore(patched_store, monkeypatch):
    """重启后摘要不能缩水。"""
    import core.session_rollup as rollup_mod
    from core.agent import Agent

    async def fake_summary(messages):
        return "摘" * 300

    monkeypatch.setattr(rollup_mod, "summarize_messages", fake_summary)

    ctx = Context()
    for i in range(30):
        m = ctx.add_message("user", f"msg{i}")
        m.seq = i + 1
    await rollup_mod.maybe_rollup(
        ctx, patched_store, session_id="s-rs", user_id="u1",
        threshold=10, keep_recent=5,
    )
    expected = ctx.get_summary()

    agent = Agent(session_id="s-rs", user_id="u1", persist_session=True)
    assert agent.context.get_summary() == expected


# ===== 7. reset 之后旧摘要不该回来 =====

@pytest.mark.asyncio
async def test_reset_drops_pre_reset_summary(patched_store):
    """reset 掉的是"旧话题",连它的摘要一起清掉。

    以前 reset 只清内存,库里的摘要还在,新 Agent 起来又把它塞回 prompt ——
    reset 等于没做。
    """
    from core.agent import Agent

    agent = Agent(session_id="s-reset", user_id="u1", persist_session=True)
    agent.context.add_user_message("旧话题")
    agent.context.add_assistant_message("旧回答")
    patched_store.save_summary(
        session_id="s-reset", user_id="u1", cover_until_seq=1, summary="[旧摘要]"
    )

    agent.reset()

    agent2 = Agent(session_id="s-reset", user_id="u1", persist_session=True)
    assert agent2.context.get_summary() == ""


@pytest.mark.asyncio
async def test_summary_after_reset_is_kept(patched_store):
    """reset 之后产生的新摘要要能正常装回。"""
    from core.agent import Agent

    agent = Agent(session_id="s-reset2", user_id="u1", persist_session=True)
    agent.context.add_user_message("旧话题")
    agent.reset()
    agent.context.add_user_message("新话题")
    patched_store.save_summary(
        session_id="s-reset2", user_id="u1", cover_until_seq=99, summary="[新摘要]"
    )

    agent2 = Agent(session_id="s-reset2", user_id="u1", persist_session=True)
    assert agent2.context.get_summary() == "[新摘要]"


# ===== 8. clear() 不该动持久化簿记 =====

def test_clear_keeps_rollup_bookkeeping():
    """``rollup_cover_seq`` 是持久化簿记,不是会话态。"""
    ctx = Context()
    ctx.add_user_message("x")
    ctx.rollup_cover_seq = 42
    ctx.metadata["clarify_round"] = 1

    ctx.clear()

    assert ctx.rollup_cover_seq == 42, "清掉它会让下次滚动摘要重复摘要一遍"
    assert ctx.metadata == {}, "会话态要清"
    assert ctx.get_summary() == ""
    assert ctx.get_last_tool_tasks() is None


# ===== 9. 连接复用 =====

def test_store_reuses_connection(tmp_path, monkeypatch):
    """写一条消息不该新建一次连接 —— 这是会话写入最贵的一笔开销。"""
    import core.sqlite_util as sqlite_util

    calls = []
    orig = sqlite_util.connect

    def counting(*args, **kwargs):
        calls.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(sqlite_util, "connect", counting)

    store = SessionMemoryStore(db_path=tmp_path / "s.db")
    calls.clear()

    for i in range(5):
        store.append_message("s1", "u1", "user", f"m{i}")
        store.get_messages("s1")

    assert calls == [], f"每条消息都新建连接(共 {len(calls)} 次)"


def test_store_is_thread_safe(tmp_path):
    """多线程并发写不能丢数据、不能报错。

    连接是**每线程一个**的 —— 共用一个连接会让两个线程的语句落进同一个
    事务,谁先 commit 谁倒霉。
    """
    import threading

    store = SessionMemoryStore(db_path=tmp_path / "s.db")
    store.ensure_session("s1", "u1")

    errors = []

    def writer(n):
        try:
            for i in range(10):
                store.append_message("s1", "u1", "user", f"t{n}-{i}")
        except Exception as exc:  # pragma: no cover - 出错就说明并发有问题
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写报错: {errors}"
    assert store.count_messages("s1") == 40


# ===== 10. sink 失败必须留下痕迹 =====

def test_sink_failure_is_logged_not_swallowed():
    """落库失败不能影响对话,但也不能静默吞掉。"""
    from infra.logger import get_logger

    log = get_logger()
    saved = list(log._subscribers)
    records = []
    log.subscribe(lambda level, comp, msg, data: records.append((level, comp, msg)))
    try:
        def boom(msg):
            raise RuntimeError("落库炸了")

        ctx = Context()
        ctx.sink = boom
        ctx.add_user_message("你好")  # 不能抛
        assert len(ctx) == 1, "落库失败不能影响对话本身"
        assert records, "落库失败必须留下日志痕迹"
        assert any("落库" in r[2] for r in records)
    finally:
        log._subscribers[:] = saved


# ===== 11. 常驻 system 指令要真的进 prompt =====

def test_system_message_reaches_manager_prompt():
    from agents.manager import ManagerAgent

    ctx = Context()
    ctx.add_system_message("你是天气助手")
    ctx.add_user_message("北京天气")

    prompt = ManagerAgent()._build_user_prompt("北京天气", ctx)
    assert "你是天气助手" in prompt


def test_system_message_reaches_orchestrator_prompt():
    from agents.orchestrator import OrchestratorAgent

    ctx = Context()
    ctx.add_system_message("你是天气助手")
    ctx.add_user_message("北京天气")

    block = OrchestratorAgent()._build_context_block(ctx, "北京天气")
    assert "你是天气助手" in block


def test_system_messages_are_not_in_message_list():
    """system 指令不能混进 messages —— 混进去会被按 role 过滤掉。"""
    ctx = Context()
    ctx.add_system_message("你是天气助手")
    ctx.add_user_message("北京天气")

    assert [m.role for m in ctx.messages] == ["user"]
    assert ctx.system_block() == "你是天气助手"


# ===== 12. 历史剔除要认内容,不能砍最后一条 =====

def test_history_exclusion_matches_content_not_position():
    """并发下最后一条未必是本轮输入。"""
    ctx = Context()
    ctx.add_user_message("第一句")
    ctx.add_assistant_message("回答")
    ctx.add_user_message("当前问题")
    ctx.add_assistant_message("别的分支追加的")

    prior = [m.content for m in ctx.history_messages(exclude_content="当前问题")]
    assert "当前问题" not in prior
    assert "第一句" in prior
    assert "别的分支追加的" in prior


def test_history_exclusion_keeps_all_when_no_match():
    """本轮输入不在上下文里时,不要误伤历史。"""
    ctx = Context()
    ctx.add_user_message("第一句")
    ctx.add_assistant_message("回答")

    prior = [m.content for m in ctx.history_messages(exclude_content="不在上下文里")]
    assert prior == ["第一句", "回答"]


def test_manager_prompt_does_not_repeat_current_input():
    from agents.manager import ManagerAgent

    ctx = Context()
    ctx.add_user_message("第一句")
    ctx.add_assistant_message("回答")
    ctx.add_user_message("当前问题")
    ctx.add_assistant_message("并发追加")

    prompt = ManagerAgent()._build_user_prompt("当前问题", ctx)
    assert prompt.count("当前问题") == 1, "本轮输入不该在历史里重复出现"
    assert "第一句" in prompt


# ===== 13. 快照隔离 =====

def test_snapshot_is_isolated_and_has_no_sink():
    ctx = Context()
    ctx.add_user_message("第一条")
    ctx.sink = lambda m: None

    snap = ctx.snapshot()
    ctx.add_user_message("第二条")

    assert len(snap) == 1
    assert len(ctx) == 2
    assert snap.sink is None, "快照不能再往库里写"
