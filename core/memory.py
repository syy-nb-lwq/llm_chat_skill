"""MemoryStore —— 长期记忆·**经验**存储（SQLite，跨会话）。

## 这个库存什么

"什么做法有效 / 什么做法会失败"：

- 失败诊断（`kind='failure'`）—— critic 每轮评估后写入
- 成功模式（`kind='success'`）
- 待审补丁（`kind='patch'`）—— 自演化产出的技能改进，带审批状态

## 它不存什么

**不存画像**：偏好 / 习惯 / 方法 / 雷区 / 事实在
`core/memory_db.py::MemoryDB`，由 `semantic_memory` 管理。

两库职责互斥、载体相同（都是 SQLite），但表不同、写入方不同：
画像要"语义检索 + 去重合并"，经验要"按时间倒序 + 人可读原文"。

## 为什么从 JSON 搬到 SQLite（`docs/20`）

原来是 `memory/failures/*.json` 之类的目录树。问题不在"人读不方便"，
而在**没法跨库查**：想知道"这个用户的雷区里，哪些已经踩过两次以上"，
得把画像查一遍、再遍历一遍 JSON 目录，在 Python 里做 join。
备份也要维护两套口径。

搬到 SQLite 后：

- 一张表，`kind` 区分三类经验，`payload` 列保完整原文；
- 常用字段（`scenario` / `skill` / `status` / `success_rate`）提成列，
  过滤和排序走 SQL 而不是 Python 侧全量读；
- 画像与经验在同一库文件的不同表里，一条 SQL 就能关联；
- 人要读原文时用 `export_json()` 单向导出，不再作为主存储。

历史 JSON 用 `import_json()` 一次性导入（幂等、可按 batch 回滚）。
"""
import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from core import sqlite_util
from core.text_match import query_fragments as _query_fragments
from core.text_match import score_fragments as _score_fragments
from infra.config import get_self_evolution_enabled
from infra.logger import get_logger
from core.exception_policy import downgrade

#: hints 与当前输入的相关度门槛(命中查询片段的 20%,与检索通道保持一致)
_HINT_MIN_SCORE = 0.2

#: 三类经验的 `kind` 取值
KIND_FAILURE = "failure"
KIND_SUCCESS = "success"
KIND_PATCH = "patch"
_ALL_KINDS = (KIND_FAILURE, KIND_SUCCESS, KIND_PATCH)

# ---- 数据结构 ----


@dataclass
class FailureRecord:
    """失败记录"""
    id: str
    trace_id: str
    timestamp: str
    scenario: str
    intent: str
    selected_skill: str
    success_rate: float
    fallback_count: int
    latency_ms: float
    diagnosis: str
    suggestion: Optional[Dict] = None
    user_corrected: bool = False


@dataclass
class SuccessRecord:
    """成功记录"""
    id: str
    trace_id: str
    timestamp: str
    scenario: str
    matched_skill: str
    latency_ms: float
    pattern: str = ""


