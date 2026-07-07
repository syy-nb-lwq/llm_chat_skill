"""Session 管理 - 按 client_id 持久化 Agent 实例"""
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from core.agent import Agent
from infra.logger import get_logger, LogEntry


@dataclass
class Session:
    """一个客户端的会话状态"""
    client_id: str
    agent: Agent
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    # 每个 session 独立的日志订阅者,断线后清理
    log_callbacks: list = field(default_factory=list)

    def touch(self):
        self.last_active = time.time()


class SessionManager:
    """Session 池,按 client_id 维护"""

    def __init__(self, ttl_s: int = 3600):
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.ttl_s = ttl_s
        self.logger = get_logger()

    async def get_or_create(self, client_id: str) -> Session:
        async with self._lock:
            sess = self._sessions.get(client_id)
            if sess is None:
                sess = Session(client_id=client_id, agent=Agent(session_id=client_id))
                self._sessions[client_id] = sess
                self.logger.info("flow_step", "SessionManager",
                                 f"创建 session: {client_id}")
            sess.touch()
            return sess

    def get(self, client_id: str) -> Optional[Session]:
        sess = self._sessions.get(client_id)
        if sess:
            sess.touch()
        return sess

    def gc(self):
        """清理过期 session"""
        now = time.time()
        expired = [cid for cid, s in self._sessions.items()
                   if now - s.last_active > self.ttl_s]
        for cid in expired:
            s = self._sessions.pop(cid, None)
            if s:
                for cb in s.log_callbacks:
                    try:
                        self.logger.unsubscribe(cb)
                    except Exception:
                        pass
                self.logger.info("flow_step", "SessionManager",
                                 f"GC session: {cid}")

    def destroy(self, client_id: str):
        s = self._sessions.pop(client_id, None)
        if s:
            for cb in s.log_callbacks:
                try:
                    self.logger.unsubscribe(cb)
                except Exception:
                    pass


# 全局单例
sessions = SessionManager()