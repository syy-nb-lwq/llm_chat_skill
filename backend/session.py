"""Session management keyed by client_id."""
import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from core.agent import Agent
from infra.logger import get_logger


DisposeCallback = Callable[[], Optional[Awaitable[None]]]


@dataclass
class Session:
    """Per-client session state."""

    client_id: str
    agent: Agent
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    dispose_callbacks: List[DisposeCallback] = field(default_factory=list)

    def touch(self):
        self.last_active = time.time()


class SessionManager:
    """In-memory session pool."""

    def __init__(self, ttl_s: int = 3600):
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.ttl_s = ttl_s
        self.logger = get_logger()

    async def get_or_create(self, client_id: str) -> Session:
        async with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                session = Session(client_id=client_id, agent=Agent(session_id=client_id))
                self._sessions[client_id] = session
                self.logger.info("Session", f"created: {client_id[:8]}")
            session.touch()
            return session

    def get(self, client_id: str) -> Optional[Session]:
        session = self._sessions.get(client_id)
        if session:
            session.touch()
        return session

    async def gc(self):
        """Remove expired sessions and run disposal callbacks."""
        now = time.time()
        # 在锁内收集过期 client_id,避免遍历时 get_or_create 并发修改
        async with self._lock:
            expired = [
                client_id
                for client_id, session in self._sessions.items()
                if now - session.last_active > self.ttl_s
            ]

        for client_id in expired:
            async with self._lock:
                session = self._sessions.pop(client_id, None)
            if session:
                await self._dispose_session(session)
                self.logger.info("Session", f"gc: {client_id[:8]}")

    async def destroy(self, client_id: str):
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
