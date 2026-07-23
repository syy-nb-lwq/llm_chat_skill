"""FeedbackEvent - 用户对某次执行的反馈(M3-02)。

设计:
- 每个 FeedbackEvent 绑定 execution_id,确保反馈可追溯到具体执行。
- 反馈存为独立 JSON 文件,方便隔离和重放。
- correction 类型反馈会自动触发 patch 生成(在 backend API 中处理)。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


@dataclass
class FeedbackEvent:
    """用户对一次执行的反馈。"""
    id: str = ""
    execution_id: str = ""
    user_id: str = "default"
    session_id: str = "default"
    type: str = "accept"   # accept / reject / correction / retry / rating
    content: str = ""
    rating: Optional[int] = None  # 1~5
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def __post_init__(self):
        if not self.id:
            self.id = f"fb-{uuid.uuid4().hex[:10]}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FeedbackStore:
    """FeedbackEvent 持久化(按 execution_id 索引 + 全局文件)。"""

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory" / "feedback"
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base / "_index.json"
        self.logger = get_logger()

    def _index(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {"feedback": {}}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return {"feedback": {}}

    def _save_index(self, idx: Dict[str, Any]) -> None:
        self.index_path.write_text(
            json.dumps(idx, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save(self, ev: FeedbackEvent) -> Path:
        path = self.base / f"{ev.execution_id}__{ev.id}.json"
        path.write_text(
            json.dumps(ev.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        idx = self._index()
        idx.setdefault("feedback", {}).setdefault(ev.execution_id, []).append(ev.id)
        self._save_index(idx)
        return path

    def list(
        self,
        user_id: str = "default",
        execution_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        files = sorted(self.base.glob("*.json"), reverse=True)
        out: List[Dict[str, Any]] = []
        for path in files:
            if path.name == "_index.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if execution_id and data.get("execution_id") != execution_id:
                continue
            if user_id and data.get("user_id") not in (user_id, "default"):
                continue
            out.append(data)
            if len(out) >= limit:
                break
        return out

    def get_for_execution(self, execution_id: str) -> List[Dict[str, Any]]:
        idx = self._index()
        ids = idx.get("feedback", {}).get(execution_id, []) or []
        out: List[Dict[str, Any]] = []
        for fid in ids:
            path = self.base / f"{execution_id}__{fid}.json"
            if not path.exists():
                continue
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out


_store: Optional[FeedbackStore] = None


def get_feedback_store(base_path: Optional[Path] = None) -> FeedbackStore:
    global _store
    if _store is None:
        _store = FeedbackStore(base_path)
    return _store


def reset_feedback_store() -> None:
    global _store
    _store = None
