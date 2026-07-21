"""M2-09 e2e:重启恢复 + 用户隔离 + 删除。

依据 docs/11-开发任务清单.md M2-09:
- 服务重启后,MemoryRepository 仍能召回用户偏好
- 另一用户无法召回该偏好
- 删除后不再召回
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_repo_singleton(monkeypatch):
    """每个测试前重置全局 MemoryRepository 单例,避免跨用例污染。"""
    from core import memory_repository as mr_mod
    monkeypatch.setattr(mr_mod, "_repo", None)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """隔离运行时:返回独立的 MemoryRepository(不污染单例)。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from core import memory_repository as mr_mod

    # 直接构造非单例实例
    repo = mr_mod.MemoryRepository(base_path=tmp_path)
    yield {"tmp": tmp_path, "repo": repo}


@pytest.mark.asyncio
async def test_user_pref_persists_across_reload(runtime):
    """用户写入偏好 → 重新构造 Repository → 仍能召回。"""
    repo = runtime["repo"]
    tmp = runtime["tmp"]
    from core.memory_repository import (
        MemoryItem, MemoryScope, MemoryType,
    )

    # 第一次会话写入
    repo.add_memory_item(MemoryItem(
        user_id="alice",
        scope=MemoryScope.USER.value,
        type=MemoryType.PREFERENCE.value,
        content="alice prefers concise report format",
        tags=["work"],
    ))

    # 模拟服务重启:重新构造 Repo(同 base_path)
    from core import memory_repository as mr_mod
    repo2 = mr_mod.MemoryRepository(base_path=tmp)
    items = await repo2.recall("concise", user_id="alice")
    contents = " ".join(it.content for it in items)
    assert "concise" in contents, items


@pytest.mark.asyncio
async def test_user_isolation(runtime):
    """alice 写入的事实不应被 bob 召回。"""
    repo = runtime["repo"]
    from core.memory_repository import (
        MemoryItem, MemoryScope, MemoryType,
    )

    # 用空白分隔的标记以便 FTS5 unicode61 能正确切词
    repo.add_memory_item(MemoryItem(
        user_id="alice", scope=MemoryScope.USER.value,
        type=MemoryType.PREFERENCE.value,
        content="alice private note MARKERALPHA",
    ))
    repo.add_memory_item(MemoryItem(
        user_id="bob", scope=MemoryScope.USER.value,
        type=MemoryType.PREFERENCE.value,
        content="bob private note MARKERBRAVO",
    ))

    alice_items = await repo.recall("MARKERALPHA", user_id="alice")
    bob_items = await repo.recall("MARKERBRAVO", user_id="bob")

    assert any("MARKERALPHA" in it.content for it in alice_items)
    assert not any("MARKERBRAVO" in it.content for it in alice_items)
    assert any("MARKERBRAVO" in it.content for it in bob_items)
    assert not any("MARKERALPHA" in it.content for it in bob_items)


@pytest.mark.asyncio
async def test_delete_memory_not_recalled(runtime):
    repo = runtime["repo"]
    from core.memory_repository import (
        MemoryItem, MemoryScope, MemoryType,
    )

    item = MemoryItem(
        user_id="alice", scope=MemoryScope.USER.value,
        type=MemoryType.PREFERENCE.value,
        content="forgettable unique-marker-cccc",
    )
    saved = repo.add_memory_item(item)
    ok = repo.delete_memory(saved.id, user_id="alice")
    assert ok
    items = await repo.recall("unique-marker-cccc", user_id="alice")
    assert not any("cccc" in it.content for it in items)


@pytest.mark.asyncio
async def test_forget_user_purges_all(runtime):
    repo = runtime["repo"]
    from core.memory_repository import (
        MemoryItem, MemoryScope, MemoryType,
    )

    for note in ("alpha-1", "beta-2", "gamma-3"):
        repo.add_memory_item(MemoryItem(
            user_id="alice", scope=MemoryScope.USER.value,
            type=MemoryType.FACT.value,
            content=note,
        ))

    n = repo.forget_user("alice")
    assert n == 3

    for note in ("alpha-1", "beta-2", "gamma-3"):
        items = await repo.recall(note, user_id="alice")
        assert not any(note in it.content for it in items)
