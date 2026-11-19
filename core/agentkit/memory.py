"""记忆门面 —— 四层写入权限 + 按作用域读取。

## 为什么要门面，而不是直接调 MemoryDB

之前的现实（docs/18 §2 P0-3）：

- 写路径**恒为 `user`** —— 子智能体没有"自己的记忆"可写；
- 三条检索 SQL **一律不过滤 scope** —— scope 是个死字段；
- 没有"提议"机制 —— 要么不写，要么直接污染用户画像。

本模块把写入权限显式分四层：

| 层 | 主智能体 | 子智能体 | 存哪 |
|---|---|---|---|
| 会话记忆 | 读写 | 读写（自己的） | `memory/sessions.db`（本模块只给接口） |
| 智能体私有记忆 | 读写 | 读写（**只能写自己的**） | `memories` 表，scope=`agent` |
| 经验候选 | **合并/裁决** | **只能提议** | `memory_proposals` 表 |
| 用户长期记忆 | 读写 | **只读** | `memories` 表，scope=`user` |

**为什么子智能体不能直接写用户长期记忆**：并发跑的时候同时改用户画像，
互相覆盖（docs/15 §6 的判断是对的）。但"堵"不是"给路" ——
所以给了**私有层**与**候选层**：子智能体有地方写，主智能体有地方审。

## 权限判定

靠 `core/exec_context` 里的当前 agent 身份判定，**不靠提示词**：

- `agent_id` 为空或 `main` → 主智能体；
- 其他 → 子智能体（只能写自己的私有记忆、只能提候选）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from infra.logger import get_logger

#: 记忆作用域
SCOPE_AGENT = "agent"
SCOPE_SESSION = "session"
SCOPE_USER = "user"
SCOPE_GLOBAL = "global"

#: 主智能体的 agent_id（也是"没有 agent 上下文"时的兜底）
MAIN_AGENT_IDS = ("", "main")


class MemoryPermissionError(PermissionError):
    """越权写记忆。"""


class MemoryFacade:
    """统一记忆读写入口（带作用域与权限）。"""

    def __init__(self, db: Any = None):
        self.logger = get_logger()
        self._db = db

    # ===== 底层 =====

    @property
    def db(self) -> Any:
        if self._db is None:
            from core.memory_db import MemoryDB

            self._db = MemoryDB()
        return self._db

    def _current_agent(self) -> str:
        try:
            from core.exec_context import get_exec_context

            return get_exec_context().agent_id or ""
        except Exception:
            return ""

    def _is_main(self) -> bool:
        return self._current_agent() in MAIN_AGENT_IDS

    def _require_main(self, what: str) -> None:
        if not self._is_main():
            raise MemoryPermissionError(
                f"{what} 只允许主智能体做"
                f"（当前 agent_id={self._current_agent() or 'unknown'}）"
            )

    # ===== 读 =====

    def read(
        self,
        *,
        scopes: Optional[List[str]] = None,
        query: str = "",
        limit: int = 5,
        agent_id: str = "",
        types: Optional[List[str]] = None,
    ) -> List[Any]:
        """按作用域读记忆。

        `agent_id` 传了就**只召回该 agent 的私有记忆**（scope=agent 且
        agent_id 匹配）；不传则不按 agent 过滤。
        """
        results: List[Any] = []
        seen: set = set()
        if query.strip():
            for entry, _score in self.db.search_cjk(
                query,
                types=types,
                scopes=scopes,
                agent_id=agent_id or None,
                limit=limit,
            ):
                if entry.id in seen:
                    continue
                seen.add(entry.id)
                results.append(entry)
        if len(results) < limit:
            for entry in self.db.get_recent(
                types=types,
                limit=limit,
            ):
                if entry.id in seen:
                    continue
                if scopes and entry.scope not in scopes:
                    continue
                if agent_id and entry.scope == SCOPE_AGENT and entry.agent_id != agent_id:
                    continue
                seen.add(entry.id)
                results.append(entry)
        return results[:limit]

    def read_agent_memory(
        self, *, agent_id: str, limit: int = 10
    ) -> List[Any]:
        """读某个智能体的私有记忆（它自己的工位记忆）。"""
        if not agent_id:
            return []
        return self.read(
            scopes=[SCOPE_AGENT],
            agent_id=agent_id,
            limit=limit,
        )

    # ===== 写：会话层 =====

    def write_session(
        self,
        content: str,
        *,
        session_id: str = "",
        agent_id: str = "",
        type: str = "context",
    ) -> Optional[Any]:
        """写会话记忆（主与子都可写）。"""
        if not content:
            return None
        return self.db.add(
            type=type,
            content=content,
            scope=SCOPE_SESSION,
            source_session_id=session_id or "",
            agent_id=agent_id or self._current_agent(),
        )

    # ===== 写：智能体私有层 =====

    def write_agent(
        self,
        content: str,
        *,
        agent_id: str = "",
        type: str = "episode",
        session_id: str = "",
    ) -> Optional[Any]:
        """写**自己的**私有记忆。

        子智能体只能写自己的 —— 写别人的直接拒绝（不是静默改写）。
        """
        if not content:
            return None
        owner = agent_id or self._current_agent()
        current = self._current_agent()
        if current and current not in MAIN_AGENT_IDS and owner != current:
            raise MemoryPermissionError(
                f"只能写自己的私有记忆（当前 {current}，目标 {owner}）"
            )
        return self.db.add(
            type=type,
            content=content,
            scope=SCOPE_AGENT,
            agent_id=owner,
            source_session_id=session_id or "",
        )

    # ===== 写：候选层（子 → 主） =====

    def propose_user_memory(
        self,
        content: str,
        *,
        agent_id: str = "",
        type: str = "context",
        reason: str = "",
    ) -> Optional[Dict[str, Any]]:
        """提议一条用户长期记忆（子智能体只能走这条路）。"""
        if not content:
            return None
        return self.db.add_proposal(
            content,
            agent_id=agent_id or self._current_agent(),
            type=type,
            reason=reason,
            source="agent",
        )

    def list_proposals(
        self,
        *,
        status: Optional[str] = "pending",
        agent_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        return self.db.list_proposals(
            status=status, agent_id=agent_id, limit=limit
        )

    def commit_user_memory(
        self,
        proposal_id: str,
        *,
        decided_by: str = "main",
        type: str = "",
    ) -> Optional[Any]:
        """把一条候选**落成**用户长期记忆（只有主智能体能做）。"""
        self._require_main("合并候选到用户长期记忆")
        proposal = self.db.get_proposal(proposal_id)
        if proposal is None:
            return None
        if proposal.get("status") == "accepted":
            return None
        entry = self.db.add(
            type=type or proposal.get("type") or "context",
            content=proposal.get("content", ""),
            scope=SCOPE_USER,
            metadata={"from_proposal": proposal_id},
            agent_id=proposal.get("agent_id", ""),
        )
        self.db.decide_proposal(proposal_id, status="accepted", decided_by=decided_by)
        return entry

    def reject_proposal(
        self, proposal_id: str, *, decided_by: str = "main", reason: str = ""
    ) -> bool:
        """否决一条候选（只有主智能体能做）。"""
        self._require_main("否决记忆候选")
        proposal = self.db.get_proposal(proposal_id)
        if proposal is None:
            return False
        ok = self.db.decide_proposal(
            proposal_id, status="rejected", decided_by=decided_by
        )
        if ok and reason:
            self.logger.info(
                "MemoryFacade", f"候选 {proposal_id} 被否决: {reason}"
            )
        return ok


# ===== 全局单例 =====

_facade: Optional[MemoryFacade] = None


def get_memory_facade() -> MemoryFacade:
    """全局记忆门面。"""
    global _facade
    if _facade is None:
        _facade = MemoryFacade()
    return _facade


def reset_memory_facade() -> None:
    global _facade
    _facade = None
