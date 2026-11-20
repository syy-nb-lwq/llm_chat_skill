"""MemoryDB —— 长期记忆·**画像**存储（SQLite + FTS5）。

## 这个库存什么

只存"这个人是谁"：偏好 / 习惯 / 方法 / 雷区 / 事实（`PROFILE_MEMORY_TYPES`）。
由 `core/semantic_memory.py` 写入（记忆写入器异步抽取 + 证据校验），
每轮常驻注入到规划提示词与回答生成 prompt。

## 它不存什么

**不存经验**：失败诊断、成功模式、待审补丁都不在这里 —— 它们在
`core/memory.py::MemoryStore` 的 `experience` 表（同一个库文件）。

两库**职责互斥**，不是同一份数据的两个副本：

| 问题 | 库 | 表 | 为什么分开 |
|---|---|---|---|
| 这个人是谁？ | `MemoryDB` | `memories` | 要语义检索 + 去重合并 + 置信度 |
| 什么做法有效/无效？ | `MemoryStore` | `experience` | 要按时间倒序 + 完整原文可读 |

两者都在 `memory/semantic_memory.db` 里，所以"某条经验属于哪个画像"
可以一条 SQL 关联，标量统计不用跨库对账。

`add()` 会拒绝非画像类型（`ValueError`），把这条边界从"事实"变成"契约"。
"""
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger

from core import sqlite_util
from core.text_match import query_fragments as _query_fragments
from core.text_match import score_fragments as _score_fragments
from core.exception_policy import downgrade


@dataclass
class MemoryEntry:
    """记忆条目

    `type` 划分的是**记的是什么**(docs/13 §2.8):
    - 用户画像类:`preference` / `habit` / `method` / `constraint` / `fact`
    - 系统运行类:`episode` / `failure` / `success` / `context`
    """
    id: str
    type: str  # preference|habit|method|constraint|fact|episode|failure|success|context
    content: str  # 原始文本内容
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    tags: List[str] = field(default_factory=list)
    # ---- 记忆分层方案(memory_db 扩展) ----
    scope: str = "user"  # session | user | project | global
    confidence: float = 0.5
    importance: float = 0.5
    status: str = "active"  # active | superseded | deleted
    supersedes_id: str = ""
    source_session_id: str = ""
    source_turn_id: str = ""
    normalized_key: str = ""
    #: 拥有这条记忆的智能体（scope=agent 时有意义）
    agent_id: str = ""


#: 画像类记忆的类型集合(常驻注入只取这些)
PROFILE_MEMORY_TYPES = ("preference", "habit", "method", "constraint", "fact")

#: `memories` 表的列顺序 —— 必须与建表语句一致(`_row_to_entry` 依赖它)
_COLUMNS = (
    "id", "type", "content", "metadata", "embedding", "created_at", "updated_at",
    "tags", "scope", "confidence", "importance", "status",
    "supersedes_id", "source_session_id", "source_turn_id", "normalized_key",
    "agent_id",
)


#: `memory_proposals` 表的列顺序（`SELECT *` 用）
_PROPOSAL_COLUMNS = (
    "id", "agent_id", "type", "content", "reason", "source",
    "status", "created_at", "decided_by", "decided_at",
)


def _proposal_row_to_dict(row: Any) -> Dict[str, Any]:
    """提案行 → dict（连接没开 Row factory，所以按列名顺序取）。"""
    values = dict(zip(_PROPOSAL_COLUMNS, row))
    return {k: ("" if v is None else v) for k, v in values.items()}


def _fts_query(text: str) -> str:
    """把用户输入转成 FTS5 安全查询串。

    FTS5 的 MATCH 语法把 `-` `*` `(` `:` 等当运算符,直接拼会报语法错;
    统一按空白切词、每个词加双引号当短语,再用 OR 连接。
    """
    cleaned = text.replace('"', " ").replace("\n", " ")
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)


