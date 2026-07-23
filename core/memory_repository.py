"""MemoryRepository - 统一长期记忆访问层(M2-01 / M2-02)。

设计:
- 所有上层代码(MemoryStore / SemanticMemory / Critic / Agent)
  不再直接读写 JSON、JSONL、SQLite。
- 全部通过 ``MemoryRepository`` 这一层访问。
- 模型层是 ``MemoryItem``(覆盖了旧的 FailureRecord / SuccessRecord /
  SemanticMemoryDB.MemoryEntry 等多个概念;短期和长期记忆都使用同一 schema
  描述,通过 scope / type 区分)。
- 持久化方式:短期/失败/成功用 JSON(Python dataclass 序列化);
  语义/偏好/项目事实用 SQLite + FTS5 + 嵌入向量。

不破坏现有 API:
- ``MemoryStore``(失败/成功/patches)继续可用,但内部委托到本 Repository。
- ``SemanticMemoryStore`` 继续可用,委托到 ``Repository.memory_db``.
- ``Agent.handle()`` 通过 ``memory_repo.add_episode()`` 写入对话结果。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ===== Enum =====


class MemoryScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    PROJECT = "project"
    SESSION = "session"


class MemoryType(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    CONTEXT = "context"
    EPISODE = "episode"           # 一段经历的总结(成功/失败/对话)
    LESSON = "lesson"             # 从反思里凝练出来的教训
    SKILL_HINT = "skill_hint"


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ===== 数据模型 =====


@dataclass
class MemoryItem:
    """统一的长期记忆条目(M2-02)。

    字段命名遵循 ``docs/10-目标架构评审与演进方案.md §6.2``。
    """
    id: str = ""
    user_id: str = "default"
    project_id: str = ""
    session_id: str = ""
    turn_id: str = ""
    execution_id: str = ""
    scope: str = MemoryScope.USER.value
    type: str = MemoryType.FACT.value
    content: str = ""
    structured_value: Dict[str, Any] = field(default_factory=dict)
    source_turn_id: str = ""
    confidence: float = 0.7
    sensitivity: str = "normal"  # normal / secret / pii
    valid_from: str = ""
    valid_until: str = ""
    supersedes_id: str = ""
    status: str = MemoryStatus.ACTIVE.value
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = f"mem-{uuid.uuid4().hex[:10]}"
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def is_active(self) -> bool:
        return self.status == MemoryStatus.ACTIVE.value


# ===== SQLite 存储层 =====


class _MemoryDB:
    """SQLite + FTS5 + 向量存储(M2-01 统一存储)。

    仅供 ``MemoryRepository`` 内部使用,业务代码不应直接访问。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        project_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        turn_id TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL,
        type TEXT NOT NULL,
        content TEXT NOT NULL,
        structured_value TEXT DEFAULT '{}',
        source_turn_id TEXT DEFAULT '',
        confidence REAL DEFAULT 0.7,
        sensitivity TEXT DEFAULT 'normal',
        valid_from TEXT DEFAULT '',
        valid_until TEXT DEFAULT '',
        supersedes_id TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        tags TEXT DEFAULT '[]',
        metadata TEXT DEFAULT '{}',
        embedding BLOB,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
    CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
    CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
    CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
    CREATE INDEX IF NOT EXISTS idx_memories_execution ON memories(execution_id);

    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        id, content, tokenize='unicode61'
    );
    """

    # 兼容旧 SemanticMemoryStore 的表(没有 scope 等列)
    ALT_COLUMNS = (
        "user_id TEXT NOT NULL DEFAULT 'default',"
        "project_id TEXT NOT NULL DEFAULT '',"
        "session_id TEXT NOT NULL DEFAULT '',"
        "turn_id TEXT NOT NULL DEFAULT '',"
        "execution_id TEXT NOT NULL DEFAULT '',"
        "scope TEXT NOT NULL DEFAULT 'user',"
        "type TEXT NOT NULL DEFAULT 'context',"
        "source_turn_id TEXT DEFAULT '',"
        "sensitivity TEXT DEFAULT 'normal',"
        "valid_from TEXT DEFAULT '',"
        "valid_until TEXT DEFAULT '',"
        "supersedes_id TEXT DEFAULT '',"
        "status TEXT NOT NULL DEFAULT 'active',"
    )

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            # 用单条 CREATE 走 IF NOT EXISTS,避免新列缺漏导致后续创建失败
            conn.executescript(self.SCHEMA)
            # 旧库兼容:补齐缺失列
            for col_def in self.ALT_COLUMNS.split(","):
                col_name = col_def.strip().split(" ", 1)[0]
                try:
                    conn.execute(
                        f"ALTER TABLE memories ADD COLUMN {col_def}",
                    )
                except Exception:
                    # 已存在,忽略
                    pass
            conn.commit()
            # 让 SQLite 重建索引(创建覆盖)
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)")
            except Exception:
                pass
        finally:
            conn.close()

    # ----- 反序列化 -----

    @staticmethod
    def _row_to_item(row: tuple) -> MemoryItem:
        def _json(col: int) -> Any:
            raw = row[col]
            if not raw:
                return {} if col in (9, 18) else []
            try:
                return json.loads(raw)
            except Exception:
                return {} if col in (9, 18) else []

        embedding_raw = row[19]
        embedding = None
        if embedding_raw:
            try:
                embedding = json.loads(embedding_raw)
            except Exception:
                embedding = None

        return MemoryItem(
            id=row[0],
            user_id=row[1] or "default",
            project_id=row[2] or "",
            session_id=row[3] or "",
            turn_id=row[4] or "",
            execution_id=row[5] or "",
            scope=row[6] or MemoryScope.USER.value,
            type=row[7] or MemoryType.FACT.value,
            content=row[8],
            structured_value=_json(9),
            source_turn_id=row[10] or "",
            confidence=float(row[11] or 0.7),
            sensitivity=row[12] or "normal",
            valid_from=row[13] or "",
            valid_until=row[14] or "",
            supersedes_id=row[15] or "",
            status=row[16] or MemoryStatus.ACTIVE.value,
            tags=_json(17),
            metadata=_json(18),
            embedding=embedding,
            created_at=row[20] or "",
            updated_at=row[21] or "",
        )

    # ----- CRUD -----

    def upsert(self, item: MemoryItem) -> MemoryItem:
        item.updated_at = datetime.now().isoformat()
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            row = (
                item.id,
                item.user_id,
                item.project_id,
                item.session_id,
                item.turn_id,
                item.execution_id,
                item.scope,
                item.type,
                item.content,
                json.dumps(item.structured_value, ensure_ascii=False),
                item.source_turn_id,
                float(item.confidence),
                item.sensitivity,
                item.valid_from,
                item.valid_until,
                item.supersedes_id,
                item.status,
                json.dumps(item.tags, ensure_ascii=False),
                json.dumps(item.metadata, ensure_ascii=False),
                json.dumps(item.embedding) if item.embedding else None,
                item.created_at,
                item.updated_at,
            )
            cursor.execute(
                """
                INSERT OR REPLACE INTO memories VALUES (
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                row,
            )
            cursor.execute(
                "INSERT INTO memories_fts(id, content) VALUES(?, ?)",
                (item.id, item.content),
            )
            conn.commit()
        finally:
            conn.close()
        return item

    def get(self, item_id: str) -> Optional[MemoryItem]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM memories WHERE id = ?", (item_id,))
            row = cursor.fetchone()
        finally:
            conn.close()
        return self._row_to_item(row) if row else None

    def list(
        self,
        *,
        user_id: Optional[str] = None,
        scope: Optional[str] = None,
        type: Optional[str] = None,
        limit: int = 50,
    ) -> List[MemoryItem]:
        sql = "SELECT * FROM memories WHERE status != 'expired'"
        params: List[Any] = []
        if user_id:
            # M2-06:作用域过滤,允许 user 看到自己的 + global
            sql += " AND (user_id = ? OR scope = 'global')"
            params.append(user_id)
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if type:
            sql += " AND type = ?"
            params.append(type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [self._row_to_item(r) for r in rows]

    def delete(self, item_id: str) -> bool:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM memories WHERE id = ?", (item_id,))
            cursor.execute("DELETE FROM memories_fts WHERE id = ?", (item_id,))
            conn.commit()
            ok = cursor.rowcount > 0
        finally:
            conn.close()
        return ok

    def delete_by_user(self, user_id: str) -> int:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM memories WHERE user_id = ?", (user_id,))
            ids = [r[0] for r in cursor.fetchall()]
            for i in ids:
                cursor.execute("DELETE FROM memories WHERE id = ?", (i,))
                cursor.execute("DELETE FROM memories_fts WHERE id = ?", (i,))
            conn.commit()
        finally:
            conn.close()
        return len(ids)

    def search_fts(
        self,
        query: str,
        *,
        user_id: Optional[str] = None,
        type: Optional[str] = None,
        limit: int = 10,
    ) -> List[MemoryItem]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            sql = """
                SELECT m.* FROM memories m
                JOIN memories_fts fts ON m.id = fts.id
                WHERE memories_fts MATCH ?
                  AND m.status != 'expired'
            """
            params: List[Any] = [query]
            if user_id:
                sql += " AND (m.user_id = ? OR m.scope = 'global')"
                params.append(user_id)
            if type:
                sql += " AND m.type = ?"
                params.append(type)
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()
        return [self._row_to_item(r) for r in rows]

    def search_vector(
        self,
        query_embedding: List[float],
        *,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Tuple[MemoryItem, float]]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            sql = "SELECT * FROM memories WHERE embedding IS NOT NULL AND status != 'expired'"
            params: List[Any] = []
            if user_id:
                sql += " AND (user_id = ? OR scope = 'global')"
                params.append(user_id)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        finally:
            conn.close()
        out: List[Tuple[MemoryItem, float]] = []
        for r in rows:
            item = self._row_to_item(r)
            if not item.embedding:
                continue
            sim = _cosine(query_embedding, item.embedding)
            if sim > 0.0:
                out.append((item, sim))
        out.sort(key=lambda x: -x[1])
        return out[:limit]

    def cleanup_expired(self) -> int:
        now = datetime.now().isoformat()
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE memories SET status='expired' WHERE valid_until != '' AND valid_until < ? AND status != 'expired'",
                (now,),
            )
            count = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return count

    def get_stats(self) -> Dict[str, Any]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM memories")
            total = cursor.fetchone()[0]
            cursor.execute("SELECT scope, COUNT(*) FROM memories GROUP BY scope")
            by_scope = dict(cursor.fetchall())
            cursor.execute("SELECT type, COUNT(*) FROM memories GROUP BY type")
            by_type = dict(cursor.fetchall())
        finally:
            conn.close()
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "total": total,
            "by_scope": by_scope,
            "by_type": by_type,
            "db_size_bytes": db_size,
        }


