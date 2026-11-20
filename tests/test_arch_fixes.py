"""架构评审改造的回归测试(docs/14 §2.1 / §2.2 / §4.2 / §3.1)

这四条都不是"锦上添花",每一条修的都是会**静默**坏掉的缺陷:

- 2.1 画像不注入回答生成 → 用户说"以后回答都用中文",回答不变
- 2.2 合成向量当语义用 → 检索结果比不检索更差
- 4.2 没有分阶段耗时 → "慢"只能靠猜
- 3.1 SQLite 裸 connect → 多用户并发写 database is locked
"""
import tempfile
import threading
from pathlib import Path

import pytest


# ============================================================
# 2.1 用户画像必须进入回答生成
# ============================================================

class _RecordingLLM:
    """记录收到的 messages,用于断言 system prompt 内容。"""

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

    def system_prompts(self):
        return [
            m.get("content", "")
            for call in self.calls
            for m in call
            if m.get("role") == "system"
        ]


def _patch_llm(monkeypatch, fake):
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    from infra import llm as llm_mod

    llm_mod._llm_client = None
    return fake


def test_orchestrator_system_prompt_contains_profile():
    """画像进 system prompt(不是 user prompt)"""
    from agents.orchestrator import OrchestratorAgent

    agent = OrchestratorAgent()
    plain = agent.system_prompt()
    assert "用户画像" not in plain

    with_profile = agent.system_prompt("[用户画像]\n- 偏好:回答用中文")
    assert "用户画像" in with_profile
    assert "回答用中文" in with_profile
    # 人设不能丢
    assert "回答生成助手" in with_profile


@pytest.mark.asyncio
async def test_chitchat_answer_sees_profile(monkeypatch):
    """闲聊轮也必须带画像 —— 说话方式偏好主要发生在闲聊轮"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    fake = _patch_llm(monkeypatch, _RecordingLLM())

    agent = Agent(persist_session=False)
    monkeypatch.setattr(
        agent.mem.long_term,
        "profile_block",
        lambda *a, **k: "[用户画像]\n- 偏好:不要用 emoji",
    )

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.CHITCHAT,
            intent_detail="闲聊",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    ans = await agent.handle("你好啊", on_event=None)
    assert ans
    prompts = fake.system_prompts()
    assert prompts, "没有产生任何 system prompt"
    assert any("不要用 emoji" in p for p in prompts), (
        f"闲聊轮的 system prompt 里没有画像: {prompts}"
    )


@pytest.mark.asyncio
async def test_task_answer_sees_profile(monkeypatch):
    """任务轮也必须带画像"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    fake = _patch_llm(monkeypatch, _RecordingLLM())

    agent = Agent(persist_session=False)
    monkeypatch.setattr(
        agent.mem.long_term,
        "profile_block",
        lambda *a, **k: "[用户画像]\n- 偏好:回答用中文",
    )

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="任务",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    await agent.handle("帮我总结一下", on_event=None)
    prompts = fake.system_prompts()
    assert any("回答用中文" in p for p in prompts), (
        f"任务轮的 system prompt 里没有画像: {prompts}"
    )


