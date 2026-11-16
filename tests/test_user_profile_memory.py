"""用户画像(长期记忆 L2)测试 —— docs/13 §2.4 / §2.8

覆盖四段:
1. 候选提取(规则,0 次 LLM)
2. 敏感信息拒绝
3. 去重 / 取代
4. 常驻注入 + 作用域隔离
"""
import pytest

from core.memory_db import PROFILE_MEMORY_TYPES, MemoryDB
from core.memory_writer import (
    MIN_CONFIDENCE,
    MemoryWriter,
    extract_candidates,
    find_sensitive,
)
from core.semantic_memory import SemanticMemoryStore
from infra.embedding import MockEmbedding


@pytest.fixture
def db(tmp_path):
    return MemoryDB(db_path=tmp_path / "semantic_memory.db")


@pytest.fixture
def store(db):
    return SemanticMemoryStore(
        memory_db=db, embedding_service=MockEmbedding(dimension=64)
    )


@pytest.fixture
def writer(store):
    return MemoryWriter(semantic_memory=store)


# ===== 1. 候选提取 =====

def test_extract_preference():
    cands = extract_candidates("以后回答都用中文")
    assert len(cands) == 1
    assert cands[0].type == "preference"
    assert cands[0].confidence >= 0.8


def test_extract_constraint():
    cands = extract_candidates("不要用 emoji")
    assert len(cands) == 1
    assert cands[0].type == "constraint"
    assert cands[0].importance >= 0.8


def test_extract_habit():
    cands = extract_candidates("我习惯每天早上先看日报")
    assert len(cands) == 1
    assert cands[0].type == "habit"


def test_extract_method():
    cands = extract_candidates("做数据分析应该先看缺失值再画图")
    assert len(cands) == 1
    assert cands[0].type == "method"
    assert "先" in cands[0].content


def test_extract_fact():
    cands = extract_candidates("我是做数据分析的")
    assert len(cands) == 1
    assert cands[0].type == "fact"


def test_extract_explicit_remember_is_top_confidence():
    cands = extract_candidates("记住我不喜欢被追问")
    assert len(cands) == 1
    assert cands[0].confidence >= 0.95


@pytest.mark.parametrize(
    "text",
    [
        "北京天气怎么样?",
        "你好",
        "帮我查一下上海天气",
        "Python 怎么读文件",
        "这个东西好用吗",
    ],
)
def test_no_candidate_for_questions_or_requests(text):
    """提问/普通任务不该被当成画像 —— 宁缺毋滥"""
    assert extract_candidates(text) == []


def test_empty_input_safe():
    assert extract_candidates("") == []
    assert extract_candidates("   ") == []


# ===== 2. 敏感信息 =====

@pytest.mark.parametrize(
    "text,label",
    [
        ("我的手机号是 13800138000", "手机号"),
        ("身份证 110101199003075612", "身份证号"),
        ("卡号 6222020000000000000", "银行卡号"),
        ("key: sk-abcdefghijklmnopqrstuvwxyz", "API Key"),
        ("password: hunter2", "密码"),
    ],
)
def test_sensitive_detected(text, label):
    assert find_sensitive(text) == label


def test_benign_text_not_sensitive():
    assert find_sensitive("以后回答都用中文") is None


@pytest.mark.asyncio
async def test_sensitive_never_written(writer, db):
    """敏感内容**绝不入库**(宁可不记,也不泄漏)"""
    result = await writer.record_turn(
        "以后我的手机号是 13800138000 就用这个", user_id="u1"
    )
    assert result.skipped_sensitive
    assert result.added == []
    assert db.get_recent(user_id="u1", limit=10) == []


# ===== 3. 写入 / 去重 / 取代 =====

@pytest.mark.asyncio
async def test_write_preference(writer, db):
    result = await writer.record_turn("以后回答都用中文", user_id="u1")
    assert len(result.added) == 1
    entries = db.get_recent(user_id="u1", limit=10)
    assert len(entries) == 1
    assert entries[0].type == "preference"
    assert entries[0].confidence >= MIN_CONFIDENCE
    assert entries[0].status == "active"


@pytest.mark.asyncio
async def test_same_statement_is_not_duplicated(writer, db):
    await writer.record_turn("以后回答都用中文", user_id="u1")
    result = await writer.record_turn("以后回答都用中文", user_id="u1")
    assert result.added == []
    assert result.skipped_duplicate
    assert len(db.get_recent(user_id="u1", limit=10)) == 1


@pytest.mark.asyncio
async def test_changed_preference_supersedes_old(writer, db):
    """用户改主意 → 新记忆取代旧记忆,旧的保留可追溯"""
    await writer.record_turn("以后回答都用中文", user_id="u1")
    result = await writer.record_turn("以后回答都用英文", user_id="u1")
    assert len(result.superseded) == 1

    active = db.get_recent(user_id="u1", limit=10)
    assert len(active) == 1
    assert "英文" in active[0].content
    # 旧的被标记 superseded,可追溯但不参与召回
    assert active[0].supersedes_id


