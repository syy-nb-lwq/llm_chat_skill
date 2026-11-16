"""会话内容持久化(短期记忆)测试。

覆盖:
  - 消息全量保留(不设过期、不删除)
  - 会话恢复(重启/重连后 context 能装回历史)
  - 会话归属校验(越权不能恢复他人会话)
  - CJK 二元组检索
  - 滚动摘要(原文保留,只压缩视图)
  - reset 语义(清空工作上下文,内容仍在)
"""
import pytest

from core.session_memory import (
    SessionMemoryStore,
    query_fragments,
    score_fragments,
)


@pytest.fixture
def store(tmp_path):
    return SessionMemoryStore(db_path=tmp_path / "sessions.db")


# ===== 写入与读取 =====

def test_append_and_read(store):
    store.append_message("s1", "u1", "user", "你好")
    store.append_message("s1", "u1", "assistant", "你好，有什么可以帮你")
    msgs = store.get_messages("s1")
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "你好"
    assert msgs[1].seq == 2


def test_seq_is_monotonic(store):
    for i in range(5):
        store.append_message("s1", "u1", "user", f"消息{i}")
    msgs = store.get_messages("s1")
    assert [m.seq for m in msgs] == [1, 2, 3, 4, 5]


def test_messages_are_all_retained(store):
    """会话内容全部保留 —— 没有 TTL、没有自动淘汰。"""
    for i in range(200):
        store.append_message("s1", "u1", "user", f"消息{i}")
    assert store.count_messages("s1") == 200
    assert len(store.get_messages("s1", limit=0)) == 200


def test_get_recent_returns_last_n_in_order(store):
    for i in range(10):
        store.append_message("s1", "u1", "user", f"消息{i}")
    recent = store.get_recent_messages("s1", limit=3)
    assert [m.content for m in recent] == ["消息7", "消息8", "消息9"]


def test_messages_isolated_by_session(store):
    store.append_message("s1", "u1", "user", "甲")
    store.append_message("s2", "u1", "user", "乙")
    assert len(store.get_messages("s1")) == 1
    assert store.get_messages("s1")[0].content == "甲"


def test_messages_filtered_by_user(store):
    store.append_message("s1", "u1", "user", "甲")
    store.append_message("s1", "u2", "user", "乙")
    assert len(store.get_messages("s1", user_id="u1")) == 1
    assert len(store.get_messages("s1", user_id="u2")) == 1


# ===== 会话归属 =====

def test_session_exists_checks_ownership(store):
    store.ensure_session("s1", "u1")
    assert store.session_exists("s1") is True
    assert store.session_exists("s1", "u1") is True
    assert store.session_exists("s1", "u2") is False
    assert store.session_exists("s-unknown") is False


def test_delete_session_removes_everything(store):
    store.append_message("s1", "u1", "user", "甲")
    assert store.delete_session("s1") is True
    assert store.count_messages("s1") == 0
    assert store.session_exists("s1") is False


def test_list_sessions(store):
    store.ensure_session("s1", "u1")
    store.ensure_session("s2", "u2")
    assert len(store.list_sessions()) == 2
    assert len(store.list_sessions("u1")) == 1


# ===== CJK 检索 =====

def test_query_fragments_cjk_bigrams():
    frags = query_fragments("北京天气")
    assert "北京" in frags
    assert "天气" in frags
    assert len(frags) == 3  # 北京 / 京天 / 天气


def test_query_fragments_ascii_words():
    frags = query_fragments("deploy the api")
    assert "deploy" in frags
    assert "api" in frags


def test_score_fragments():
    frags = query_fragments("天气")
    assert score_fragments("今天天气不错", frags) == 1.0
    assert score_fragments("完全无关", frags) == 0.0


def test_search_finds_chinese_content(store):
    store.append_message("s1", "u1", "user", "帮我看看北京明天的天气")
    store.append_message("s1", "u1", "assistant", "北京明天晴,气温 25 度")
    store.append_message("s1", "u1", "user", "顺便查一下上海")
    hits = store.search("s1", "北京天气", limit=3)
    assert hits
    assert any("北京" in h.content for h in hits)


def test_search_returns_empty_for_unrelated(store):
    store.append_message("s1", "u1", "user", "帮我看看北京明天的天气")
    assert store.search("s1", "数据库索引优化", limit=3) == []


def test_search_scoped_to_session(store):
    store.append_message("s1", "u1", "user", "北京天气")
    store.append_message("s2", "u1", "user", "北京天气")
    hits = store.search("s1", "北京天气")
    assert len(hits) == 1


def test_search_ignores_empty_query(store):
    store.append_message("s1", "u1", "user", "内容")
    assert store.search("s1", "") == []
    assert store.search("s1", "   ") == []


# ===== 摘要 =====

def test_summary_roundtrip(store):
    store.save_summary("s1", "u1", 10, "早期讨论了天气查询")
    latest = store.get_latest_summary("s1")
    assert latest is not None
    assert latest.cover_until_seq == 10
    assert "天气" in latest.summary