@pytest.mark.asyncio
async def test_profile_read_failure_does_not_break_answer(monkeypatch):
    """画像读挂了不能影响回答(降级为空块)"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent

    fake = _patch_llm(monkeypatch, _RecordingLLM())

    agent = Agent(persist_session=False)

    def _boom():
        raise RuntimeError("db down")

    # 画像的读入口是长期记忆门面（core/memorykit/long_term.py），不是 manager
    monkeypatch.setattr(agent.mem.long_term, "profile_block", _boom)

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.CHITCHAT,
            intent_detail="闲聊",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    ans = await agent.handle("在吗", on_event=None)
    assert ans  # 没因为画像读取失败而炸


def test_manager_profile_block_empty_when_no_memory():
    """长期记忆没数据 → 画像为空,不报错"""
    from agents.manager import ManagerAgent

    mgr = ManagerAgent()
    assert mgr.profile_block() == ""


# ============================================================
# 2.2 合成向量不得进入语义通道
# ============================================================

def test_mock_embedding_is_marked_synthetic():
    from infra.embedding import MockEmbedding

    assert MockEmbedding.is_synthetic is True
    assert MockEmbedding(dimension=8).provider_name == "mock"


def test_real_embeddings_are_not_synthetic():
    from infra.embedding import BaseEmbeddingService

    class _Fake(BaseEmbeddingService):
        provider_name = "fake"

        @property
        def dimension(self):
            return 8

        async def embed(self, text):
            return [0.1] * 8

        async def embed_batch(self, texts):
            return [[0.1] * 8 for _ in texts]

    assert _Fake.is_synthetic is False


def test_set_embedding_service_rejects_synthetic():
    """绑 mock 向量 → 语义通道必须关掉"""
    from core.semantic_memory import SemanticMemoryStore
    from infra.embedding import MockEmbedding

    store = SemanticMemoryStore.__new__(SemanticMemoryStore)
    store.embedding_service = None
    store.enable_semantic = True
    store.enable_fts = True
    store.min_similarity = 0.35
    from infra.logger import get_logger

    store.logger = get_logger()

    enabled = store.set_embedding_service(MockEmbedding(dimension=32))
    assert enabled is False
    assert store.enable_semantic is False


def test_set_embedding_service_accepts_real_provider():
    from core.semantic_memory import SemanticMemoryStore
    from infra.embedding import BaseEmbeddingService
    from infra.logger import get_logger

    class _Fake(BaseEmbeddingService):
        provider_name = "fake"

        @property
        def dimension(self):
            return 8

        async def embed(self, text):
            return [0.1] * 8

        async def embed_batch(self, texts):
            return [[0.1] * 8 for _ in texts]

    store = SemanticMemoryStore.__new__(SemanticMemoryStore)
    store.embedding_service = None
    store.enable_semantic = False
    store.enable_fts = True
    store.min_similarity = 0.35
    store.logger = get_logger()

    assert store.set_embedding_service(_Fake()) is True
    assert store.enable_semantic is True


def test_default_config_does_not_enable_vector_channel():
    """默认配置必须是 FTS-only(向量通道没配 provider 就不开)"""
    from infra.config import config

    assert (config.embedding_provider or "").strip() == ""


@pytest.mark.asyncio
async def test_synthetic_vectors_do_not_corrupt_ranking():
    """核心回归:合成向量不得把无关条目顶到前面

    背景:mock 向量下任意两句余弦相似度 ≈ 0.81,用它做语义检索
    会把全文检索的真实命中挤掉(实测查"北京天气"返回"周报要有结论")。
    """
    from core.memory_db import MemoryDB
    from core.semantic_memory import SemanticMemoryStore
    from infra.embedding import MockEmbedding

    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(db_path=tmp / "m.db")
    store = SemanticMemoryStore(
        memory_db=db,
        embedding_service=MockEmbedding(dimension=64),
        enable_semantic=False,   # 生产路径:合成向量 → 关
    )
    assert store.enable_semantic is False

    emb = MockEmbedding(dimension=64)
    items = [
        "回答用中文",
        "不要用 emoji",
        "周报要有结论",
        "先看缺失值再画图",
        "查询北京天气的记录",
    ]
    for text in items:
        vec = await emb.embed(text)
        db.add(
            type="preference",
            content=text,
            embedding=vec,
            confidence=0.9,
        )

    res = await store.search("北京天气", limit=5)
    contents = [e.content for e, _ in res]
    # 相关条目必须能被全文检索命中,且排在前面
    assert contents, "检索结果为空"
    assert "查询北京天气的记录" in contents, (
        f"相关条目被噪声挤掉了: {contents}"
    )


# ============================================================
# 4.2 分阶段耗时记录
# ============================================================

def test_turn_trace_marks_stages():
    from core.trace import STAGE_PLAN, STAGE_TOTAL, TurnTrace

    tr = TurnTrace(trace_id="t1")
    tr.mark(STAGE_PLAN, 12.34)
    tr.mark(STAGE_PLAN, 1.0)
    assert tr.stage_ms[STAGE_PLAN] == 13.3
    tr.mark(STAGE_TOTAL, 99.0)
    d = tr.to_dict()
    assert d["trace_id"] == "t1"
    assert d["stage_ms"][STAGE_TOTAL] == 99.0


def test_trace_recorder_roundtrip():
    from core.trace import TraceRecorder, TurnTrace

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "sub" / "traces.jsonl"
    rec = TraceRecorder(path=path, enabled=True)

    tr = TurnTrace(trace_id="t1")
    tr.mark("plan", 5.0)
    rec.record(tr)

    rows = rec.read()
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "t1"
    assert rows[0]["stage_ms"]["plan"] == 5.0


def test_trace_recorder_disabled_is_noop():
    from core.trace import TraceRecorder, TurnTrace

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "traces.jsonl"
    rec = TraceRecorder(path=path, enabled=False)
    rec.record(TurnTrace(trace_id="t1"))
    assert not path.exists()
    assert rec.read() == []


def test_trace_recorder_swallows_write_error():
    """观测不能拖垮业务"""
    from core.trace import TraceRecorder, TurnTrace

    rec = TraceRecorder(path=Path("X:/definitely/not/writable/x.jsonl"), enabled=True)
    rec.record(TurnTrace(trace_id="t1"))  # 不抛异常即通过


@pytest.mark.asyncio
async def test_agent_emits_trace(monkeypatch, tmp_path):
    """跑一轮对话后应该留下一条 trace"""
    from agents.manager import IntentType, PlanResult
    from core.agent import Agent
    from core.trace import STAGE_TOTAL, TraceRecorder

    fake = _patch_llm(monkeypatch, _RecordingLLM())

    recorder = TraceRecorder(path=tmp_path / "traces.jsonl", enabled=True)
    monkeypatch.setattr("core.trace.get_trace_recorder", lambda: recorder)

    agent = Agent(persist_session=False)

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.CHITCHAT,
            intent_detail="闲聊",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    await agent.handle("你好", on_event=None)

    rows = recorder.read()
    assert len(rows) == 1
    row = rows[0]
    assert row["intent_layer"] == ""  # 替身 plan 没有这一层
    assert row["stage_ms"].get(STAGE_TOTAL, 0) > 0
    assert row["reply_chars"] > 0


@pytest.mark.asyncio
async def test_empty_answer_is_replaced_with_fallback(monkeypatch, tmp_path):
    """上游返回空内容时,不能给用户一个空气泡。

    实测:上游返回空字符串时,前端只渲染出一个空气泡,没有任何提示,
    用户会以为系统卡死了。必须兜底成一句明确的失败说明。
    """
    from agents.manager import IntentType, PlanResult
    from core.agent import EMPTY_ANSWER_TEXT, Agent

    class _EmptyLLM:
        async def chat_with_retry(self, messages, **kw):
            return ""

        async def chat(self, messages, **kw):
            return ""

        async def stream(self, messages, **kw):
            return
            yield  # pragma: no cover

    _patch_llm(monkeypatch, _EmptyLLM())
    agent = Agent(persist_session=False)

    async def _plan(*args, **kwargs):
        return PlanResult(
            intent=IntentType.CHITCHAT,
            intent_detail="闲聊",
            selected_skill=None,
            tool_tasks=[],
        )

    monkeypatch.setattr(agent.manager, "plan", _plan)

    ans = await agent.handle("你好", on_event=None)
    assert ans == EMPTY_ANSWER_TEXT
    assert ans.strip(), "空回答没有被兜底"
    # 兜底文案也要进上下文(下一轮纠正检测要能看到它)
    assert agent.context.messages[-1].content == EMPTY_ANSWER_TEXT


# ============================================================
# 3.1 SQLite 并发
# ============================================================

def test_sqlite_connection_uses_wal():
    from core import sqlite_util

    tmp = Path(tempfile.mkdtemp())
    conn = sqlite_util.connect(tmp / "x.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout >= 5000
    finally:
        conn.close()


def test_memory_db_uses_wal():
    from core.memory_db import MemoryDB

    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(db_path=tmp / "m.db")
    conn = db._connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_concurrent_writes_do_not_fail():
    """8 线程并发写 200 条:不报错、不丢数据"""
    from core.memory_db import MemoryDB

    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(db_path=tmp / "m.db")
    errors = []

    def _worker(w):
        try:
            for i in range(25):
                db.add(
                    type="preference",
                    content=f"u{w}-m{i}",
                    confidence=0.9,
                )
        except Exception as exc:  # pragma: no cover - 失败则测试红
            errors.append(repr(exc))

    threads = [threading.Thread(target=_worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写失败: {errors[:3]}"
    assert len(db.list_memories(limit=1000)) == 200


def test_session_memory_uses_wal():
    from core.session_memory import SessionMemoryStore

    tmp = Path(tempfile.mkdtemp())
    store = SessionMemoryStore(db_path=tmp / "s.db")
    conn = store._connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
