"""Session management keyed by a server-issued client_id.

本地单用户：没有 user_id / 令牌 / 假用户服务。client_id 仍由服务端签发，
只作为一条 websocket 连接的会话键；session_id 从客户端恢复，用于跨重启
续聊。没有跨用户隔离问题。

## 为什么一个 Session 可能被多个 client_id 共用

重连是常态（刷新页面、切网络、心跳超时），每次重连都会拿到**新的**
client_id，但 session_id 不变。如果按 client_id 直接新建 Session + Agent，
同一个会话就会有两个 Agent 同时写 `sessions.db`、各自维护摘要基线 ——
trace 串台、纠正信号挂错轮次都从这里来。

所以：`session_id` 相同的连接共用**同一个 Session 对象**（也就同一个 Agent），
写入通过该 Session 的单条队列串行化。
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
#: 投进会话队列的工作单元（必须是"自己做完全部事情"的协程函数）
Job = Callable[[], Awaitable[None]]

logger = get_logger()


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
    """Per-session state（可能被多条连接共享，见模块 docstring）。"""

    client_id: str
    agent: Agent
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    dispose_callbacks: List[DisposeCallback] = field(default_factory=list)
    #: 会话级串行闸门。队列之外的外部入口（脚本、测试）也可以用它。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # --- 单条工作队列 ---
    #
    # 一轮对话必须整段独占 Agent：`handle()` 依赖 identity / execution_id /
    # context / 画像缓存等一堆可变实例状态。两轮交叉 = trace 串台。
    #
    # 放在 Session 上而不是 dispatcher 里，是因为 dispatcher 是所有连接
    # 共用的**一条**订阅回调：在它里面 await 一整轮，会把其他人的
    # ping/init 一起卡住（60s 的回答期间谁都动不了）。
    _queue: asyncio.Queue = field(default_factory=asyncio.Queue, repr=False)
    _worker: Optional[asyncio.Task] = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    def touch(self):
        self.last_active = time.time()

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(self, job: Job) -> bool:
        """把一轮工作排进队列，立即返回（不阻塞调用方）。

        返回 False 表示会话已关闭（TTL 回收后又被引用）。
        """
        if self._closed:
            return False
        self._queue.put_nowait(job)
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain(),
                name=f"session-worker:{self.session_id or self.client_id}",
            )

    async def _drain(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 单个 job 失败不能让整条队列停摆：后面的用户消息还要处理
                logger.error("Session", f"job failed in {self.session_id}: {exc}")
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """等队列跑空（测试与优雅关闭用）。"""
        await self._queue.join()

    def pending(self) -> int:
        return self._queue.qsize()

    async def close(self) -> None:
        """停掉 worker。未开始的任务直接丢弃（会话已过期）。"""
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


class SessionManager:
    """In-memory session pool."""

    def __init__(self, ttl_s: int = 3600):
        self._sessions: Dict[str, Session] = {}
        # session_id → Session（重连时按它复用同一个 Agent）
        self._by_session: Dict[str, Session] = {}
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
        """取这条连接对应的 Session；同 session_id 的连接复用同一个 Agent。

        **重连不能新建 Agent**：新 client_id + 旧 session_id 时，旧 Agent 可能
        还在跑上一轮，两个 Agent 会同时往 `sessions.db` 写、各自维护摘要基线。
        """
        async with self._lock:
            session = self._sessions.get(client_id)
            if session is not None:
                session.touch()
                return session

            target_session_id = session_id or client_id
            shared = self._by_session.get(target_session_id)
            if shared is not None and not shared.closed:
                self._sessions[client_id] = shared
                shared.touch()
                self.logger.info(
                    "Session",
                    f"reused: {target_session_id[:8]} by {client_id[:8]}",
                )
                return shared

            session = Session(
                client_id=client_id,
                session_id=target_session_id,
                agent=Agent(
                    session_id=target_session_id,
                    persist_session=True,
                ),
            )
            self._sessions[client_id] = session
            self._by_session[target_session_id] = session
            self.logger.info("Session", f"created: {client_id[:8]}")
            session.touch()
            return session

    def get(self, client_id: str) -> Optional[Session]:
        session = self._sessions.get(client_id)
        if session:
            session.touch()
        return session

    def _referenced(self, session: Session) -> bool:
        """这条 Session 是否还被别的 client_id 引用（调用方需持锁）。"""
        return any(s is session for s in self._sessions.values())

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
            to_dispose: List[Session] = []
            for client_id in expired:
                session = self._sessions.pop(client_id, None)
                identity = self._identities.pop(client_id, None)
                if identity is not None:
                    self._by_conn.pop(identity.conn_id, None)
                if session is None:
                    continue
                # 会话可能被多条连接共享：还有人引用就不销毁
                if self._referenced(session):
                    continue
                self._by_session.pop(session.session_id, None)
                if session not in to_dispose:
                    to_dispose.append(session)

        for session in to_dispose:
            await self._dispose_session(session)
            self.logger.info("Session", f"gc: {session.client_id[:8]}")

    async def destroy(self, client_id: str):
        async with self._lock:
            session = self._sessions.pop(client_id, None)
            identity = self._identities.pop(client_id, None)
            if identity is not None:
                self._by_conn.pop(identity.conn_id, None)
            if session is None:
                return
            if self._referenced(session):
                # 别的连接还在用这个会话 —— 只摘掉当前映射
                return
            self._by_session.pop(session.session_id, None)
        await self._dispose_session(session)

    async def _dispose_session(self, session: Session):
        await session.close()
        for callback in session.dispose_callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive cleanup
                self.logger.warning("Session", f"dispose callback failed: {exc}")


sessions = SessionManager()