def test_latest_summary_wins(store):
    store.save_summary("s1", "u1", 5, "第一段")
    store.save_summary("s1", "u1", 12, "第二段")
    latest = store.get_latest_summary("s1")
    assert latest.cover_until_seq == 12


def test_no_summary_returns_none(store):
    assert store.get_latest_summary("s1") is None


# ===== reset 语义 =====

def test_mark_reset_keeps_messages(store):
    store.append_message("s1", "u1", "user", "甲")
    store.append_message("s1", "u1", "assistant", "乙")
    seq = store.mark_reset("s1")
    assert seq == 2
    # 内容仍在
    assert store.count_messages("s1") == 2
    assert store.get_reset_at_seq("s1") == 2


def test_mark_reset_on_unknown_session(store):
    assert store.mark_reset("s-unknown") == 0


# ===== Agent 集成 =====

@pytest.fixture
def patched_store(monkeypatch, tmp_path):
    """把会话存储单例指到临时目录,避免污染真实库。"""
    import core.session_memory as sm_mod

    store = SessionMemoryStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(sm_mod, "get_session_memory_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_agent_persists_messages(patched_store):
    from core.agent import Agent

    agent = Agent(session_id="s-persist", user_id="u1", persist_session=True)
    agent.context.add_user_message("第一条")
    agent.context.add_assistant_message("第二条")

    msgs = patched_store.get_messages("s-persist")
    assert [m.content for m in msgs] == ["第一条", "第二条"]


@pytest.mark.asyncio
async def test_agent_restores_history(patched_store):
    """新 Agent 接上同一会话 → 历史被装回 context。"""
    from core.agent import Agent

    a1 = Agent(session_id="s-restore", user_id="u1", persist_session=True)
    a1.context.add_user_message("你好")
    a1.context.add_assistant_message("你好，有什么可以帮你")

    a2 = Agent(session_id="s-restore", user_id="u1", persist_session=True)
    contents = [m.content for m in a2.context.messages]
    assert "你好" in contents
    assert "你好，有什么可以帮你" in contents


@pytest.mark.asyncio
async def test_agent_restore_does_not_duplicate(patched_store):
    """恢复后继续对话,不应把旧消息重复写回库。"""
    from core.agent import Agent

    a1 = Agent(session_id="s-dup", user_id="u1", persist_session=True)
    a1.context.add_user_message("旧消息")

    a2 = Agent(session_id="s-dup", user_id="u1", persist_session=True)
    a2.context.add_user_message("新消息")

    msgs = patched_store.get_messages("s-dup")
    assert [m.content for m in msgs] == ["旧消息", "新消息"]


@pytest.mark.asyncio
async def test_agent_restore_is_user_scoped(patched_store):
    """别人的会话不能被恢复。"""
    from core.agent import Agent

    a1 = Agent(session_id="s-priv", user_id="u1", persist_session=True)
    a1.context.add_user_message("u1 的私密内容")

    a2 = Agent(session_id="s-priv", user_id="u2", persist_session=True)
    assert len(a2.context.messages) == 0


@pytest.mark.asyncio
async def test_agent_reset_keeps_stored_content(patched_store):
    """reset 清空工作上下文,但内容仍在库里。"""
    from core.agent import Agent

    agent = Agent(session_id="s-reset", user_id="u1", persist_session=True)
    agent.context.add_user_message("旧内容")
    agent.reset()
    assert len(agent.context.messages) == 0
    assert patched_store.count_messages("s-reset") == 1

    # 重新建 Agent:旧内容不再装回工作上下文,但仍可检索
    again = Agent(session_id="s-reset", user_id="u1", persist_session=True)
    assert len(again.context.messages) == 0
    hits = patched_store.search("s-reset", "旧内容")
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_agent_without_persist_does_not_write(patched_store):
    """默认不持久化(CLI / 测试场景)。"""
    from core.agent import Agent

    agent = Agent(session_id="s-nopersist", user_id="u1")
    agent.context.add_user_message("不该落库")
    assert patched_store.count_messages("s-nopersist") == 0


@pytest.mark.asyncio
async def test_agent_assigns_seq(patched_store):
    """落库后回填绝对序号(滚动摘要按它记账)。"""
    from core.agent import Agent

    agent = Agent(session_id="s-seq", user_id="u1", persist_session=True)
    m1 = agent.context.add_user_message("一")
    m2 = agent.context.add_assistant_message("二")
    assert m1.seq == 1
    assert m2.seq == 2


@pytest.mark.asyncio
async def test_agent_end_to_end_rollup_and_restore(patched_store, monkeypatch):
    """完整链路:落库 → 滚动摘要 → 重建 Agent 恢复摘要 + 最近消息。"""
    import core.session_rollup as rollup_mod

    monkeypatch.setattr(rollup_mod, "summarize_messages", _fake_summary_async)

    from core.agent import Agent

    agent = Agent(session_id="s-e2e", user_id="u1", persist_session=True)
    for i in range(30):
        agent.context.add_user_message(f"消息{i}")
    await agent._maybe_rollup()

    # 摘要已生成并落库
    latest = patched_store.get_latest_summary("s-e2e")
    assert latest is not None
    assert latest.cover_until_seq == 22  # 30 条留最近 8 条

    # 重建 Agent:恢复摘要 + 最近 8 条原文
    again = Agent(session_id="s-e2e", user_id="u1", persist_session=True)
    assert again.context.get_summary()
    assert len(again.context.messages) == 8
    # seq 23~30(消息内容从 0 开始编号)
    assert again.context.messages[0].content == "消息22"
    assert again.context.messages[-1].content == "消息29"
    # 序号也恢复了
    assert again.context.messages[0].seq == 23
    assert again.context.messages[-1].seq == 30

    # 原文全量保留
    assert patched_store.count_messages("s-e2e") == 30


# ===== 滚动摘要 =====

@pytest.mark.asyncio
async def test_rollup_creates_summary_and_keeps_messages(patched_store, monkeypatch):
    import core.session_rollup as rollup_mod

    monkeypatch.setattr(
        rollup_mod, "summarize_messages", _fake_summary_async
    )
    from core.context import Context

    ctx = Context()
    for i in range(30):
        msg = ctx.add_message("user", f"消息{i}")
        msg.seq = i + 1  # 模拟落库后回填的绝对序号

    summary = await rollup_mod.maybe_rollup(
        ctx,
        patched_store,
        session_id="s-roll",
        user_id="u1",
        threshold=10,
        keep_recent=5,
    )
    assert summary
    assert ctx.get_summary()
    # 30 条里摘要 25 条,保留最近 5 条原文
    assert ctx.metadata["rollup_cover_seq"] == 25
    # 摘要已落库
    latest = patched_store.get_latest_summary("s-roll")
    assert latest is not None
    assert latest.cover_until_seq == 25
    # 原文一条都没少
    assert len(ctx.messages) == 30


@pytest.mark.asyncio
async def test_rollup_noop_when_short(patched_store, monkeypatch):
    import core.session_rollup as rollup_mod
    from core.context import Context

    ctx = Context()
    for i in range(5):
        msg = ctx.add_message("user", f"消息{i}")
        msg.seq = i + 1
    result = await rollup_mod.maybe_rollup(
        ctx, patched_store, session_id="s-short", user_id="u1", threshold=10
    )
    assert result is None
    assert patched_store.get_latest_summary("s-short") is None


@pytest.mark.asyncio
async def test_rollup_incremental(patched_store, monkeypatch):
    """第二次滚动只摘要新增部分,不重复摘要已覆盖的。"""
    import core.session_rollup as rollup_mod
    from core.context import Context

    monkeypatch.setattr(
        rollup_mod, "summarize_messages", _fake_summary_async
    )
    ctx = Context()
    for i in range(30):
        msg = ctx.add_message("user", f"消息{i}")
        msg.seq = i + 1
    await rollup_mod.maybe_rollup(
        ctx, patched_store, session_id="s-inc", user_id="u1",
        threshold=10, keep_recent=5,
    )
    first_cover = ctx.metadata["rollup_cover_seq"]
    assert first_cover == 25

    for i in range(30, 45):
        msg = ctx.add_message("user", f"消息{i}")
        msg.seq = i + 1
    await rollup_mod.maybe_rollup(
        ctx, patched_store, session_id="s-inc", user_id="u1",
        threshold=10, keep_recent=5,
    )
    # 第二次只摘要 26~40,保留 41~45
    assert ctx.metadata["rollup_cover_seq"] == 40
    assert ctx.metadata["rollup_cover_seq"] > first_cover


@pytest.mark.asyncio
async def test_rollup_skips_already_covered(patched_store, monkeypatch):
    """已覆盖的消息不会被重复摘要。"""
    import core.session_rollup as rollup_mod
    from core.context import Context

    calls = []

    async def counting_summary(messages):
        calls.append(list(messages))
        return f"[摘要] {len(messages)} 条"

    monkeypatch.setattr(rollup_mod, "summarize_messages", counting_summary)

    ctx = Context()
    for i in range(30):
        msg = ctx.add_message("user", f"消息{i}")
        msg.seq = i + 1
    await rollup_mod.maybe_rollup(
        ctx, patched_store, session_id="s-skip", user_id="u1",
        threshold=10, keep_recent=5,
    )
    # 第一次摘要的是 1~25
    assert [m.seq for m in calls[0]] == list(range(1, 26))

    # 立刻再调一次:未覆盖的只有 5 条,不该再摘要
    result = await rollup_mod.maybe_rollup(
        ctx, patched_store, session_id="s-skip", user_id="u1",
        threshold=10, keep_recent=5,
    )
    assert result is None
    assert len(calls) == 1


def _fake_summary(messages):
    return f"[摘要] 覆盖 {len(messages)} 条消息"


async def _fake_summary_async(messages):
    return _fake_summary(messages)

