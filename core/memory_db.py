"""MemoryDB - SQLite + FTS5 长期记忆存储"""
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    type: str  # "failure" | "success" | "preference" | "context"
    content: str  # 原始文本内容
    metadata: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    tags: List[str] = field(default_factory=list)
    user_id: Optional[str] = None  # 多用户支持


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

    def _init_db(self) -> None:
        """初始化数据库"""
        conn = sqlite3.connect(str(self.db_path))
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
                user_id TEXT
            )
        """)
        
        # FTS5 虚拟表用于全文搜索
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                id,
                content,
                content_rowid='rowid',
                tokenize='unicode61'
            )
        """)
        
        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)")
        
        conn.commit()
        conn.close()

    def _row_to_entry(self, row: tuple) -> MemoryEntry:
        """将数据库行转换为 MemoryEntry"""
        return MemoryEntry(
            id=row[0],
            type=row[1],
            content=row[2],
            metadata=json.loads(row[3]),
            embedding=json.loads(row[4]) if row[4] else None,
            created_at=row[5],
            updated_at=row[6],
            tags=json.loads(row[7]),
            user_id=row[8],
        )

    def add(
        self,
        type: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        embedding: Optional[List[float]] = None,
        tags: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> MemoryEntry:
        """添加记忆"""
        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            type=type,
            content=content,
            metadata=metadata or {},
            embedding=embedding,
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            tags=tags or [],
            user_id=user_id,
        )
        
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO memories (id, type, content, metadata, embedding, created_at, updated_at, tags, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.id,
            entry.type,
            entry.content,
            json.dumps(entry.metadata),
            json.dumps(entry.embedding) if entry.embedding else None,
            entry.created_at,
            entry.updated_at,
            json.dumps(entry.tags),
            entry.user_id,
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
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM memories WHERE id = ?", (id,))
        row = cursor.fetchone()
        conn.close()
        
        return self._row_to_entry(row) if row else None

    def search_fts(self, query: str, type: Optional[str] = None, limit: int = 10) -> List[MemoryEntry]:
        """全文搜索"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # FTS5 搜索
        sql = """
            SELECT m.* FROM memories m
            JOIN memories_fts fts ON m.id = fts.id
            WHERE memories_fts MATCH ?
        """
        params: List[Any] = [query]
        
        if type:
            sql += " AND m.type = ?"
            params.append(type)
        
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        
        return [self._row_to_entry(row) for row in rows]

    def search_vector(
        self,
        query_embedding: List[float],
        type: Optional[str] = None,
        limit: int = 10,
        threshold: float = 0.7,
    ) -> List[tuple[MemoryEntry, float]]:
        """向量相似度搜索
        
        Returns:
            List of (MemoryEntry, similarity_score) sorted by score descending
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        sql = "SELECT * FROM memories WHERE embedding IS NOT NULL"
        params: List[Any] = []
        
        if type:
            sql += " AND type = ?"
            params.append(type)
        
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
        user_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[MemoryEntry]:
        """获取最近的记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        sql = "SELECT * FROM memories WHERE 1=1"
        params: List[Any] = []
        
        if type:
            sql += " AND type = ?"
            params.append(type)
        
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        
        return [self._row_to_entry(row) for row in rows]

    def update(self, id: str, content: Optional[str] = None, metadata: Optional[Dict] = None) -> bool:
        """更新记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        updates: List[str] = []
        params: List[Any] = []
        
        if content:
            updates.append("content = ?")
            params.append(content)
            # 更新 FTS 表
            cursor.execute("UPDATE memories_fts SET content = ? WHERE id = ?", (content, id))
        
        if metadata:
            updates.append("metadata = ?")
            params.append(json.dumps(metadata))
        
        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        
        params.append(id)
        
        cursor.execute(f"UPDATE memories SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        
        return success

    def delete(self, id: str) -> bool:
        """删除记忆"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM memories WHERE id = ?", (id,))
        cursor.execute("DELETE FROM memories_fts WHERE id = ?", (id,))
        
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        
        return success

    def cleanup_old(self, days: int = 90) -> int:
        """清理过期记忆
        
        Args:
            days: 保留最近多少天的记忆
            
        Returns:
            删除的记忆条数
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        from datetime import timedelta
        cutoff = cutoff - timedelta(days=days)
        
        # 只删除非重要记忆
        cursor.execute("""
            DELETE FROM memories 
            WHERE created_at < ? 
            AND type NOT IN ('preference', 'context')
            AND id NOT IN (
                SELECT id FROM memories WHERE metadata LIKE '%"important":true%'
            )
        """, (cutoff.isoformat(),))
        
        count = cursor.rowcount
        
        # 重建 FTS
        cursor.execute("INSERT INTO memories_fts(memories_fts) VALUES('optimize')")
        
        conn.commit()
        conn.close()
        
        self.logger.info("MemoryDB", f"清理过期记忆: 删除 {count} 条")
        return count

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # 总数
        cursor.execute("SELECT COUNT(*) FROM memories")
        total = cursor.fetchone()[0]
        
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
            "by_type": by_type,
            "with_embedding": with_embedding,
            "db_size_bytes": db_size,
            "db_size_mb": round(db_size / 1024 / 1024, 2),
        }


# ---- 全局单例 ----
_memory_db: Optional[MemoryDB] = None


def get_memory_db() -> MemoryDB:
    """获取 MemoryDB 全局实例"""
    global _memory_db
    if _memory_db is None:
        _memory_db = MemoryDB()
    return _memory_db


def reset_memory_db() -> None:
    """重置 MemoryDB(用于测试)"""
    global _memory_db
    _memory_db = None