@pytest.mark.asyncio
async def test_memory_written_per_user(writer, db):
    await writer.record_turn("以后回答都用中文", user_id="u1")
    await writer.record_turn("以后回答都用英文", user_id="u2")
    assert len(db.get_recent(user_id="u1", limit=10)) == 1
    assert len(db.get_recent(user_id="u2", limit=10)) == 1
    assert "中文" in db.get_recent(user_id="u1", limit=10)[0].content
    assert "英文" in db.get_recent(user_id="u2", limit=10)[0].content


@pytest.mark.asyncio
async def test_writer_never_raises_on_bad_store():
    """写入失败不影响主流程"""

    class _Boom:
        @property
        def memory_db(self):
            raise RuntimeError("db down")

        embedding_service = None

    writer = MemoryWriter(semantic_memory=_Boom())
    result = await writer.record_turn("以后回答都用中文", user_id="u1")
    assert result.total == 0


# ===== 4. 常驻注入 =====

def test_profile_block_contains_entries(store, db):
    db.add(
        type="preference",
        content="回答用中文",
        user_id="u1",
        confidence=0.9,
        importance=0.9,
    )
    db.add(
        type="constraint",
        content="不要用 emoji",
        user_id="u1",
        confidence=0.9,
        importance=0.95,
    )
    block = store.build_profile_block("u1")
    assert block
    assert "用户画像" in block
    assert "回答用中文" in block
    assert "不要用 emoji" in block
    assert "[偏好]" in block
    assert "[雷区]" in block


def test_profile_block_empty_for_unknown_user(store):
    assert store.build_profile_block("nobody") == ""


def test_profile_block_respects_budget(store, db):
    for i in range(30):
        db.add(
            type="preference",
            content=f"这是一条很长的偏好描述{i}" * 3,
            user_id="u1",
            confidence=0.9,
            importance=0.9,
        )
    block = store.build_profile_block("u1", max_chars=200)
    assert len(block) <= 400  # 含标题
    assert "..." not in block  # 是截断条目数,不是截断字符串


def test_profile_block_ordered_by_confidence(store, db):
    db.add(type="preference", content="低置信偏好", user_id="u1",
           confidence=0.6, importance=0.6)
    db.add(type="preference", content="高置信偏好", user_id="u1",
           confidence=0.99, importance=0.99)
    block = store.build_profile_block("u1")
    assert block.index("高置信偏好") < block.index("低置信偏好")


def test_profile_only_profile_types(store, db):
    """会话/执行类记忆不进画像块"""
    db.add(type="failure", content="工具超时失败", user_id="u1")
    db.add(type="episode", content="查了一次北京天气", user_id="u1")
    db.add(type="preference", content="回答用中文", user_id="u1")
    block = store.build_profile_block("u1")
    assert "回答用中文" in block
    assert "工具超时失败" not in block
    assert "查了一次北京天气" not in block


# ===== 5. 作用域隔离(数据泄漏回归) =====

@pytest.mark.asyncio
async def test_search_requires_user_id():
    """`user_id` 是必填参数 —— 不传就应该报错,而不是静默泄漏"""
    store = SemanticMemoryStore(memory_db=MemoryDB.__new__(MemoryDB))
    with pytest.raises(TypeError):
        await store.search("随便什么")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_search_does_not_leak_across_users(store, db):
    db.add(type="preference", content="u1 的秘密偏好", user_id="u1",
           confidence=0.9, importance=0.9)
    results = await store.search("秘密偏好", user_id="u2", limit=5)
    assert results == []


@pytest.mark.asyncio
async def test_search_finds_own_memory(store, db):
    db.add(type="preference", content="回答用中文", user_id="u1",
           confidence=0.9, importance=0.9)
    results = await store.search("回答用中文", user_id="u1", limit=5)
    assert len(results) == 1
    assert results[0][0].content == "回答用中文"


# ===== 6. 遗忘 =====

@pytest.mark.asyncio
async def test_forget_removes_from_search(store, db):
    """「忘掉这件事」后不再被召回(索引也要清)"""
    entry = db.add(type="preference", content="回答用中文", user_id="u1",
                   confidence=0.9, importance=0.9)
    assert await store.search("回答用中文", user_id="u1", limit=5)

    assert store.forget(entry.id) is True
    assert await store.search("回答用中文", user_id="u1", limit=5) == []
    assert store.build_profile_block("u1") == ""