def _cosine(a: List[float], b: List[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


# ===== JSON-side Episodes / Failures / Successes =====


@dataclass
class EpisodeRecord:
    """一次执行的整体记录(M0-02/M3)。

    与 ``FailureRecord`` 兼容:同时保留旧字段名,避免破坏现有测试。
    """
    execution_id: str
    trace_id: str
    user_id: str
    session_id: str
    turn_id: str
    scenario: str
    intent: str
    selected_skill: str
    selected_skill_version: str
    success_rate: float
    fallback_count: int
    retry_count: int
    latency_ms: float
    diagnosis: str = ""
    user_corrected: bool = False
    user_feedback: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    skill_diagnosis: str = ""
    tool_attempts: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # 兼容旧 FailureRecord 字段
    @property
    def id(self) -> str:
        return f"ep-{self.execution_id}"


# ===== MemoryRepository =====


_SENSITIVE_KEYWORDS = (
    "身份证", "password", "密码", "secret", "token", "api_key",
    "ssn", "openai",
)


class MemoryRepository:
    """统一记忆仓库(M2-01)。

    业务代码只调用 ``MemoryRepository``,不直接读写任何存储。

    用法:
        repo = MemoryRepository()
        repo.add_episode(EpisodeRecord(...))
        items = repo.recall(user_id="alice", query="weather")
    """

    def __init__(
        self,
        base_path: Optional[Path] = None,
        embedding_service: Optional[Any] = None,
    ):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory"
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)

        self.episodes_dir = self.base / "episodes"
        self.episodes_dir.mkdir(exist_ok=True)

        # SQLite 用于语义/偏好
        self._db = _MemoryDB(self.base / "semantic_memory.db")

        self._embedding_service = embedding_service
        self._embedding_failed = False

    # ===== embedding =====
    @property
    def embedding_service(self):
        return self._embedding_service

    def set_embedding_service(self, service: Any) -> None:
        self._embedding_service = service
        self._embedding_failed = False

    async def _try_embed(self, text: str) -> Optional[List[float]]:
        if not self._embedding_service or self._embedding_failed:
            return None
        try:
            return await self._embedding_service.embed(text)
        except Exception:
            # M2-04:embedding 失败时降级为全文检索,不阻塞主流程
            self._embedding_failed = True
            return None

    # ===== 写入 =====

    def add_memory_item(self, item: MemoryItem) -> MemoryItem:
        # M2 写入策略:敏感检查 + 冲突检测
        if item.sensitivity == "normal" and any(
            kw in item.content.lower() for kw in _SENSITIVE_KEYWORDS
        ):
            item.sensitivity = "secret"
        item = self._db.upsert(item)
        return item

    def list_memory(
        self,
        user_id: str,
        *,
        scope: Optional[str] = None,
        type: Optional[str] = None,
        limit: int = 50,
    ) -> List[MemoryItem]:
        return self._db.list(user_id=user_id, scope=scope, type=type, limit=limit)

    def get_memory(self, item_id: str) -> Optional[MemoryItem]:
        return self._db.get(item_id)

    def delete_memory(self, item_id: str, user_id: str = "default") -> bool:
        item = self._db.get(item_id)
        if not item:
            return False
        if user_id and item.user_id not in (user_id, "default", "global"):
            return False
        return self._db.delete(item_id)

    def forget_user(self, user_id: str) -> int:
        return self._db.delete_by_user(user_id)

    # ===== Episode 写入 =====

    def add_episode(self, ep: EpisodeRecord) -> Path:
        # 按执行 id 命名,确保每次 handle 不覆盖(M0-01)
        path = self.episodes_dir / f"{ep.execution_id}.json"
        path.write_text(
            json.dumps(ep.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def list_episodes(
        self, user_id: Optional[str] = None, limit: int = 20
    ) -> List[EpisodeRecord]:
        files = sorted(self.episodes_dir.glob("*.json"), reverse=True)
        out: List[EpisodeRecord] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if user_id and data.get("user_id") not in (user_id, "default"):
                continue
            try:
                out.append(EpisodeRecord(**data))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    def get_episode(self, execution_id: str) -> Optional[EpisodeRecord]:
        path = self.episodes_dir / f"{execution_id}.json"
        if not path.exists():
            return None
        try:
            return EpisodeRecord(
                **json.loads(path.read_text(encoding="utf-8")),
            )
        except Exception:
            return None

    # ===== 召回 =====

    async def recall(
        self,
        query: str,
        *,
        user_id: str = "default",
        project_id: str = "",
        type: Optional[str] = None,
        limit: int = 5,
    ) -> List[MemoryItem]:
        """混合召回:M2-06 作用域过滤 + 全文/向量。"""
        seen: Dict[str, MemoryItem] = {}

        # FTS
        try:
            fts_items = self._db.search_fts(
                query, user_id=user_id, type=type, limit=limit * 2,
            )
            for it in fts_items:
                seen[it.id] = it
        except Exception:
            pass

        # 向量
        if self._embedding_service and not self._embedding_failed:
            emb = await self._try_embed(query)
            if emb:
                vec = self._db.search_vector(
                    emb, user_id=user_id, limit=limit * 2,
                )
                for it, _score in vec:
                    seen[it.id] = it

        # 在 user 作用域过滤之上,允许 project 与 user 级记忆同时出现
        results = list(seen.values())
        # 排序:user > project > global,然后置信度
        scope_rank = {
            MemoryScope.USER.value: 0,
            MemoryScope.PROJECT.value: 1,
            MemoryScope.GLOBAL.value: 2,
        }
        results.sort(
            key=lambda it: (
                scope_rank.get(it.scope, 9),
                -float(it.confidence or 0.0),
                -(0 if it.valid_until == "" else 1),
            )
        )
        return results[:limit]

    async def recall_strings(
        self,
        query: str,
        *,
        user_id: str = "default",
        project_id: str = "",
        type: Optional[str] = None,
        limit: int = 5,
    ) -> List[str]:
        items = await self.recall(
            query=query,
            user_id=user_id,
            project_id=project_id,
            type=type,
            limit=limit,
        )
        out: List[str] = []
        for it in items:
            label = {
                MemoryType.PREFERENCE.value: "[偏好]",
                MemoryType.FACT.value: "[事实]",
                MemoryType.CONTEXT.value: "[上下文]",
                MemoryType.EPISODE.value: "[经历]",
                MemoryType.LESSON.value: "[教训]",
                MemoryType.SKILL_HINT.value: "[技能提示]",
            }.get(it.type, "[记忆]")
            out.append(f"{label} {it.content}")
        return out

    # ===== 维护 =====

    def cleanup(self) -> int:
        return self._db.cleanup_expired()

    def get_stats(self) -> Dict[str, Any]:
        return self._db.get_stats()


# ===== 全局单例 =====

_repo: Optional[MemoryRepository] = None


def get_memory_repository() -> MemoryRepository:
    """获取 MemoryRepository 单例(M2-01 统一入口)。"""
    global _repo
    if _repo is None:
        _repo = MemoryRepository()
    return _repo


def reset_memory_repository() -> None:
    """重置单例,主要用于测试。"""
    global _repo
    _repo = None
