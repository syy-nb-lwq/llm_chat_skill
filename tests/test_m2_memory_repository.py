"""M2-01 / M2-02 / M2-03 / M2-06 测试:MemoryRepository。

覆盖:
- MemoryItem 写入 / 召回(精确 FTS)
- 用户作用域隔离(user A 看不到 user B)
- 删除 + 计数
- Episode 写入与检索
"""
import json
import tempfile
from pathlib import Path

import pytest

from core.memory_repository import (
    EpisodeRecord,
    MemoryItem,
    MemoryScope,
    MemoryType,
    get_memory_repository,
    reset_memory_repository,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """每次使用隔离的 base_path。"""
    reset_memory_repository()
    # 替换默认 base
    from core import memory_repository as mr_mod
    monkeypatch.setattr(mr_mod, "_repo", None)
    return mr_mod.MemoryRepository(base_path=tmp_path)


def test_repo_singleton_isolated(monkeypatch):
    """单例测试:get_memory_repository 第二次调用返回同对象。"""
    from core.memory_repository import get_memory_repository, reset_memory_repository
    import core.memory_repository as mr_mod

    reset_memory_repository()
    a = get_memory_repository()
    b = get_memory_repository()
    assert a is b


def test_add_and_recall_by_user(repo):
    item = MemoryItem(
        user_id="alice",
        scope=MemoryScope.USER.value,
        type=MemoryType.FACT.value,
        content="alice prefers concise report format",
    )
    repo.add_memory_item(item)

    # FTS 检索英文 → 中文 unicode61 不分词时退化为 LIKE,这里用英文关键词
    import asyncio
    results = asyncio.run(repo.recall("concise", user_id="alice"))
    assert any("concise" in it.content for it in results), results


def test_user_isolation(repo):
    """user_alice 写入的事实不应被 user_bob 召回。"""
    repo.add_memory_item(MemoryItem(
        user_id="alice", scope=MemoryScope.USER.value,
        type=MemoryType.PREFERENCE.value,
        content="alice 偏好 python",
    ))
    repo.add_memory_item(MemoryItem(
        user_id="bob", scope=MemoryScope.USER.value,
        type=MemoryType.PREFERENCE.value,
        content="bob 偏好 rust",
    ))

    import asyncio
    a = asyncio.run(repo.recall("偏好", user_id="alice"))
    b = asyncio.run(repo.recall("偏好", user_id="bob"))
    a_contents = " ".join(it.content for it in a)
    b_contents = " ".join(it.content for it in b)
    assert "alice" in a_contents and "bob" not in a_contents
    assert "bob" in b_contents and "alice" not in b_contents


def test_forget_user(repo):
    repo.add_memory_item(MemoryItem(
        user_id="alice", scope=MemoryScope.USER.value,
        type=MemoryType.FACT.value, content="secret note",
    ))
    n = repo.forget_user("alice")
    assert n == 1

    import asyncio
    a = asyncio.run(repo.recall("secret", user_id="alice"))
    assert all(it.user_id != "alice" for it in a)


def test_episode_persisted_and_queryable(repo):
    ep = EpisodeRecord(
        execution_id="exec_x",
        trace_id="exec_x",
        user_id="alice",
        session_id="s",
        turn_id="t",
        scenario="weather",
        intent="skill",
        selected_skill="weather",
        selected_skill_version="1.0.0",
        success_rate=1.0,
        fallback_count=0,
        retry_count=0,
        latency_ms=120.0,
        diagnosis="ok",
    )
    repo.add_episode(ep)

    fetched = repo.get_episode("exec_x")
    assert fetched is not None
    assert fetched.scenario == "weather"

    eps = repo.list_episodes(user_id="alice")
    assert any(e.execution_id == "exec_x" for e in eps)


def test_sensitive_content_marked(repo):
    item = MemoryItem(
        user_id="alice",
        scope=MemoryScope.USER.value,
        type=MemoryType.FACT.value,
        content="my password is secret-1234",
    )
    saved = repo.add_memory_item(item)
    # 关键字命中 → sensitivity 提升到 secret
    assert saved.sensitivity == "secret"


def test_recall_mixed_scope(repo):
    """global scope 应该被任何用户召回。"""
    repo.add_memory_item(MemoryItem(
        user_id="system", scope=MemoryScope.GLOBAL.value,
        type=MemoryType.FACT.value,
        content="global weather note",
    ))
    repo.add_memory_item(MemoryItem(
        user_id="alice", scope=MemoryScope.USER.value,
        type=MemoryType.FACT.value,
        content="alice private weather note",
    ))
    import asyncio
    # alice 能看到自己的 + global
    out = asyncio.run(repo.recall("weather", user_id="alice"))
    contents = [it.content for it in out]
    assert any("alice private" in c for c in contents)
    assert any("global weather" in c for c in contents)
