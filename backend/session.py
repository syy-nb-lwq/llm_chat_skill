"""Session management keyed by client_id."""
import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional

from core.agent import Agent
from infra.logger import get_logger

DisposeCallback = Callable[[], Optional[Awaitable[None]]]


@dataclass
class Session:
    """Per-client session state."""

    client_id: str
    agent: Agent
    user_id: str = "default"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    dispose_callbacks: list[DisposeCallback] = field(default_factory=list)

    def touch(self):
        self.last_active = time.time()


class SessionManager:
    """In-memory session pool."""

    def __init__(self, ttl_s: int = 3600):
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.ttl_s = ttl_s
        self.logger = get_logger()

    async def get_or_create(self, client_id: str, user_id: str = "default") -> Session:
        async with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                agent = Agent(session_id=client_id, user_id=user_id)
                session = Session(
                    client_id=client_id,
                    user_id=user_id,
                    agent=agent,
                )
                self._sessions[client_id] = session
                self.logger.info("Session", f"created: client={client_id[:8]} user={user_id[:8]}")
            session.touch()
            return session

    def get(self, client_id: str) -> Optional[Session]:
        """读取 session(不更新 last_active,P1-09)。

        如需刷新活跃时间,调用方应显式调用 ``session.touch()``。
        旧版 get 会在读取时隐式 touch,导致 GC 与 get 交错时活跃时间被意外刷新。
        """
        return self._sessions.get(client_id)

    async def gc(self):
        """Remove expired sessions and run disposal callbacks.

        P1-09: 在单次锁内完成收集 + pop,避免与 get_or_create 并发竞争。
        dispose 在锁外执行(可能耗时,不阻塞其他 session 操作)。
        """
        now = time.time()
        async with self._lock:
            expired_ids = [
                cid
                for cid, s in self._sessions.items()
                if now - s.last_active > self.ttl_s
            ]
            expired_sessions = [
                self._sessions.pop(cid, None) for cid in expired_ids
            ]
        for cid, session in zip(expired_ids, expired_sessions):
            if session:
                await self._dispose_session(session)
                self.logger.info("Session", f"gc: {cid[:8]}")

    async def destroy(self, client_id: str):
        """销毁指定 session(P1-09: 走 _lock 避免与 gc/ get_or_create 竞争)。"""
        async with self._lock:
            session = self._sessions.pop(client_id, None)
        if session:
            await self._dispose_session(session)

    async def _dispose_session(self, session: Session):
        for callback in session.dispose_callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive cleanup
                self.logger.warning("Session", f"dispose callback failed: {exc}")


sessions = SessionManager()