def test_cleanup_never_deletes_profile(db):
    """画像类记忆不会被定时清理(时间淘汰等于把人忘了)"""
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(days=400)).isoformat()
    entry = db.add(type="preference", content="回答用中文", user_id="u1")
    conn = __import__("sqlite3").connect(str(db.db_path))
    conn.execute("UPDATE memories SET created_at = ? WHERE id = ?", (old, entry.id))
    conn.commit()
    conn.close()

    db.cleanup_old(days=90)
    assert db.get(entry.id) is not None
    assert db.get(entry.id).status == "active"


# ===== 7. 主链接入 =====

@pytest.mark.asyncio
async def test_agent_schedules_memory_write(monkeypatch):
    """Agent.handle 会调度画像写入(不阻塞本轮)"""
    import asyncio

    from core import agent as agent_mod

    calls = []

    class _FakeWriter:
        async def record_turn(self, user_input, **kwargs):
            calls.append((user_input, kwargs))
            return None

    monkeypatch.setattr(
        "core.memory_writer.get_memory_writer", lambda: _FakeWriter()
    )

    agent = agent_mod.Agent(session_id="s1", user_id="u1")
    assert agent.persist_memory is True
    agent._schedule_memory_write("以后回答都用中文")
    await asyncio.sleep(0)  # 让 create_task 跑起来
    assert calls
    assert calls[0][0] == "以后回答都用中文"
    assert calls[0][1]["user_id"] == "u1"


def test_agent_skips_memory_write_when_disabled(monkeypatch):
    from core import agent as agent_mod
    from infra.config import config

    monkeypatch.setattr(config, "semantic_memory_enabled", False)
    calls = []

    class _FakeWriter:
        async def record_turn(self, user_input, **kwargs):
            calls.append(user_input)

    monkeypatch.setattr(
        "core.memory_writer.get_memory_writer", lambda: _FakeWriter()
    )
    agent = agent_mod.Agent(session_id="s1", user_id="u1")
    assert agent.persist_memory is False
    agent._schedule_memory_write("以后回答都用中文")
    assert calls == []


def test_manager_injects_profile_block(monkeypatch):
    """L2 规划提示词里带上了用户画像"""
    from agents.manager import ManagerAgent

    mgr = ManagerAgent()
    monkeypatch.setattr(
        mgr, "_build_profile_block", lambda uid: "[用户画像](长期记忆)\n- [偏好] 回答用中文"
    )
    prompt = mgr._build_user_prompt(
        "帮我查个天气", None, None, mgr._build_profile_block("u1")
    )
    assert "用户画像" in prompt
    assert "回答用中文" in prompt


def test_profile_types_constant():
    assert set(PROFILE_MEMORY_TYPES) == {
        "preference", "habit", "method", "constraint", "fact",
    }


# ===== 8. 记忆管理 API =====


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.main import app

    return TestClient(app)


def _seed(store, user_id="u1"):
    db = store.memory_db
    return db.add(
        type="preference",
        content="回答用中文",
        user_id=user_id,
        confidence=0.9,
        importance=0.9,
    )


def test_api_list_memories(client, monkeypatch, store):
    _seed(store)
    monkeypatch.setattr(
        "core.semantic_memory.get_semantic_memory", lambda: store
    )
    r = client.get("/api/memories", params={"user_id": "u1"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["memories"][0]["content"] == "回答用中文"
    assert body["memories"][0]["type"] == "preference"


def test_api_list_memories_requires_user_id(client, monkeypatch, store):
    """不给 user_id 就查全部 = 数据泄漏,必须 400"""
    monkeypatch.setattr(
        "core.semantic_memory.get_semantic_memory", lambda: store
    )
    r = client.get("/api/memories")
    assert r.status_code == 400


def test_api_list_memories_isolated_by_user(client, monkeypatch, store):
    _seed(store, "u1")
    _seed(store, "u2")
    monkeypatch.setattr(
        "core.semantic_memory.get_semantic_memory", lambda: store
    )
    r1 = client.get("/api/memories", params={"user_id": "u1"})
    r2 = client.get("/api/memories", params={"user_id": "u2"})
    assert r1.json()["count"] == 1
    assert r2.json()["count"] == 1
    assert r1.json()["memories"][0]["id"] != r2.json()["memories"][0]["id"]


def test_api_forget_memory(client, monkeypatch, store):
    entry = _seed(store)
    monkeypatch.setattr(
        "core.semantic_memory.get_semantic_memory", lambda: store
    )
    r = client.delete(f"/api/memories/{entry.id}")
    assert r.status_code == 200
    assert r.json()["forgotten"] == entry.id
    # 忘掉之后列表里就没有了
    r2 = client.get("/api/memories", params={"user_id": "u1"})
    assert r2.json()["count"] == 0


def test_api_forget_unknown_memory(client, monkeypatch, store):
    monkeypatch.setattr(
        "core.semantic_memory.get_semantic_memory", lambda: store
    )
    r = client.delete("/api/memories/not-exist")
    assert r.status_code == 404
