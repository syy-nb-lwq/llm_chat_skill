"""TeachingSession - 多轮教学状态机(M1-01)

设计:
- 每个教学会话用 ``teaching_session_id`` 标识
- 状态:Collecting → Draft → Testing → AwaitingApproval → Active
- 持久化:使用 ``teachings/{teaching_session_id}.json`` 保存
- 字段:teaching_session_id, user_id, session_id, draft_skill, evidence_turns,
       missing_fields, current_question, user_choice, status, created_at, updated_at
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


class TeachingStatus:
    COLLECTING = "collecting"
    DRAFT = "draft"
    TESTING = "testing"
    AWAITING_APPROVAL = "awaiting_approval"
    ACTIVE = "active"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


REQUIRED_FIELDS = ("name", "method", "capability")


@dataclass
class TeachingSession:
    teaching_session_id: str
    user_id: str
    session_id: str
    status: str = TeachingStatus.COLLECTING
    partial_skill: Dict[str, Any] = field(default_factory=dict)
    evidence_turns: List[Dict[str, str]] = field(default_factory=list)  # [{role, content}]
    missing_fields: List[str] = field(default_factory=list)
    current_question: str = ""
    user_choice: str = ""  # for duplicate-skill: reuse/update_new/cancel
    duplicate_of: Optional[str] = None  # 已存在的同名 skill
    draft_skill: Optional[Dict[str, Any]] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def touch(self):
        self.updated_at = time.time()

    def is_terminal(self) -> bool:
        return self.status in (TeachingStatus.ACTIVE, TeachingStatus.REJECTED, TeachingStatus.CANCELLED)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def new(cls, user_id: str, session_id: str) -> "TeachingSession":
        return cls(
            teaching_session_id=f"teach-{uuid.uuid4().hex[:10]}",
            user_id=user_id,
            session_id=session_id,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TeachingSession":
        # 兼容旧缺省字段
        return cls(
            teaching_session_id=data.get("teaching_session_id", ""),
            user_id=data.get("user_id", "default"),
            session_id=data.get("session_id", "default"),
            status=data.get("status", TeachingStatus.COLLECTING),
            partial_skill=data.get("partial_skill", {}) or {},
            evidence_turns=data.get("evidence_turns", []) or [],
            missing_fields=data.get("missing_fields", []) or [],
            current_question=data.get("current_question", "") or "",
            user_choice=data.get("user_choice", "") or "",
            duplicate_of=data.get("duplicate_of"),
            draft_skill=data.get("draft_skill"),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


class TeachingSessionStore:
    """TeachingSession 持久化(基于 JSON 文件,后期可替换为 MemoryRepository)。"""

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).resolve().parent.parent / "memory" / "teachings"
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()

    def _path(self, teaching_session_id: str) -> Path:
        return self.base / f"{teaching_session_id}.json"

    def save(self, ts: TeachingSession) -> Path:
        ts.touch()
        path = self._path(ts.teaching_session_id)
        path.write_text(
            json.dumps(ts.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def get(self, teaching_session_id: str) -> Optional[TeachingSession]:
        path = self._path(teaching_session_id)
        if not path.exists():
            return None
        try:
            return TeachingSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:
            self.logger.warning("TeachingSessionStore", f"读取失败 {teaching_session_id}: {e}")
            return None

    def find_active_for(self, user_id: str, session_id: str) -> Optional[TeachingSession]:
        """找到当前 session 上尚未结束的最近一个教学会话。"""
        candidates: List[TeachingSession] = []
        for path in self.base.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            ts = TeachingSession.from_dict(data)
            if ts.user_id == user_id and ts.session_id == session_id and not ts.is_terminal():
                candidates.append(ts)
        candidates.sort(key=lambda x: x.updated_at, reverse=True)
        return candidates[0] if candidates else None

    def list_active(self) -> List[TeachingSession]:
        out: List[TeachingSession] = []
        for path in self.base.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            ts = TeachingSession.from_dict(data)
            if not ts.is_terminal():
                out.append(ts)
        return out


_store: Optional[TeachingSessionStore] = None


def get_teaching_store(base_path: Optional[Path] = None) -> TeachingSessionStore:
    global _store
    if _store is None:
        _store = TeachingSessionStore(base_path)
    return _store


def reset_teaching_store():
    global _store
    _store = None
