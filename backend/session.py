"""Session management keyed by a server-issued client_id.

本地单用户：没有 user_id / 令牌 / 假用户服务。client_id 仍由服务端签发，
只作为一条 websocket 连接的会话键；session_id 从客户端恢复，用于跨重启
续聊。没有跨用户隔离问题。
"""
import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from core.agent import Agent
from infra.auth import gen_client_id, gen_session_id
from infra.logger import get_logger


DisposeCallback = Callable[[], Optional[Awaitable[None]]]


@dataclass
class ClientIdentity:
    """服务端签发的连接身份（本地单用户，无令牌、无 user_id）。

    - ``client_id``: 服务端签发的会话键，只用于 topic 路由与会话复用
    - ``conn_id``: 客户端自带的 topic 句柄
    - ``session_id``: 会话 id，客户端可带回以恢复历史
    """

    session_id: str
    client_id: str
    conn_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()

    def to_payload(self) -> Dict:
        return {
            "client_id": self.client_id,
            "session_id": self.session_id,
        }


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
        # conn_id → 服务端签发的 client_id
        self._by_conn: Dict[str, str] = {}
        # client_id → ClientIdentity
        self._identities: Dict[str, ClientIdentity] = {}
        self._lock = asyncio.Lock()
        self.ttl_s = ttl_s
        self.logger = get_logger()

    # ===== 身份（本地单用户，无令牌） =====

    async def issue_identity(
        self, conn_id: str, resume_session_id: str = ""
    ) -> ClientIdentity:
        """为一条连接签发服务端身份。

        client_id 由服务端生成，只用于 topic 路由；session_id 可由客户端
        带回以恢复历史（本地单用户，无需校验归属）。
        """
        session_id = resume_session_id or gen_session_id()
        if resume_session_id:
            self.logger.info("Session", f"恢复会话: {resume_session_id}")

        identity = ClientIdentity(
            session_id=session_id,
            client_id=gen_client_id(),
            conn_id=conn_id,
        )
        async with self._lock:
            self._identities[identity.client_id] = identity
            self._by_conn[conn_id] = identity.client_id
        self.logger.info("Session", f"identity issued: {identity.client_id}")
        return identity

    def resolve_identity(self, conn_id: str) -> Optional[ClientIdentity]:
        """conn_id → 服务端身份。没有令牌校验（本地单用户）。"""
        client_id = self._by_conn.get(conn_id)
        if not client_id:
            return None
        identity = self._identities.get(client_id)
        if identity is None:
            return None
        identity.touch()
        return identity

    def get_identity(self, client_id: str) -> Optional[ClientIdentity]:
        return self._identities.get(client_id)

    # ===== 会话 =====

    async def get_or_create(
        self,
        client_id: str,
        *,
        session_id: str = "",
    ) -> Session:
        async with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                session = Session(
                    client_id=client_id,
                    agent=Agent(
                        session_id=session_id or client_id,
                        persist_session=True,
                    ),
                )
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
                identity = self._identities.pop(client_id, None)
                if identity is not None:
                    self._by_conn.pop(identity.conn_id, None)
            if session:
                await self._dispose_session(session)
                self.logger.info("Session", f"gc: {client_id[:8]}")

    async def destroy(self, client_id: str):
        session = self._sessions.pop(client_id, None)
        identity = self._identities.pop(client_id, None)
        if identity is not None:
            self._by_conn.pop(identity.conn_id, None)
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