@dataclass
class SkillPatch:
    """待审阅的技能改进建议"""
    id: str
    trace_id: str
    timestamp: str
    target_skill: str
    patch_type: str  # "improve_skill" / "new_skill" / "fix_method"
    diagnosis: str
    suggestion: Dict
    confidence: float = 0.5  # 0.0 ~ 1.0
    status: str = "pending"  # pending / approved / rejected / auto_approved
    reviewed_by: Optional[str] = None
    # M3-05:发布前校验结果与风险等级(给前端展示)
    validation: Optional[Dict] = None
    risk_level: str = "unknown"  # low / medium / high / unknown
    # M3-06:自动发布被拦下的原因
    blocked_reason: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS experience (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    payload        TEXT NOT NULL,
    trace_id       TEXT NOT NULL DEFAULT '',
    scenario       TEXT NOT NULL DEFAULT '',
    intent         TEXT NOT NULL DEFAULT '',
    skill          TEXT NOT NULL DEFAULT '',
    success_rate   REAL,
    latency_ms     REAL,
    user_corrected INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT '',
    confidence     REAL,
    created_at     TEXT NOT NULL,
    decided_at     TEXT NOT NULL DEFAULT '',
    decided_by     TEXT NOT NULL DEFAULT '',
    batch_id       TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL DEFAULT '',
    source_path    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_experience_kind    ON experience(kind);
CREATE INDEX IF NOT EXISTS idx_experience_created ON experience(kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_experience_status  ON experience(kind, status);
CREATE INDEX IF NOT EXISTS idx_experience_trace   ON experience(trace_id);
CREATE INDEX IF NOT EXISTS idx_experience_hash    ON experience(content_hash);
CREATE INDEX IF NOT EXISTS idx_experience_batch   ON experience(batch_id);
"""


def content_hash(payload: Dict[str, Any]) -> str:
    """规范化 JSON 的 hash —— 导入幂等用。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class MemoryStore:
    """经验存储（SQLite）。

    表 `experience` 一行一条记录：

    - `payload` 是**完整原文**（事实源，字段增删不需要改表）；
    - 其余列是 `payload` 里被频繁过滤/排序的字段的冗余副本，
      写的时候一起写，读的时候以 `payload` 为准。

    刻意不做 `content_hash` 的唯一约束：运行期同一段诊断文本在不同轮次
    出现是正常的（trace_id 不同），用唯一约束会把第二条静默丢掉。
    幂等只在 `import_json()` 里显式判断。
    """

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory"
        self.base = Path(base_path)
        #: 与画像同库不同表 —— 这样"画像 ⋈ 经验"能一条 SQL 查完，
        #: 备份也只有一个文件（见 docs/20 的动机）。
        self.db_path = self.base / "semantic_memory.db"
        self.audit_dir = self.base / "audit"
        self.logger = get_logger()
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._ensure_dirs()
        self._ensure_schema()

    # ---- 底层 ----

    def _ensure_dirs(self) -> None:
        """审计目录仍走文件系统（append-only 留痕，不属于经验）。"""
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            downgrade(exc, where='core/memory.py')

    def _connect(self) -> sqlite3.Connection:
        return sqlite_util.connect(self.db_path)

    def _ensure_schema(self) -> None:
        with self._schema_lock:
            if self._schema_ready:
                return
            conn = self._connect()
            try:
                with conn:
                    conn.executescript(_SCHEMA)
                self._schema_ready = True
            finally:
                conn.close()

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """执行一条写语句，返回受影响行数。

        遇到"库被删 / 表不存在"重建一次再重试。

        实机踩过：跑测试时 `rm -rf memory/`，而后端还在跑 —— 单例早就建好，
        手里攥着失效的 db 路径。**目录没了应该被当成"没有记录"，而不是崩**。
        所以每次写之前确认目录、表在；真丢了就重建（数据自然丢失，
        但外部把库删了本来也不该指望还有数据）。
        """
        try:
            self._ensure_schema()
            return self._execute_once(sql, params)
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            self._schema_ready = False
            self._ensure_schema()
            return self._execute_once(sql, params)
        except Exception as exc:
            downgrade(exc, where='core/memory.py')
            self._schema_ready = False
            self._ensure_schema()
            return self._execute_once(sql, params)

    def _execute_once(self, sql: str, params: Iterable[Any]) -> int:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(sql, tuple(params))
                return cur.rowcount or 0
        finally:
            conn.close()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> List[Tuple[Any, ...]]:
        try:
            self._ensure_schema()
            conn = self._connect()
            try:
                return list(conn.execute(sql, tuple(params)).fetchall())
            finally:
                conn.close()
        except Exception as exc:
            downgrade(exc, where='core/memory.py')
            self._schema_ready = False
            try:
                self._ensure_schema()
                conn = self._connect()
                try:
                    return list(conn.execute(sql, tuple(params)).fetchall())
                finally:
                    conn.close()
            except Exception as exc2:
                downgrade(exc2, where='core/memory.py')
                return []

    def _insert(self, row: Dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
        self._execute(
            f"INSERT INTO experience ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            row.values(),
        )

    def _load(self, kind: str, payload: str) -> Optional[Any]:
        """payload JSON → dataclass（坏数据当没有，不抛）。"""
        try:
            data = json.loads(payload)
        except Exception as exc:
            downgrade(exc, where='core/memory.py')
            return None
        try:
            if kind == KIND_FAILURE:
                keys = set(FailureRecord.__dataclass_fields__)
                return FailureRecord(**{k: v for k, v in data.items() if k in keys})
            if kind == KIND_SUCCESS:
                keys = set(SuccessRecord.__dataclass_fields__)
                return SuccessRecord(**{k: v for k, v in data.items() if k in keys})
            if kind == KIND_PATCH:
                keys = set(SkillPatch.__dataclass_fields__)
                return SkillPatch(**{k: v for k, v in data.items() if k in keys})
        except Exception as exc:
            downgrade(exc, where='core/memory.py')
        return None

    # ---- 审计(M3-06) ----

    def write_audit(self, kind: str, payload: Dict) -> str:
        """写一条审计记录,返回文件路径。

        自动发布 / 人工发布都必须留痕,便于事后追溯"谁在什么时候改了哪个技能"。
        审计是 append-only 的操作流水，不是"经验"，仍落文件。
        """
        record = {
            "kind": kind,
            "timestamp": datetime.now().isoformat(),
            **payload,
        }
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.audit_dir / f"{kind}_{stamp}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
        except Exception as exc:
            self.logger.error("MemoryStore", f"写审计失败: {exc}")
            return ""
        return str(path)

    # ---- 记录 ----

    def record_failure(
        self,
        trace_id: str,
        scenario: str,
        intent: str,
        selected_skill: str,
        success_rate: float,
        fallback_count: int,
        latency_ms: float,
        diagnosis: str,
        suggestion: Optional[Dict] = None,
        user_corrected: bool = False,
        *,
        execution_id: Optional[str] = None,
        session_id: str = "default",
        turn_id: str = "",
    ) -> FailureRecord:
        """记录一次失败分析。

        id 默认用 ``execution_id``(每次 handle 调用唯一)，以避免同 session
        多轮失败互相覆盖。``trace_id`` 仍保留在内容里以兼容旧的查询代码。
        """
        eid = execution_id or trace_id
        record = FailureRecord(
            id=f"fail_{eid}",
            trace_id=trace_id,
            timestamp=datetime.now().isoformat(),
            scenario=scenario,
            intent=intent,
            selected_skill=selected_skill,
            success_rate=success_rate,
            fallback_count=fallback_count,
            latency_ms=latency_ms,
            diagnosis=diagnosis,
            suggestion=suggestion,
            user_corrected=user_corrected,
        )

        payload = asdict(record)
        payload.update({
            "execution_id": eid,
            "session_id": session_id,
            "turn_id": turn_id,
        })

        self._insert({
            "id": record.id,
            "kind": KIND_FAILURE,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "trace_id": trace_id,
            "scenario": scenario or "",
            "intent": intent or "",
            "skill": selected_skill or "",
            "success_rate": float(success_rate),
            "latency_ms": float(latency_ms),
            "user_corrected": 1 if user_corrected else 0,
            "status": "",
            "confidence": None,
            "created_at": record.timestamp,
            "batch_id": "",
            "content_hash": content_hash(payload),
            "source_path": "",
        })

        self.logger.info("MemoryStore", f"记录失败: {eid}, rate={success_rate:.2f}")
        return record

    def record_success(
        self,
        trace_id: str,
        scenario: str,
        matched_skill: str,
        latency_ms: float,
        pattern: str = "",
    ) -> SuccessRecord:
        """记录一次成功路径"""
        record = SuccessRecord(
            id=f"succ_{trace_id}",
            trace_id=trace_id,
            timestamp=datetime.now().isoformat(),
            scenario=scenario,
            matched_skill=matched_skill,
            latency_ms=latency_ms,
            pattern=pattern,
        )
        payload = asdict(record)
        self._insert({
            "id": record.id,
            "kind": KIND_SUCCESS,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "trace_id": trace_id,
            "scenario": scenario or "",
            "intent": "",
            "skill": matched_skill or "",
            "success_rate": None,
            "latency_ms": float(latency_ms),
            "user_corrected": 0,
            "status": "",
            "confidence": None,
            "created_at": record.timestamp,
            "batch_id": "",
            "content_hash": content_hash(payload),
            "source_path": "",
        })
        self.logger.info("MemoryStore", f"记录成功: {trace_id}, skill={matched_skill}")
        return record

    def add_pending_patch(self, patch: SkillPatch) -> None:
        """添加待审阅的技能改进建议"""
        payload = asdict(patch)
        self._insert({
            "id": patch.id,
            "kind": KIND_PATCH,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "trace_id": patch.trace_id or "",
            "scenario": "",
            "intent": "",
            "skill": patch.target_skill or "",
            "success_rate": None,
            "latency_ms": None,
            "user_corrected": 0,
            "status": patch.status or "pending",
            "confidence": float(patch.confidence),
            "created_at": patch.timestamp,
            "batch_id": "",
            "content_hash": content_hash(payload),
            "source_path": "",
        })
        self.logger.info(
            "MemoryStore", f"添加 SkillPatch: {patch.id}, confidence={patch.confidence:.2f}"
        )

    # ---- 查询 ----

    def get_recent_failures(
        self,
        scenario: Optional[str] = None,
        top_k: int = 5,
    ) -> List[FailureRecord]:
        """获取最近的失败记录,按时间倒序。"""
        if scenario:
            rows = self._query(
                "SELECT payload FROM experience WHERE kind = ? AND scenario LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (KIND_FAILURE, f"%{scenario}%", top_k),
            )
        else:
            rows = self._query(
                "SELECT payload FROM experience WHERE kind = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (KIND_FAILURE, top_k),
            )
        records: List[FailureRecord] = []
        for (payload,) in rows:
            rec = self._load(KIND_FAILURE, payload)
            if rec is not None:
                records.append(rec)
        return records

    def get_recent_successes(self, top_k: int = 100) -> List[SuccessRecord]:
        """最近的若干条成功记录，按时间倒序。"""
        rows = self._query(
            "SELECT payload FROM experience WHERE kind = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (KIND_SUCCESS, top_k),
        )
        records: List[SuccessRecord] = []
        for (payload,) in rows:
            rec = self._load(KIND_SUCCESS, payload)
            if rec is not None:
                records.append(rec)
        return records

    def get_skill_hints(self, user_input: str) -> List[str]:
        """获取与当前输入相关的历史教训提示

        策略:
        1. 从最近失败中提取 diagnosis
        2. 从成功中提取匹配 pattern
        """
        if not get_self_evolution_enabled():
            return []

        # 中文没有空格,`diagnosis.split()` 会得到整句,永远匹配不上 ——
        # 统一用 CJK 二元组打分(见 core/text_match)。
        fragments = _query_fragments(user_input)
        if not fragments:
            return []

        hints: List[str] = []
        seen: set = set()

        # 从最近失败中找相关
        failures = self.get_recent_failures(top_k=10)
        for f in failures:
            if not f.diagnosis or f.diagnosis in seen:
                continue
            haystack = " ".join(
                part for part in (f.diagnosis, f.scenario, f.intent) if part
            )
            if _score_fragments(haystack, fragments) >= _HINT_MIN_SCORE:
                seen.add(f.diagnosis)
                hints.append(f"[失败教训] {f.diagnosis}")

        # 从成功中找相关
        hints.extend(self._get_success_hints(user_input, top_k=3))

        return hints[:5]  # 最多 5 条

    def _get_success_hints(self, user_input: str, top_k: int = 3) -> List[str]:
        """从成功记录中提取提示"""
        fragments = _query_fragments(user_input)
        if not fragments:
            return []

        hints: List[str] = []
        for record in self.get_recent_successes(top_k=100):
            haystack = " ".join(
                part
                for part in (record.scenario, record.matched_skill, record.pattern)
                if part
            )
            if _score_fragments(haystack, fragments) >= _HINT_MIN_SCORE:
                hints.append(
                    f"[成功经验] {record.matched_skill} 适合处理 {record.scenario}"
                )
            if len(hints) >= top_k:
                break
        return hints

    def get_pending_patches(self) -> List[SkillPatch]:
        """获取所有待审阅的改进建议（按 confidence 降序）。"""
        rows = self._query(
            "SELECT payload FROM experience WHERE kind = ? AND status = 'pending' "
            "ORDER BY confidence DESC",
            (KIND_PATCH,),
        )
        patches: List[SkillPatch] = []
        for (payload,) in rows:
            rec = self._load(KIND_PATCH, payload)
            if rec is not None:
                patches.append(rec)
        return patches

    def get_patch(self, patch_id: str) -> Optional[SkillPatch]:
        """按 id 取一条补丁（含非 pending 的）。"""
        rows = self._query(
            "SELECT payload FROM experience WHERE kind = ? AND id = ?",
            (KIND_PATCH, patch_id),
        )
        if not rows:
            return None
        return self._load(KIND_PATCH, rows[0][0])

    def _set_patch_status(self, patch_id: str, status: str, reviewer: str) -> bool:
        patch = self.get_patch(patch_id)
        if patch is None:
            return False
        patch.status = status
        patch.reviewed_by = reviewer
        payload = asdict(patch)
        payload["reviewed_at"] = datetime.now().isoformat()
        self._insert({
            "id": patch.id,
            "kind": KIND_PATCH,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "trace_id": patch.trace_id or "",
            "scenario": "",
            "intent": "",
            "skill": patch.target_skill or "",
            "success_rate": None,
            "latency_ms": None,
            "user_corrected": 0,
            "status": status,
            "confidence": float(patch.confidence),
            "created_at": patch.timestamp,
            "decided_at": payload["reviewed_at"],
            "decided_by": reviewer,
            "batch_id": "",
            "content_hash": content_hash(payload),
            "source_path": "",
        })
        return True

    def approve_patch(self, patch_id: str, reviewer: str = "human") -> bool:
        """审核通过一个 SkillPatch"""
        ok = self._set_patch_status(patch_id, "approved", reviewer)
        if ok:
            self.logger.info("MemoryStore", f"批准 SkillPatch: {patch_id}")
        return ok

    def reject_patch(self, patch_id: str, reviewer: str = "human") -> bool:
        """拒绝一个 SkillPatch"""
        ok = self._set_patch_status(patch_id, "rejected", reviewer)
        if ok:
            self.logger.info("MemoryStore", f"拒绝 SkillPatch: {patch_id}")
        return ok

    # ---- 容量控制 ----

    def enforce_capacity(self, max_per_month: int = 50, keep_ratio: float = 0.8):
        """每月超过 max_per_month 条时,保留 success_rate 最高的 keep_ratio 部分"""
        rows = self._query(
            "SELECT substr(created_at, 1, 7) AS m, count(*) FROM experience "
            "WHERE kind = ? GROUP BY m",
            (KIND_FAILURE,),
        )
        for month, count in rows:
            if count <= max_per_month:
                continue
            keep = int(count * keep_ratio)
            # 先按 success_rate 降序找出要保留的 id，其余删掉。
            keep_rows = self._query(
                "SELECT id FROM experience WHERE kind = ? "
                "AND substr(created_at, 1, 7) = ? "
                "ORDER BY success_rate DESC LIMIT ?",
                (KIND_FAILURE, month, keep),
            )
            keep_ids = {r[0] for r in keep_rows}
            victims = self._query(
                "SELECT id FROM experience WHERE kind = ? "
                "AND substr(created_at, 1, 7) = ?",
                (KIND_FAILURE, month),
            )
            for (vid,) in victims:
                if vid in keep_ids:
                    continue
                self._execute("DELETE FROM experience WHERE id = ?", (vid,))
                self.logger.info("MemoryStore", f"容量清理删除: {vid}")

    # ---- 统计 ----

    def remove_success(self, trace_id: str) -> bool:
        """撤销一条成功记录。

        **为什么需要**:critic 先按"工具成功"记了成功,下一轮用户说不对,
        我们会用纠正信息重评这一轮 —— 这时必须把之前那条成功记录撤掉,
        否则同一轮既算成功又算失败,采纳率就失真了。
        """
        removed = self._execute(
            "DELETE FROM experience WHERE kind = ? AND trace_id = ?",
            (KIND_SUCCESS, trace_id),
        )
        if removed:
            self.logger.info("MemoryStore", f"撤销成功记录: {trace_id}")
        return removed > 0

    def get_adoption_rate(self) -> Dict[str, Any]:
        """答案采纳率 —— **自演化最核心的质量指标**。

        采纳率 = 1 − (被用户纠正的轮数 / 有评估记录的轮数)

        旧指标(success_rate)只看工具调没调通,一个"工具全通但答案全错"
        的系统能拿 100%。采纳率不会。
        """
        rows = self._query(
            "SELECT kind, count(*) FROM experience WHERE kind IN (?, ?) GROUP BY kind",
            (KIND_SUCCESS, KIND_FAILURE),
        )
        counts = {kind: n for kind, n in rows}
        successes = counts.get(KIND_SUCCESS, 0)
        failures = counts.get(KIND_FAILURE, 0)

        corrected_rows = self._query(
            "SELECT count(*) FROM experience WHERE kind = ? AND user_corrected = 1",
            (KIND_FAILURE,),
        )
        corrected = corrected_rows[0][0] if corrected_rows else 0

        evaluated = successes + failures
        rate = (1.0 - corrected / evaluated) if evaluated else 0.0
        return {
            "evaluated": evaluated,
            "successes": successes,
            "failures": failures,
            "corrected": corrected,
            "adoption_rate": round(rate, 4),
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计"""
        failures_rows = self._query(
            "SELECT count(*) FROM experience WHERE kind = ?", (KIND_FAILURE,)
        )
        successes_rows = self._query(
            "SELECT count(*) FROM experience WHERE kind = ?", (KIND_SUCCESS,)
        )
        by_month_rows = self._query(
            "SELECT substr(created_at, 1, 7) AS m, count(*) FROM experience "
            "WHERE kind = ? GROUP BY m ORDER BY m",
            (KIND_FAILURE,),
        )

        stats = {
            "total_failures": failures_rows[0][0] if failures_rows else 0,
            "total_successes": successes_rows[0][0] if successes_rows else 0,
            "pending_patches": len(self.get_pending_patches()),
            "recent_failures_by_month": {m: n for m, n in by_month_rows},
        }
        stats.update(self.get_adoption_rate())
        return stats

    # ---- 导入 / 导出（JSON 只作单向出口） ----

    def export_json(self, dest: Optional[Path] = None) -> Dict[str, int]:
        """把三类经验导出成 JSON 目录树（人工审阅 / 备份用）。

        这是**单向出口**：导出后不再回写，也不参与主链读写。
        """
        dest = Path(dest) if dest else (self.base / "export")
        counts = {kind: 0 for kind in _ALL_KINDS}
        for kind in _ALL_KINDS:
            (dest / kind).mkdir(parents=True, exist_ok=True)
        rows = self._query("SELECT id, kind, payload FROM experience")
        for rid, kind, payload in rows:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(rid))
            try:
                (dest / kind / f"{safe}.json").write_text(payload, encoding="utf-8")
                counts[kind] = counts.get(kind, 0) + 1
            except Exception as exc:
                downgrade(exc, where='core/memory.py')
        return counts

    def import_json(
        self,
        source: Optional[Path] = None,
        *,
        batch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把历史 JSON 目录树导入本表（幂等，可按 batch 回滚）。

        幂等判断两条：id 已存在，或同样内容已存在（`content_hash`）。
        重复执行不会重复插入，也不会覆盖运行期新写入的同 id 记录。
        """
        source = Path(source) if source else self.base
        batch = batch_id or f"import-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        report: Dict[str, Any] = {
            "batch_id": batch,
            "scanned": 0,
            "inserted": 0,
            "skipped": 0,
            "by_kind": {kind: 0 for kind in _ALL_KINDS},
        }

        legacy = {
            KIND_FAILURE: source / "failures",
            KIND_SUCCESS: source / "successes",
            KIND_PATCH: source / "skill_patches" / "pending",
        }

        for kind, directory in legacy.items():
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*.json")) + sorted(directory.glob("*.jsonl")):
                for payload in self._read_legacy_file(path, kind):
                    report["scanned"] += 1
                    digest = content_hash(payload)
                    rid = str(payload.get("id") or f"exp-{digest}")
                    exists = self._query(
                        "SELECT 1 FROM experience WHERE id = ? OR content_hash = ? LIMIT 1",
                        (rid, digest),
                    )
                    if exists:
                        report["skipped"] += 1
                        continue
                    self._insert(self._row_from_payload(kind, rid, payload, batch, str(path)))
                    report["inserted"] += 1
                    report["by_kind"][kind] += 1
        return report

    def _read_legacy_file(self, path: Path, kind: str) -> List[Dict[str, Any]]:
        """读一个历史文件；`.jsonl` 按行拆。坏文件当没有。"""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            downgrade(exc, where='core/memory.py')
            return []
        if path.suffix == ".jsonl":
            out: List[Dict[str, Any]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    out.append(row)
            return out
        try:
            data = json.loads(text)
        except Exception as exc:
            downgrade(exc, where='core/memory.py')
            return []
        return [data] if isinstance(data, dict) else []

    def _row_from_payload(
        self,
        kind: str,
        rid: str,
        payload: Dict[str, Any],
        batch: str,
        source_path: str,
    ) -> Dict[str, Any]:
        """历史 payload → 表行（缺失字段给安全默认值）。"""
        return {
            "id": rid,
            "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
            "trace_id": str(payload.get("trace_id") or ""),
            "scenario": str(payload.get("scenario") or ""),
            "intent": str(payload.get("intent") or ""),
            "skill": str(
                payload.get("selected_skill")
                or payload.get("matched_skill")
                or payload.get("target_skill")
                or ""
            ),
            "success_rate": _maybe_float(payload.get("success_rate")),
            "latency_ms": _maybe_float(payload.get("latency_ms")),
            "user_corrected": 1 if payload.get("user_corrected") else 0,
            "status": str(payload.get("status") or ""),
            "confidence": _maybe_float(payload.get("confidence")),
            "created_at": str(payload.get("timestamp") or datetime.now().isoformat()),
            "decided_at": "",
            "decided_by": str(payload.get("reviewed_by") or ""),
            "batch_id": batch,
            "content_hash": content_hash(payload),
            "source_path": source_path,
        }

    def rollback_batch(self, batch_id: str) -> int:
        """删除某个导入批次写入的记录。"""
        return self._execute("DELETE FROM experience WHERE batch_id = ?", (batch_id,))


def _maybe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---- 全局单例 ----
_memory_store: Optional[MemoryStore] = None
_store_lock = threading.Lock()


def get_memory_store() -> MemoryStore:
    """获取 MemoryStore 全局实例（双检锁，防多 worker 双实例双写）"""
    global _memory_store
    if _memory_store is None:
        with _store_lock:
            if _memory_store is None:
                _memory_store = MemoryStore()
    return _memory_store


def reset_memory_store() -> None:
    """重置单例（测试用）"""
    global _memory_store
    with _store_lock:
        _memory_store = None
