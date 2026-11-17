"""SessionMemoryStore —— 会话内容持久化(短期记忆)。

设计原则(见 `docs/13-记忆分层与RAG方案.md` §2.7):
  - **会话内容全部保留**:不设过期、不删除,用户随时可以回来接着问答
  - **存储 ≠ 注入**:存储全留;注入 LLM 时才按 token 预算裁剪成
    「滚动摘要 + 最近 N 轮」

与长期记忆(`core/memory_db.py`)物理隔离,各自一个 SQLite 文件:
  - `memory/sessions.db` —— 会话内容(短期记忆)
  - `memory/semantic_memory.db` —— 长期记忆 / 用户画像

检索说明:
  SQLite FTS5 的 unicode61 分词器把整句中文当作一个 token,
  trigram 又要求查询串 ≥3 字符 —— 都不适合中文短词检索("北京"、"天气")。
  因此会话内检索走 **CJK 二元组打分**:把查询切成 ASCII 词 + CJK 二元组,
  按命中比例打分排序。规模到十万级再换 sqlite-vec / 专用分词器。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger

from core import sqlite_util
from core.text_match import query_fragments as _query_fragments
from core.text_match import score_fragments as _score_fragments


DEFAULT_TENANT = "default"

# 中文检索片段:CJK 连续段切二元组
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z0-9_]+")


@dataclass
class SessionMessage:
    """一条会话消息(已落库)。"""

    id: str
    session_id: str
    user_id: str
    seq: int
    turn_id: str
    role: str
    content: str
    created_at: str = ""

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "seq": self.seq,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass
class SessionSummary:
    """一条滚动摘要(覆盖 session 的前 cover_until_seq 条消息)。"""

    session_id: str
    user_id: str
    cover_until_seq: int
    summary: str
    created_at: str = ""
    id: str = field(default="")

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "cover_until_seq": self.cover_until_seq,
            "summary": self.summary,
            "created_at": self.created_at,
        }


def _now() -> str:
    return datetime.now().isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def query_fragments(query: str) -> List[str]:
    """把查询切成检索片段(实现见 `core.text_match`)。"""
    return _query_fragments(query)


def score_fragments(content: str, fragments: List[str]) -> float:
    """命中比例(0~1)(实现见 `core.text_match`)。"""
    return _score_fragments(content, fragments)


class SessionMemoryStore:
    """会话内容存储(SQLite)。"""

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = Path(__file__).parent.parent / "memory" / "sessions.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()
        self._init_db()

    # ===== schema =====

    def _connect(self) -> sqlite3.Connection:
        """打开一个配好并发参数的连接(WAL + busy_timeout)。

        统一走 `core.sqlite_util.connect` —— 会话写入是高频路径,
        多用户并发时裸 `sqlite3.connect` 会 `database is locked`。

        **自愈**:目录只在 `__init__` 建一次。如果运行期间被外部删掉
        (日志清理/部署脚本/手动清理),这里要重新建 —— 否则每次读会话
        都抛 `unable to open database file`,接口直接 500。

        实机踩过:删了 memory/ 之后刷新页面,`GET /api/session/messages`
        就是 500(而不是"没有历史")。
        """
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.logger.warning("SessionMemory", f"建目录失败 {self.db_path.parent}: {exc}")

        # 文件也可能被删了(不只是目录)。
        # 新建的空库里没有表,之后每次读写都会
        # `no such table: sessions` —— 同样表现为 500。
        # 所以此时补一次建表。
        need_init = not self.db_path.exists()
        conn = sqlite_util.connect(self.db_path, row_factory=sqlite3.Row)
        if need_init:
            try:
                self._init_db(conn)
            except Exception as exc:
                self.logger.warning("SessionMemory", f"重建表失败: {exc}")
        return conn

    def _init_db(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """建表。传入连接时复用它(避免 `_connect` 递归)。"""
        own = conn is None
        if own:
            conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id    TEXT PRIMARY KEY,
                tenant_id     TEXT NOT NULL DEFAULT 'default',
                user_id       TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                reset_at_seq  INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_messages (
                id         TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tenant_id  TEXT NOT NULL DEFAULT 'default',
                user_id    TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                turn_id    TEXT NOT NULL DEFAULT '',
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sess_msg "
            "ON session_messages(session_id, seq)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sess_msg_user "
            "ON session_messages(user_id, created_at)"
        )
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS session_summaries (
                id               TEXT PRIMARY KEY,
                session_id       TEXT NOT NULL,
                tenant_id        TEXT NOT NULL DEFAULT 'default',
                user_id          TEXT NOT NULL,
                cover_until_seq  INTEGER NOT NULL,
                summary          TEXT NOT NULL,
                created_at       TEXT NOT NULL
            )
        """)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sess_summary "
            "ON session_summaries(session_id, cover_until_seq)"
        )
        conn.commit()
        if own:
            conn.close()

    # ===== 会话 =====

    def ensure_session(
        self,
        session_id: str,
        user_id: str,
        tenant_id: str = DEFAULT_TENANT,
    ) -> None:
        """登记会话(幂等)。"""
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO sessions
                    (session_id, tenant_id, user_id, message_count, created_at, updated_at)
                VALUES (?, ?, ?, 0, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, tenant_id, user_id, _now(), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def session_exists(self, session_id: str, user_id: Optional[str] = None) -> bool:
        """会话是否存在。带 user_id 时同时校验归属 —— 防止越权恢复他人会话。"""
        if not session_id:
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            if user_id:
                row2 = conn.execute(
                    "SELECT session_id FROM sessions WHERE session_id = ? AND user_id = ?",
                    (session_id, user_id),
                ).fetchone()
                return row2 is not None
            return True
        finally:
            conn.close()

    def list_sessions(self, user_id: Optional[str] = None) -> List[Dict]:
        conn = self._connect()
        try:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def mark_reset(self, session_id: str) -> int:
        """标记"工作上下文已清空"。

        **不删除任何消息** —— 会话内容始终保留、可检索。
        只是让恢复会话时不把 reset 之前的内容重新装回工作上下文。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT message_count FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return 0
            seq = int(row["message_count"])
            conn.execute(
                "UPDATE sessions SET reset_at_seq = ?, updated_at = ? WHERE session_id = ?",
                (seq, _now(), session_id),
            )
            conn.commit()
            return seq
        finally:
            conn.close()

    def get_reset_at_seq(self, session_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT reset_at_seq FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["reset_at_seq"]) if row else 0
        finally:
            conn.close()

    def delete_session(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM session_messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ?", (session_id,)
            )
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ===== 消息 =====

    def append_message(
        self,
        session_id: str,
        user_id: str,
        role: str,
        content: str,
        turn_id: str = "",
        tenant_id: str = DEFAULT_TENANT,
    ) -> int:
        """追加一条消息,返回分配的 ``seq``(会话内绝对序号)。

        ``seq`` 是会话内单调递增序号,用于摘要覆盖范围与稳定排序。
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT message_count FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sessions
                        (session_id, tenant_id, user_id, message_count, created_at, updated_at)
                    VALUES (?, ?, ?, 0, ?, ?)
                    """,
                    (session_id, tenant_id, user_id, _now(), _now()),
                )
                seq = 0
            else:
                seq = int(row["message_count"])
            seq += 1
            msg_id = _new_id()
            conn.execute(
                """
                INSERT INTO session_messages
                    (id, session_id, tenant_id, user_id, seq, turn_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id, session_id, tenant_id, user_id, seq, turn_id,
                    role, content or "", _now(),
                ),
            )
            conn.execute(
                "UPDATE sessions SET message_count = ?, updated_at = ? WHERE session_id = ?",
                (seq, _now(), session_id),
            )
            conn.commit()
            return seq
        finally:
            conn.close()

    def get_messages(
        self,
        session_id: str,
        limit: int = 50,
        user_id: Optional[str] = None,
        from_seq: int = 0,
    ) -> List[SessionMessage]:
        """取消息(按 seq 升序)。

        - ``limit`` <= 0 表示不限
        - ``from_seq``: 只取 seq > from_seq 的(用于跳过已被摘要覆盖的部分)
        """
        conn = self._connect()
        try:
            sql = "SELECT * FROM session_messages WHERE session_id = ? AND seq > ?"
            params: List[Any] = [session_id, from_seq]
            if user_id:
                sql += " AND user_id = ?"
                params.append(user_id)
            sql += " ORDER BY seq ASC"
            if limit and limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [
                SessionMessage(
                    id=r["id"],
                    session_id=r["session_id"],
                    user_id=r["user_id"],
                    seq=r["seq"],
                    turn_id=r["turn_id"],
                    role=r["role"],
                    content=r["content"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_recent_messages(
        self,
        session_id: str,
        limit: int = 50,
        user_id: Optional[str] = None,
    ) -> List[SessionMessage]:
        """取最近 N 条(按 seq 升序返回)。"""
        conn = self._connect()
        try:
            sql = "SELECT * FROM session_messages WHERE session_id = ?"
            params: List[Any] = [session_id]
            if user_id:
                sql += " AND user_id = ?"
                params.append(user_id)
            sql += " ORDER BY seq DESC"
            if limit and limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            out = [
                SessionMessage(
                    id=r["id"],
                    session_id=r["session_id"],
                    user_id=r["user_id"],
                    seq=r["seq"],
                    turn_id=r["turn_id"],
                    role=r["role"],
                    content=r["content"],
                    created_at=r["created_at"],
                )
                for r in reversed(rows)
            ]
            return out
        finally:
            conn.close()

    def count_messages(self, session_id: str) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM session_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["c"])
        finally:
            conn.close()

    # ===== 摘要 =====

    def save_summary(
        self,
        session_id: str,
        user_id: str,
        cover_until_seq: int,
        summary: str,
        tenant_id: str = DEFAULT_TENANT,
    ) -> str:
        """保存一条滚动摘要(覆盖前 cover_until_seq 条消息)。"""
        sid = _new_id()
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO session_summaries
                    (id, session_id, tenant_id, user_id, cover_until_seq, summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sid, session_id, tenant_id, user_id, cover_until_seq, summary or "", _now()),
            )
            conn.commit()
            return sid
        finally:
            conn.close()

    def get_latest_summary(self, session_id: str) -> Optional[SessionSummary]:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM session_summaries WHERE session_id = ?
                ORDER BY cover_until_seq DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return SessionSummary(
                id=row["id"],
                session_id=row["session_id"],
                user_id=row["user_id"],
                cover_until_seq=row["cover_until_seq"],
                summary=row["summary"],
                created_at=row["created_at"],
            )
        finally:
            conn.close()

    # ===== 检索 =====

    def search(
        self,
        session_id: str,
        query: str,
        limit: int = 5,
        user_id: Optional[str] = None,
        min_score: float = 0.3,
    ) -> List[SessionMessage]:
        """会话内检索(按 CJK 二元组命中比例排序)。

        只在本会话内检索 —— 会话内容是短期记忆,不跨用户、不跨会话。
        """
        fragments = query_fragments(query)
        if not fragments:
            return []
        candidates = self.get_recent_messages(
            session_id, limit=0, user_id=user_id
        )
        scored = []
        for msg in candidates:
            s = score_fragments(msg.content, fragments)
            if s >= min_score:
                scored.append((s, msg))
        scored.sort(key=lambda pair: (pair[0], pair[1].seq), reverse=True)
        return [msg for _, msg in scored[:limit]]

    def get_stats(self) -> Dict[str, Any]:
        conn = self._connect()
        try:
            sessions = conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
            msgs = conn.execute("SELECT COUNT(*) AS c FROM session_messages").fetchone()["c"]
            summaries = conn.execute(
                "SELECT COUNT(*) AS c FROM session_summaries"
            ).fetchone()["c"]
            return {
                "sessions": sessions,
                "messages": msgs,
                "summaries": summaries,
                "db_path": str(self.db_path),
            }
        finally:
            conn.close()

    def clear_all(self) -> None:
        """清空所有会话数据(测试隔离用)。"""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM session_messages")
            conn.execute("DELETE FROM session_summaries")
            conn.execute("DELETE FROM sessions")
            conn.commit()
        finally:
            conn.close()


# ===== 单例 =====

_session_store: Optional[SessionMemoryStore] = None


def get_session_memory_store() -> SessionMemoryStore:
    global _session_store
    if _session_store is None:
        _session_store = SessionMemoryStore()
    return _session_store


def reset_session_memory_store() -> None:
    global _session_store
    _session_store = None
