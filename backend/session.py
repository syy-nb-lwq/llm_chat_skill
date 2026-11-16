"""Session management keyed by a server-issued client_id (C-01)."""
import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from core.agent import Agent
from infra.auth import (
    gen_client_id,
    gen_session_id,
    issue_session_token,
    verify_session_token,
)
from infra.logger import get_logger


DisposeCallback = Callable[[], Optional[Awaitable[None]]]


@dataclass
class ClientIdentity:
    """服务端签发的客户端身份(C-01)。

    - ``client_id``: 服务端签发的会话键,不再信任客户端提交的 id
    - ``conn_id``: 客户端自带的 topic 句柄,只用于消息路由
    - ``token``: 绑定三者的签名令牌,后续请求必须携带
    - ``user_token``: 长期用户凭证(假用户服务签发),客户端持久化后跨连接复用
    """

    user_id: str
    session_id: str
    client_id: str
    conn_id: str
    token: str
    user_token: str = ""
    tenant_id: str = "default"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()

    def to_payload(self) -> Dict:
        return {
            "client_id": self.client_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "token": self.token,
            "user_token": self.user_token,
            "tenant_id": self.tenant_id,
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

    # ===== 身份(C-01) =====

    async def issue_identity(
        self, conn_id: str, user_token: str = "", resume_session_id: str = ""
    ) -> ClientIdentity:
        """为一条连接签发服务端身份。

        客户端自带的 conn_id 只用于 topic 路由;身份(user_id / session_id /
        client_id)全部由服务端生成。

        user_token 是"假用户服务"签发的长期凭证:带了它就认出同一个用户
        (长期记忆因此能跨重启/跨连接连续);没带或无效则新建一个用户。

        resume_session_id:客户端希望恢复的会话。**必须属于该用户**才复用,
        否则一律新建 —— 防止越权读取他人会话内容。
        """
        from core.user_service import get_user_service

        service = get_user_service()
        user = service.get_or_create(user_token)
        user_token = service.issue_token(user.id, user.tenant_id)

        session_id = gen_session_id()
        if resume_session_id:
            if self._session_belongs_to(resume_session_id, user.id):
                session_id = resume_session_id
                self.logger.info(
                    "Session", f"恢复会话: {resume_session_id} (user={user.id})"
                )
            else:
                self.logger.info(
                    "Session",
                    f"会话 {resume_session_id} 不属于该用户,新建会话",
                )

        identity = ClientIdentity(
            user_id=user.id,
            session_id=session_id,
            client_id=gen_client_id(),
            conn_id=conn_id,
            token="",
            user_token=user_token,
            tenant_id=user.tenant_id,
        )
        identity.token = issue_session_token(
            identity.user_id, identity.session_id, identity.client_id
        )
        async with self._lock:
            self._identities[identity.client_id] = identity
            self._by_conn[conn_id] = identity.client_id
        self.logger.info(
            "Session", f"identity issued: {identity.client_id} (user={identity.user_id})"
        )
        return identity

    @staticmethod
    def _session_belongs_to(session_id: str, user_id: str) -> bool:
        """会话是否存在且属于该用户。"""
        try:
            from core.session_memory import get_session_memory_store

            return get_session_memory_store().session_exists(session_id, user_id)
        except Exception:
            return False

    def resolve_identity(
        self, conn_id: str, token: str = ""
    ) -> Optional[ClientIdentity]:
        """conn_id → 服务端身份。

        令牌是必需的:必须校验通过、且其中的 client_id 与绑定一致。
        缺失或伪造的令牌一律拒绝(conn_id 只是路由句柄,不是凭证)。
        """
        client_id = self._by_conn.get(conn_id)
        if not client_id:
            return None
        identity = self._identities.get(client_id)
        if identity is None:
            return None
        payload = verify_session_token(token)
        if not payload or payload.get("client_id") != identity.client_id:
            self.logger.warning("Session", f"token 校验失败: {conn_id[:8]}")
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
        user_id: str = "default",
        session_id: str = "",
    ) -> Session:
        async with self._lock:
            session = self._sessions.get(client_id)
            if session is None:
                session = Session(
                    client_id=client_id,
                    agent=Agent(
                        session_id=session_id or client_id,
                        user_id=user_id,
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
