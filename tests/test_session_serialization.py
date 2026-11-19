"""会话串行化与重连复用（P0-01 / P0-02 回归）。

两个必须守住的性质：

1. **同一个会话永远只有一个 Agent** —— 重连拿到新 client_id，但 session_id
   不变，必须复用旧 Session；否则两个 Agent 同时写会话库、各自维护摘要基线。
2. **同一会话的轮次串行** —— `Agent.handle()` 依赖一堆可变实例状态，
   两轮交叉 = trace 串台、纠正信号挂错轮次。
3. **dispatcher 不能被一轮对话占住** —— 它是所有连接共用的一条回调。
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from backend import session as session_mod
from backend.session import Session, SessionManager


class _StubAgent:
    """只为复用/排队测试存在：不碰 DB、不碰 LLM。"""

    def __init__(self, session_id="", persist_session=False):
        self.session_id = session_id
        self.persist_session = persist_session
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1


@pytest.fixture(autouse=True)
def stub_agent(monkeypatch):
    monkeypatch.setattr(session_mod, "Agent", _StubAgent)


@pytest.mark.asyncio
async def test_reconnect_reuses_same_agent():
    """新 client_id + 旧 session_id → 同一个 Session / Agent。"""
    mgr = SessionManager()

    first = await mgr.get_or_create("client-a", session_id="s-shared")
    second = await mgr.get_or_create("client-b", session_id="s-shared")

    assert first is second
    assert first.agent is second.agent
    assert first.agent.session_id == "s-shared"


@pytest.mark.asyncio
async def test_different_sessions_stay_separate():
    mgr = SessionManager()
    a = await mgr.get_or_create("client-a", session_id="s-one")
    b = await mgr.get_or_create("client-b", session_id="s-two")
    assert a is not b
    assert a.agent is not b.agent


@pytest.mark.asyncio
async def test_jobs_run_one_at_a_time_and_in_order():
    """两轮不能交叉，且按提交顺序执行（用户连发两条消息）。"""
    s = Session(client_id="c", agent=_StubAgent(), session_id="s")
    trace: list[str] = []

    def make_job(name: str):
        async def job():
            trace.append(f"{name}:start")
            await asyncio.sleep(0.05)
            trace.append(f"{name}:end")

        return job

    assert s.submit(make_job("a")) is True
    assert s.submit(make_job("b")) is True
    await s.drain()

    assert trace == ["a:start", "a:end", "b:start", "b:end"]
    await s.close()


@pytest.mark.asyncio
async def test_failed_job_does_not_stop_queue():
    s = Session(client_id="c", agent=_StubAgent(), session_id="s")
    ran: list[str] = []

    async def boom():
        raise RuntimeError("job 挂了")

    async def ok():
        ran.append("ok")

    s.submit(boom)
    s.submit(ok)
    await s.drain()

    assert ran == ["ok"]
    await s.close()


@pytest.mark.asyncio
async def test_submit_after_close_is_rejected():
    s = Session(client_id="c", agent=_StubAgent(), session_id="s")
    await s.close()
    assert s.submit(lambda: asyncio.sleep(0)) is False


@pytest.mark.asyncio
async def test_destroy_keeps_shared_session_alive():
    """一个连接断开时，不能把另一个连接还在用的会话销毁掉。"""
    mgr = SessionManager()
    a = await mgr.get_or_create("client-a", session_id="s-shared")
    b = await mgr.get_or_create("client-b", session_id="s-shared")
    assert a is b

    await mgr.destroy("client-a")
    assert a.closed is False

    ran: list[bool] = []

    async def job():
        ran.append(True)

    assert b.submit(job) is True
    await b.drain()
    assert ran == [True]

    # 最后一个引用消失才真正销毁
    await mgr.destroy("client-b")
    assert a.closed is True


@pytest.mark.asyncio
async def test_gc_disposes_only_unreferenced_sessions():
    mgr = SessionManager(ttl_s=0)
    a = await mgr.get_or_create("client-a", session_id="s-shared")
    await mgr.get_or_create("client-b", session_id="s-shared")

    # 两条连接都过期，但共享同一个 Session → 只销毁一次、且能销毁
    await asyncio.sleep(0.01)
    await mgr.gc()
    assert a.closed is True


# ===== dispatcher 不阻塞 =====


@pytest.mark.asyncio
async def test_dispatcher_returns_without_waiting_for_turn(monkeypatch):
    """回调必须"投递即返回"：否则一次长回答会卡住所有人的 ping/init。"""
    from backend import websocket_handler as wsh
    from backend.session import sessions

    async def slow_handle(self, user_input, on_event=None):
        await asyncio.sleep(0.5)
        return ""

    async def noop_push(*args, **kwargs):
        return None

    monkeypatch.setattr(session_mod.Agent, "handle", slow_handle, raising=False)
    monkeypatch.setattr(wsh, "push_event", noop_push)

    identity = await sessions.issue_identity("conn-slow")
    try:
        sub = MagicMock()
        sub.topic = f"chat/{identity.conn_id}"

        started = time.monotonic()
        await wsh.dispatcher(sub, {"content": "hi"})
        elapsed = time.monotonic() - started

        assert elapsed < 0.2, f"dispatcher 被一轮对话阻塞了 {elapsed:.2f}s"

        # 收尾：等后台那一轮跑完，别把任务泄漏到下一个测试
        session = sessions.get(identity.client_id)
        assert session is not None
        await session.drain()
    finally:
        await sessions.destroy(identity.client_id)
