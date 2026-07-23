"""MemoryStore - 跨 Session 长期记忆,让 Agent 记住历史失败和成功经验"""
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.config import get_self_evolution_enabled
from infra.logger import get_logger

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


class MemoryStore:
    """长期记忆存储

    目录结构:
        memory/
        ├── failures/
        │   └── YYYY-MM/
        │       └── {trace_id}.json
        ├── successes/
        │   └── success_index.jsonl
        └── skill_patches/
            └── pending/
                └── {id}.json

    容量控制:每月超过 50 条时,丢弃 success_rate 最低的 20%。
    """

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory"
        self.base = base_path
        self.failures_dir = self.base / "failures"
        self.successes_dir = self.base / "successes"
        self.patches_dir = self.base / "skill_patches" / "pending"
        self.logger = get_logger()

        self._ensure_dirs()

    def _ensure_dirs(self):
        for d in [self.failures_dir, self.successes_dir, self.patches_dir]:
            d.mkdir(parents=True, exist_ok=True)

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
        user_id: str = "default",
        session_id: str = "default",
        turn_id: str = "",
    ) -> FailureRecord:
        """记录一次失败分析。

        落盘文件名默认使用 ``execution_id``(每次 handle 调用唯一),
        以避免同 session 多轮失败互相覆盖。``trace_id`` 仍保留在内容里以兼容
        旧的查询代码。
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

        # 落盘 payload 附带身份字段(便于后续跨 user 隔离查询,M2 准备)
        payload = asdict(record)
        payload.update({
            "execution_id": eid,
            "user_id": user_id,
            "session_id": session_id,
            "turn_id": turn_id,
        })

        # 按月归档
        month_dir = self.failures_dir / datetime.now().strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)

        path = month_dir / f"{eid}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

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

        path = self.successes_dir / "success_index.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        self.logger.info("MemoryStore", f"记录成功: {trace_id}, skill={matched_skill}")
        return record

    def add_pending_patch(self, patch: SkillPatch) -> None:
        """添加待审阅的技能改进建议"""
        path = self.patches_dir / f"{patch.id}.json"
        path.write_text(json.dumps(asdict(patch), ensure_ascii=False), encoding="utf-8")
        self.logger.info("MemoryStore", f"添加 SkillPatch: {patch.id}, confidence={patch.confidence:.2f}")

    # ---- 查询 ----

    def get_recent_failures(
        self,
        scenario: Optional[str] = None,
        top_k: int = 5,
    ) -> List[FailureRecord]:
        """获取最近的失败记录,按时间倒序"""
        records: List[FailureRecord] = []
        for month_dir in sorted(self.failures_dir.iterdir(), reverse=True):
            if not month_dir.is_dir():
                continue
            for path in sorted(month_dir.glob("*.json"), reverse=True):
                # scenario 过滤通过读取文件内容判断,避免误杀
                if scenario:
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                        if scenario not in data.get("scenario", ""):
                            continue
                    except Exception:
                        continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                try:
                    # 过滤掉非 FailureRecord 字段(payload 中携带的 identity 信息)
                    valid_keys = {f.name for f in FailureRecord.__dataclass_fields__.values()}
                    payload = {k: v for k, v in data.items() if k in valid_keys}
                    records.append(FailureRecord(**payload))
                except Exception:
                    continue
                if len(records) >= top_k:
                    break
            if len(records) >= top_k:
                break
        return records[:top_k]

    def get_skill_hints(self, user_input: str) -> List[str]:
        """获取与当前输入相关的历史教训提示

        策略:
        1. 从最近失败中提取 diagnosis
        2. 从成功中提取匹配 pattern
        """
        if not get_self_evolution_enabled():
            return []
            
        hints: List[str] = []

        # 从最近失败中找相关
        failures = self.get_recent_failures(top_k=10)
        for f in failures:
            if not f.diagnosis:
                continue
            # 简单关键词匹配
            keywords = [w for w in f.diagnosis.split() if len(w) > 2]
            matches = sum(1 for kw in keywords if kw in user_input)
            if matches >= 1:
                hints.append(f"[失败教训] {f.diagnosis}")

        # 从成功中找相关
        success_hints = self._get_success_hints(user_input, top_k=3)
        hints.extend(success_hints)

        return hints[:5]  # 最多 5 条

    def _get_success_hints(self, user_input: str, top_k: int = 3) -> List[str]:
        """从成功记录中提取提示"""
        hints: List[str] = []
        path = self.successes_dir / "success_index.jsonl"
        if not path.exists():
            return hints

        try:
            with path.open(encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines[-100:]):  # 只看最近 100 条
                try:
                    data = json.loads(line)
                    record = SuccessRecord(**data)
                    # 关键词匹配
                    if any(kw in user_input for kw in [record.scenario, record.matched_skill]):
                        hints.append(f"[成功经验] {record.matched_skill} 适合处理 {record.scenario}")
                except Exception as e:
                    self.logger.debug("MemoryStore", f"跳过损坏的成功记录行: {e}")
                if len(hints) >= top_k:
                    break
        except Exception as e:
            self.logger.warning("MemoryStore", f"读取成功经验失败: {e}")

        return hints

    def get_pending_patches(self) -> List[SkillPatch]:
        """获取所有待审阅的改进建议。"""
        patches: List[SkillPatch] = []
        reviewable_statuses = {"pending", "auto_approved"}
        for path in self.patches_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") in reviewable_statuses:
                    patches.append(SkillPatch(**data))
            except Exception as e:
                self.logger.debug("MemoryStore", f"跳过损坏的 patch 文件 {path.name}: {e}")
        # 按 confidence 降序
        patches.sort(key=lambda x: -x.confidence)
        return patches

    def approve_patch(self, patch_id: str, reviewer: str = "human") -> bool:
        """审核通过一个 SkillPatch"""
        path = self.patches_dir / f"{patch_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = "approved"
            data["reviewed_by"] = reviewer
            data["reviewed_at"] = datetime.now().isoformat()
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.logger.info("MemoryStore", f"批准 SkillPatch: {patch_id}")
            return True
        except Exception as e:
            self.logger.error("MemoryStore", f"批准失败: {e}")
            return False

    def reject_patch(self, patch_id: str, reviewer: str = "human") -> bool:
        """拒绝一个 SkillPatch"""
        path = self.patches_dir / f"{patch_id}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["status"] = "rejected"
            data["reviewed_by"] = reviewer
            data["reviewed_at"] = datetime.now().isoformat()
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.logger.info("MemoryStore", f"拒绝 SkillPatch: {patch_id}")
            return True
        except Exception as e:
            self.logger.error("MemoryStore", f"拒绝失败: {e}")
            return False

    # ---- 容量控制 ----

    def enforce_capacity(self, max_per_month: int = 50, keep_ratio: float = 0.8):
        """每月超过 max_per_month 条时,保留 success_rate 最高的 keep_ratio 部分"""
        for month_dir in self.failures_dir.iterdir():
            if not month_dir.is_dir():
                continue
            files = list(month_dir.glob("*.json"))
            if len(files) <= max_per_month:
                continue

            # 读取所有记录
            records: List[tuple] = []
            for path in files:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    rate = data.get("success_rate", 0.0)
                    records.append((path, rate))
                except Exception as e:
                    self.logger.debug("MemoryStore", f"容量控制跳过损坏记录 {path.name}: {e}")

            # 按 success_rate 降序排序,保留 top keep_ratio
            records.sort(key=lambda x: -x[1])
            keep_count = int(len(records) * keep_ratio)
            for path, _ in records[keep_count:]:
                path.unlink(missing_ok=True)
                self.logger.info("MemoryStore", f"容量清理删除: {path.name}")

    # ---- 统计 ----

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计"""
        total_failures = sum(
            1 for m in self.failures_dir.iterdir()
            if m.is_dir() for _ in m.glob("*.json")
        )
        total_successes = 0
        success_path = self.successes_dir / "success_index.jsonl"
        if success_path.exists():
            total_successes = len(success_path.read_text(encoding="utf-8").splitlines())

        pending = self.get_pending_patches()

        return {
            "total_failures": total_failures,
            "total_successes": total_successes,
            "pending_patches": len(pending),
            "recent_failures_by_month": {
                m.name: len(list(m.glob("*.json")))
                for m in self.failures_dir.iterdir() if m.is_dir()
            },
        }


# ---- 全局单例 ----
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """获取 MemoryStore 全局实例"""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store