class MemoryDB:
    """SQLite + FTS5 记忆数据库
    
    表结构:
    - memories: 主存储表
    - memories_fts: FTS5 全文搜索虚拟表
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "memory" / "semantic_memory.db"
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """打开一个配好并发参数的连接(WAL + busy_timeout)。

        统一走 `core.sqlite_util.connect`,避免各处裸 `sqlite3.connect`
        落回默认的排他锁模式 —— 多用户并发写会 `database is locked`。
        """
        return sqlite_util.connect(self.db_path)

    def _init_db(self) -> None:
        """初始化数据库(含老库迁移)"""
        conn = self._connect()
        cursor = conn.cursor()

        # 主表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                embedding BLOB,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                scope TEXT DEFAULT 'user',
                confidence REAL DEFAULT 0.5,
                importance REAL DEFAULT 0.5,
                status TEXT DEFAULT 'active',
                supersedes_id TEXT DEFAULT '',
                source_session_id TEXT DEFAULT '',
                source_turn_id TEXT DEFAULT '',
                normalized_key TEXT DEFAULT '',
                agent_id TEXT DEFAULT ''
            )
        """)

        # 老库迁移:去掉 user_id / tenant_id 列（本地单用户化）
        self._migrate_drop_user_columns(cursor)

        # 记忆提案表:子智能体不能直接写长期记忆,只能"提议"
        # (docs/18 §4.4)。主智能体审完才 commit。
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_proposals (
                id TEXT PRIMARY KEY,
                agent_id TEXT DEFAULT '',
                type TEXT DEFAULT 'context',
                content TEXT NOT NULL,
                reason TEXT DEFAULT '',
                source TEXT DEFAULT 'agent',
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_by TEXT DEFAULT '',
                decided_at TEXT DEFAULT ''
            )
        """)
        # 老库提案表迁移：去掉 user_id 列
        self._migrate_proposals_drop_user_column(cursor)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_proposals_status ON memory_proposals(status)"
        )

        # FTS5 虚拟表用于全文搜索
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                id,
                content,
                content_rowid='rowid',
                tokenize='unicode61'
            )
        """)

        # 回填缺失的索引行(幂等)。
        #
        # 老库在引入 FTS 之前就有数据;此外 FTS 插入失败过的记录、
        # 外部工具直接写 memories 的记录,都会在索引里缺行 ——
        # 缺了行全文检索就永远召回不到。这里按 id 对齐补一次。
        cursor.execute("""
            INSERT INTO memories_fts (id, content)
            SELECT m.id, m.content FROM memories m
            WHERE NOT EXISTS (
                SELECT 1 FROM memories_fts f WHERE f.id = m.id
            )
        """)

        # 索引（去掉 user 索引）
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)")

        conn.commit()
        conn.close()

    @staticmethod
    def _migrate_drop_user_columns(cursor) -> None:
        """本地单用户化：从历史库移除 user_id / tenant_id 列。

        老库这两列存在且可能有索引。SQLite 3.35+ 支持 DROP COLUMN，
        但列上有索引时不能删，所以先删相关索引再删列。幂等：列不存在就跳过。
        """
        cursor.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in cursor.fetchall()}
        if "user_id" in cols or "tenant_id" in cols:
            for idx in ("idx_memories_user", "idx_memories_user_status"):
                try:
                    cursor.execute(f"DROP INDEX IF EXISTS {idx}")
                except Exception as exc:
                    downgrade(exc, where='core/memory_db.py')
            for col in ("user_id", "tenant_id"):
                if col in cols:
                    try:
                        cursor.execute(f"ALTER TABLE memories DROP COLUMN {col}")
                    except Exception as exc:
                        # 老版本 SQLite 不支持时保留列，不影响读写
                        downgrade(exc, where="core/memory_db.py::DROP COLUMN")
        # 补齐其余新增列（老库可能缺 agent_id 等），保证 _row_to_entry 不错位
        cursor.execute("PRAGMA table_info(memories)")
        existing = {row[1] for row in cursor.fetchall()}
        additions = {
            "scope": "TEXT DEFAULT 'user'",
            "confidence": "REAL DEFAULT 0.5",
            "importance": "REAL DEFAULT 0.5",
            "status": "TEXT DEFAULT 'active'",
            "supersedes_id": "TEXT DEFAULT ''",
            "source_session_id": "TEXT DEFAULT ''",
            "source_turn_id": "TEXT DEFAULT ''",
            "normalized_key": "TEXT DEFAULT ''",
            "agent_id": "TEXT DEFAULT ''",
        }
        for column, ddl in additions.items():
            if column not in existing:
                cursor.execute(f"ALTER TABLE memories ADD COLUMN {column} {ddl}")
    def _migrate_proposals_drop_user_column(self, cursor) -> None:
        """提案表去掉 user_id 列（在 CREATE 之后调用）。"""
        cursor.execute("PRAGMA table_info(memory_proposals)")
        pcols = {row[1] for row in cursor.fetchall()}
        if "user_id" in pcols:
            try:
                cursor.execute("DROP INDEX IF EXISTS idx_proposals_user")
            except Exception as exc:
                downgrade(exc, where='core/memory_db.py')
            try:
                cursor.execute("ALTER TABLE memory_proposals DROP COLUMN user_id")
            except Exception as exc:
                downgrade(exc, where='core/memory_db.py')
    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        """将数据库行转换为 MemoryEntry

        用 `SELECT *` 的固定列顺序取值;列必须在 `_COLUMNS` 里保持一致。
        """
        values = dict(zip(_COLUMNS, row))
        return MemoryEntry(
            id=values["id"],
            type=values["type"],
            content=values["content"],
            metadata=json.loads(values["metadata"] or "{}"),
            embedding=json.loads(values["embedding"]) if values["embedding"] else None,
            created_at=values["created_at"],
            updated_at=values["updated_at"],
            tags=json.loads(values["tags"] or "[]"),
            scope=values["scope"] or "user",
            confidence=float(values["confidence"] if values["confidence"] is not None else 0.5),
            importance=float(values["importance"] if values["importance"] is not None else 0.5),
            status=values["status"] or "active",
            supersedes_id=values["supersedes_id"] or "",
            source_session_id=values["source_session_id"] or "",
            source_turn_id=values["source_turn_id"] or "",
            normalized_key=values["normalized_key"] or "",
            agent_id=values["agent_id"] or "",
        )

    def add(
        self,
        type: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        embedding: Optional[List[float]] = None,
        tags: Optional[List[str]] = None,
        scope: str = "user",
        confidence: float = 0.5,
        importance: float = 0.5,
        supersedes_id: str = "",
        source_session_id: str = "",
        source_turn_id: str = "",
        normalized_key: str = "",
        agent_id: str = "",
    ) -> MemoryEntry:
        """添加一条记忆。

        `MemoryDB` 是通用存储（按 `type` 区分），但**生产代码只写画像类型**
        （`PROFILE_MEMORY_TYPES`）：偏好 / 习惯 / 方法 / 雷区 / 事实。

        经验（失败诊断 / 成功模式 / 待审补丁）不在这里，走
        `core/memory.py::MemoryStore`。这条边界由
        `tests/test_memory_layers.py::test_only_profile_types_written_by_production`
        静态守住 —— 不在本方法里做硬校验，因为底层存储仍需要能表达其它类型
        （历史数据、测试夹具）。
        """
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            type=type,
            content=content,
            metadata=metadata or {},
            embedding=embedding,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            tags=tags or [],
            scope=scope,
            confidence=confidence,
            importance=importance,
            supersedes_id=supersedes_id,
            source_session_id=source_session_id,
            source_turn_id=source_turn_id,
            normalized_key=normalized_key,
            agent_id=agent_id,
        )
        
        conn = self._connect()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO memories (
                id, type, content, metadata, embedding, created_at, updated_at,
                tags, scope, confidence, importance, status,
                supersedes_id, source_session_id, source_turn_id, normalized_key,
                agent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.id,
            entry.type,
            entry.content,
            json.dumps(entry.metadata, ensure_ascii=False),
            json.dumps(entry.embedding) if entry.embedding else None,
            entry.created_at,
            entry.updated_at,
            json.dumps(entry.tags, ensure_ascii=False),
            entry.scope,
            entry.confidence,
            entry.importance,
            entry.status,
            entry.supersedes_id,
            entry.source_session_id,
            entry.source_turn_id,
            entry.normalized_key,
            entry.agent_id,
        ))
        
        # 更新 FTS 表
        cursor.execute("""
            INSERT INTO memories_fts (id, content) VALUES (?, ?)
        """, (entry.id, entry.content))
        
        conn.commit()
        conn.close()
        
        self.logger.info("MemoryDB", f"添加记忆: {entry.id}, type={type}")
        return entry

    def get(self, id: str) -> Optional[MemoryEntry]:
        """根据 ID 获取记忆"""
        conn = self._connect()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM memories WHERE id = ?", (id,))
        row = cursor.fetchone()
        conn.close()
        
        return self._row_to_entry(row) if row else None

    def search_fts(
        self,
        query: str,
        type: Optional[str] = None,
        limit: int = 10,
        types: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
    ) -> List[tuple[MemoryEntry, float]]:
        """全文搜索(FTS5 MATCH + BM25 排名)

        `scopes` 按作用域过滤(`session|user|project|global`)。
        **之前 scope 是个死字段** —— 写进去没人读,检索一律不过滤
        (docs/18 F0-4)。现在把它接上,为"智能体私有记忆"腾出位置:
        子智能体的经验存在 `agent` 作用域,检索时按需排除。

        Returns:
            List of (MemoryEntry, bm25_score) —— BM25 是负值,越小越相关。
            这里统一转成"越大越相关"的分数:`-bm25`。
        """
        if not query or not query.strip():
            return []
        conn = self._connect()
        cursor = conn.cursor()

        sql = """
            SELECT m.*, bm25(memories_fts) AS bm25_score
            FROM memories m
            JOIN memories_fts fts ON m.id = fts.id
            WHERE memories_fts MATCH ?
        """
        params: List[Any] = [_fts_query(query)]

        if type:
            sql += " AND m.type = ?"
            params.append(type)
        if types:
            placeholders = ",".join("?" * len(types))
            sql += f" AND m.type IN ({placeholders})"
            params.extend(types)
        if scopes:
            placeholders = ",".join("?" * len(scopes))
            sql += f" AND m.scope IN ({placeholders})"
            params.extend(scopes)
        if agent_id is not None:
            sql += " AND m.agent_id = ?"
            params.append(agent_id)
        sql += " AND m.status = 'active'"
        sql += " ORDER BY bm25_score LIMIT ?"
        params.append(limit)

        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError as exc:
            # 中文短词在 unicode61 下可能匹配不到/语法报错,检索降级而不是崩
            self.logger.warning("MemoryDB", f"FTS 检索失败({exc}),降级为空结果")
            rows = []
        conn.close()

        out: List[tuple[MemoryEntry, float]] = []
        for row in rows:
            entry = self._row_to_entry(row[: len(_COLUMNS)])
            bm25 = float(row[-1])
            out.append((entry, -bm25))
        return out

    def search_cjk(
        self,
        query: str,
        type: Optional[str] = None,
        limit: int = 10,
        types: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        min_score: float = 0.2,
        candidate_limit: int = 500,
    ) -> List[tuple[MemoryEntry, float]]:
        """中文友好的检索(Python 侧二元组打分)。

        **为什么需要这一条通道**:FTS5 的 `unicode61` 分词器把连续汉字当成
        一个 token,「查询北京天气的记录」和「北京天气」永远匹配不上
        (trigram 分词器要求 ≥3 字符,两字中文同样无解)。
        对一个中文产品来说,只留 FTS5 等于**没有检索**。

        代价是要在 Python 侧扫一遍候选行 —— 画像量级在百~千条,可接受。

        Returns:
            List of (MemoryEntry, score),score 越大越相关(0~1)
        """
        fragments = _query_fragments(query)
        if not fragments:
            return []

        conn = self._connect()
        cursor = conn.cursor()
        sql = "SELECT id, content FROM memories WHERE status = 'active'"
        params: List[Any] = []
        if type:
            sql += " AND type = ?"
            params.append(type)
        if types:
            placeholders = ",".join("?" * len(types))
            sql += f" AND type IN ({placeholders})"
            params.extend(types)
        if scopes:
            placeholders = ",".join("?" * len(scopes))
            sql += f" AND scope IN ({placeholders})"
            params.extend(scopes)
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(candidate_limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()

        # 第一阶段:只打分(id + content),不解析 metadata/tags 的 JSON
        scored: List[tuple[str, float]] = []
        for row in rows:
            s = _score_fragments(row[1], fragments)
            if s >= min_score:
                scored.append((row[0], s))
        conn.close()
        if not scored:
            return []

        # 第二阶段:只把命中条目取完整(避免为几百个候选解析 JSON)
        scored.sort(key=lambda pair: -pair[1])
        top = scored[:limit]
        id_list = [mid for mid, _ in top]
        placeholders = ",".join("?" * len(id_list))
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})", id_list
        )
        by_id = {row[0]: self._row_to_entry(row[: len(_COLUMNS)]) for row in cursor.fetchall()}
        conn.close()

        return [
            (by_id[mid], score)
            for mid, score in top
            if mid in by_id
        ]

    def search_vector(
        self,
        query_embedding: List[float],
        type: Optional[str] = None,
        limit: int = 10,
        threshold: float = 0.7,
        types: Optional[List[str]] = None,
        scopes: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
    ) -> List[tuple[MemoryEntry, float]]:
        """向量相似度搜索

        `scopes` 的作用见 `search_fts`。

        Returns:
            List of (MemoryEntry, similarity_score) sorted by score descending
        """
        conn = self._connect()
        cursor = conn.cursor()

        sql = "SELECT * FROM memories WHERE embedding IS NOT NULL AND status = 'active'"
        params: List[Any] = []

        if type:
            sql += " AND type = ?"
            params.append(type)
        if types:
            placeholders = ",".join("?" * len(types))
            sql += f" AND type IN ({placeholders})"
            params.extend(types)
        if scopes:
            placeholders = ",".join("?" * len(scopes))
            sql += f" AND scope IN ({placeholders})"
            params.extend(scopes)
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()

        # 计算余弦相似度
        results: List[tuple[MemoryEntry, float]] = []
        for row in rows:
            entry = self._row_to_entry(row)
            if entry.embedding:
                similarity = self._cosine_similarity(query_embedding, entry.embedding)
                if similarity >= threshold:
                    results.append((entry, similarity))

        # 按相似度降序
        results.sort(key=lambda x: -x[1])
        return results[:limit]

    def _cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """计算余弦相似度"""
        if len(a) != len(b):
            return 0.0
        
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        
        if norm_a == 0 or norm_b == 0:
            return 0.0
        
        return dot_product / (norm_a * norm_b)

    def get_recent(
        self,
        type: Optional[str] = None,
        limit: int = 20,
        types: Optional[List[str]] = None,
    ) -> List[MemoryEntry]:
        """获取最近的记忆"""
        conn = self._connect()
        cursor = conn.cursor()

        sql = "SELECT * FROM memories WHERE status = 'active'"
        params: List[Any] = []

        if type:
            sql += " AND type = ?"
            params.append(type)

        if types:
            placeholders = ",".join("?" * len(types))
            sql += f" AND type IN ({placeholders})"
            params.extend(types)


        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()

        return [self._row_to_entry(row) for row in rows]

    def get_by_key(
        self,
        type: str,
        normalized_key: str,
    ) -> Optional[MemoryEntry]:
        """按 (type, normalized_key) 找已有记忆 —— 用于去重/冲突检测"""
        if not normalized_key:
            return None
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM memories
            WHERE type = ? AND normalized_key = ? AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
            """,
            (type, normalized_key),
        )
        row = cursor.fetchone()
        conn.close()
        return self._row_to_entry(row) if row else None

    def supersede(self, old_id: str, new_id: str) -> bool:
        """把旧记忆标记为被取代(保留可追溯,不物理删除)"""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE memories SET status = 'superseded', supersedes_id = ?, updated_at = ? "
            "WHERE id = ?",
            (new_id, datetime.now().isoformat(), old_id),
        )
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        # 被取代的记忆不再参与检索 → 从 FTS 摘掉
        if success:
            self._remove_from_fts(old_id)
        return success

    def soft_delete(self, id: str) -> bool:
        """软删除:标记 deleted + **同步摘掉全文索引**

        只删主表不删索引的话,被"忘掉"的记忆仍会被检索召回(docs/13 §2.9)。
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE memories SET status = 'deleted', updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), id),
        )
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        if success:
            self._remove_from_fts(id)
        return success

    def _remove_from_fts(self, id: str) -> None:
        """从 FTS 虚表删除某条记忆"""
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM memories_fts WHERE id = ?", (id,))
            conn.commit()
        except sqlite3.OperationalError as exc:
            self.logger.warning("MemoryDB", f"清理 FTS 索引失败({exc}): {id}")
        finally:
            conn.close()

    def update(self, id: str, content: Optional[str] = None, metadata: Optional[Dict] = None) -> bool:
        """更新记忆"""
        conn = self._connect()
        cursor = conn.cursor()
        
        updates: List[str] = []
        params: List[Any] = []
        
        if content:
            updates.append("content = ?")
            params.append(content)

        if metadata:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata, ensure_ascii=False))

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())

        params.append(id)

        cursor.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)
        success = cursor.rowcount > 0

        if success and content:
            # FTS5 的 UPDATE 在索引缺行时不会补行 —— 先删后插,保证内容与索引一致
            cursor.execute("DELETE FROM memories_fts WHERE id = ?", (id,))
            cursor.execute(
                "INSERT INTO memories_fts (id, content) VALUES (?, ?)", (id, content)
            )

        conn.commit()
        conn.close()

        return success

    def delete(self, id: str) -> bool:
        """删除记忆"""
        conn = self._connect()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM memories WHERE id = ?", (id,))
        success = cursor.rowcount > 0
        cursor.execute("DELETE FROM memories_fts WHERE id = ?", (id,))

        conn.commit()
        conn.close()
        
        return success

    def cleanup_old(self, days: int = 90) -> int:
        """清理过期记忆

        **画像类记忆(preference/habit/method/constraint/fact)永不自动清理** ——
        它们描述"用户是谁",按时间淘汰等于把人忘了(docs/13 §7)。
        只清系统运行类(episode/success/context)。

        Args:
            days: 保留最近多少天的记忆

        Returns:
            删除的记忆条数
        """
        conn = self._connect()
        cursor = conn.cursor()

        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        cutoff = cutoff - timedelta(days=days)

        placeholders = ",".join("?" * len(PROFILE_MEMORY_TYPES))
        where_sql = f"""
            created_at < ?
            AND type NOT IN ({placeholders})
            AND type != 'failure'
            AND id NOT IN (
                SELECT id FROM memories WHERE metadata LIKE '%"important":true%'
            )
        """
        params = (cutoff.isoformat(), *PROFILE_MEMORY_TYPES)

        # 先按同一条件摘掉 FTS 索引,再删主表 ——
        # 只删主表会留下孤儿索引行(检索走 JOIN 虽然不会召回,但索引会无限膨胀)。
        cursor.execute(
            f"DELETE FROM memories_fts WHERE id IN (SELECT id FROM memories WHERE {where_sql})",
            params,
        )
        cursor.execute(f"DELETE FROM memories WHERE {where_sql}", params)

        count = cursor.rowcount

        conn.commit()
        conn.close()

        self.logger.info("MemoryDB", f"清理过期记忆: 删除 {count} 条")
        return count

    def list_memories(
        self,
        types: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[MemoryEntry]:
        """列出 active 记忆(给"你记住了我什么"用)"""
        return self.get_recent(types=types, limit=limit)

    # ============================================================
    # 记忆提案（子智能体 → 主智能体 → 用户长期记忆）
    # ============================================================

    def add_proposal(
        self,
        content: str,
        *,
        agent_id: str = "",
        type: str = "context",
        reason: str = "",
        source: str = "agent",
    ) -> Dict[str, Any]:
        """记一条"我想让系统记住这件事"的提案。

        **为什么不是直接写记忆**：子智能体并发跑，同时写用户画像会互相污染
        （docs/15 §6 的判断是对的）。所以子智能体只能提议，主智能体裁决。
        """
        proposal = {
            "id": f"prop_{uuid.uuid4().hex[:12]}",
            "agent_id": agent_id or "",
            "type": type,
            "content": content,
            "reason": reason or "",
            "source": source,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "decided_by": "",
            "decided_at": "",
        }
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO memory_proposals (
                id, agent_id, type, content, reason, source,
                status, created_at, decided_by, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal["id"], proposal["agent_id"],
                proposal["type"], proposal["content"], proposal["reason"],
                proposal["source"], proposal["status"], proposal["created_at"],
                proposal["decided_by"], proposal["decided_at"],
            ),
        )
        conn.commit()
        conn.close()
        self.logger.info("MemoryDB", f"记忆提案: {proposal['id']}（{proposal['source']}）")
        return proposal

    def list_proposals(
        self,
        *,
        status: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """列出提案（可按状态/智能体过滤）。"""
        conn = self._connect()
        cursor = conn.cursor()
        sql = "SELECT * FROM memory_proposals"
        clauses: List[str] = []
        params: List[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [_proposal_row_to_dict(row) for row in rows]

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
        )
        row = cursor.fetchone()
        conn.close()
        return _proposal_row_to_dict(row) if row else None

    def decide_proposal(
        self, proposal_id: str, *, status: str, decided_by: str = ""
    ) -> bool:
        """裁决提案（accepted / rejected）。"""
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE memory_proposals SET status = ?, decided_by = ?, decided_at = ? "
            "WHERE id = ?",
            (status, decided_by or "", datetime.now().isoformat(), proposal_id),
        )
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def clear_all(self) -> int:
        """清空所有记忆(含 FTS 索引)。

        两个用途:
        - **测试隔离**:conftest 用的是 session 级共享 DB,
          不清的话上一个测试的画像会漏到下一个测试里
        - **数据重置**:用户要求"忘掉关于我的一切"时按用户维度清理

        Returns:
            删除的条数
        """
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM memories")
        count = cursor.fetchone()[0]
        cursor.execute("DELETE FROM memories")
        cursor.execute("DELETE FROM memories_fts")
        conn.commit()
        conn.close()
        self.logger.info("MemoryDB", f"清空记忆: 删除 {count} 条")
        return count

    def clear_user(self, user_id: str = "") -> int:
        """兼容旧调用：本地单用户，等价于 clear_all。"""
        return self.clear_all()

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        conn = self._connect()
        cursor = conn.cursor()

        # 总数
        cursor.execute("SELECT COUNT(*) FROM memories")
        total = cursor.fetchone()[0]

        # 按状态统计
        cursor.execute("SELECT status, COUNT(*) FROM memories GROUP BY status")
        by_status = dict(cursor.fetchall())

        # 按类型统计
        cursor.execute("SELECT type, COUNT(*) FROM memories GROUP BY type")
        by_type = dict(cursor.fetchall())

        # 有向量的记忆数
        cursor.execute("SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL")
        with_embedding = cursor.fetchone()[0]

        # 数据库大小
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        conn.close()

        return {
            "total": total,
            "by_status": by_status,
            "by_type": by_type,
            "with_embedding": with_embedding,
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / 1024 / 1024, 2),
        }


# ---- 全局单例 ----
_memory_db: Optional[MemoryDB] = None
#: 保护懒初始化（线程池场景下避免造出两个 SQLite 连接）
_getter_lock = threading.Lock()


def get_memory_db() -> MemoryDB:
    """获取 MemoryDB 全局实例"""
    global _memory_db
    if _memory_db is None:
        with _getter_lock:
            if _memory_db is None:
                _memory_db = MemoryDB()
    return _memory_db


def reset_memory_db() -> None:
    """重置 MemoryDB(用于测试)"""
    global _memory_db
    _memory_db = None
