"""Session manager cleanup tests."""
import asyncio

import pytest

from backend.session import Session, SessionManager


@pytest.mark.asyncio
async def test_session_gc_awaits_async_dispose_callbacks():
    manager = SessionManager(ttl_s=0)
    session = Session(client_id="cid", agent=None)  # type: ignore[arg-type]
    called = {"value": False}

    async def dispose():
        called["value"] = True

    session.dispose_callbacks.append(dispose)
    manager._sessions["cid"] = session
    session.last_active = 0

    await manager.gc()
    assert called["value"] is True
    assert "cid" not in manager._sessions


# ===== P1-09: SessionManager 锁修复测试 =====


@pytest.mark.asyncio
async def test_gc_and_get_or_create_concurrent_no_race():
    """GC 与 get_or_create 并发交错时不应崩溃或数据不一致(P1-09)。

    场景:多个协程同时 get_or_create 新 session,同时 GC 在清理过期 session。
    旧版 gc 在收集和 pop 之间释放锁,可能导致:
    - get_or_create 在 gc pop 前创建了同 client_id 的新 session,被 gc 误删
    - dict 遍历时被并发修改
    """
    manager = SessionManager(ttl_s=1)
    errors: list[Exception] = []

    async def worker_create(cid: str):
        try:
            for _ in range(50):
                s = await manager.get_or_create(cid, user_id="u")
                s.touch()
                await asyncio.sleep(0)
        except Exception as e:
            errors.append(e)

    async def worker_gc():
        try:
            for _ in range(50):
                await manager.gc()
                await asyncio.sleep(0)
        except Exception as e:
            errors.append(e)

    # 用少量 client_id 制造竞争
    cids = [f"c-{i}" for i in range(5)]
    tasks = [worker_create(c) for c in cids] + [worker_gc() for _ in range(3)]
    await asyncio.gather(*tasks)

    assert not errors, f"并发错误: {errors}"
    # GC 不应误删活跃 session
    for cid in cids:
        s = manager.get(cid)
        if s is not None:
            assert s.last_active > 0


@pytest.mark.asyncio
async def test_gc_pops_in_single_lock(monkeypatch):
    """验证 gc 在单次锁内完成收集 + pop(P1-09)。

    通过计数 lock 获取次数,确保 gc 只获取一次锁(而非每个过期 session 一次)。
    """
    manager = SessionManager(ttl_s=0)
    # 放入 3 个过期 session
    for i in range(3):
        s = Session(client_id=f"cid-{i}", agent=None)  # type: ignore[arg-type]
        s.last_active = 0
        manager._sessions[f"cid-{i}"] = s

    lock_count = 0
    original_acquire = manager._lock.acquire

    async def counting_acquire():
        nonlocal lock_count
        lock_count += 1
        await original_acquire()

    monkeypatch.setattr(manager._lock, "acquire", counting_acquire)

    await manager.gc()

    # gc 应只获取一次锁(收集 + pop 在同一次锁内)
    assert lock_count == 1, f"gc 应只获取 1 次锁,实际 {lock_count}"
    assert len(manager._sessions) == 0


@pytest.mark.asyncio
async def test_destroy_uses_lock(monkeypatch):
    """验证 destroy 走 _lock(P1-09)。"""
    manager = SessionManager(ttl_s=3600)
    s = Session(client_id="cid", agent=None)  # type: ignore[arg-type]
    manager._sessions["cid"] = s

    lock_count = 0
    original_acquire = manager._lock.acquire

    async def counting_acquire():
        nonlocal lock_count
        lock_count += 1
        await original_acquire()

    monkeypatch.setattr(manager._lock, "acquire", counting_acquire)

    await manager.destroy("cid")

    assert lock_count == 1, f"destroy 应获取 1 次锁,实际 {lock_count}"
    assert "cid" not in manager._sessions


@pytest.mark.asyncio
async def test_get_does_not_touch():
    """get 不应隐式更新 last_active(P1-09)。"""
    manager = SessionManager(ttl_s=3600)
    s = Session(client_id="cid", agent=None)  # type: ignore[arg-type]
    old_active = 1000.0
    s.last_active = old_active
    manager._sessions["cid"] = s

    result = manager.get("cid")
    assert result is not None
    assert result.last_active == old_active  # 未被 touch


@pytest.mark.asyncio
async def test_gc_does_not_delete_active_session():
    """GC 不应删除仍在活跃窗口内的 session(P1-09)。"""
    import time
    manager = SessionManager(ttl_s=60)
    s = Session(client_id="cid", agent=None)  # type: ignore[arg-type]
    s.last_active = time.time()  # 当前时间,在活跃窗口内
    manager._sessions["cid"] = s

    await manager.gc()
    assert "cid" in manager._sessions
