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
    # M3-05:发布前校验结果与风险等级(给前端展示)
    validation: Optional[Dict] = None
    risk_level: str = "unknown"  # low / medium / high / unknown
    # M3-06:自动发布被拦下的原因
    blocked_reason: str = ""


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
        self.audit_dir = self.base / "audit"
        self.logger = get_logger()

        self._ensure_dirs()

    def _ensure_dirs(self):
        for d in [self.failures_dir, self.successes_dir, self.patches_dir, self.audit_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def _ensure_parent(self, path: Path) -> Path:
        """确保某个文件的**父目录**存在,返回该路径。

        为什么要单独做这个:`_ensure_dirs()` 只在 `__init__` 调一次。
        如果目录在进程运行期间被删掉(日志清理、部署脚本、磁盘清理、
        或者有人手动 `rm -rf memory/`),store 手里就攥着**失效的路径**,
        之后每次写/读都抛
        `[WinError 3] 系统找不到指定的路径`。

        实机踩过:跑测试时 `Remove-Item memory -Recurse -Force` 把整个目录删了,
        而后端还在跑 —— 自演化链路当场断掉,用户在界面上看到两次报错。

        所以:**每个碰磁盘的操作前都确认一次目录还在**。
        开销是几个 stat,换来的是"目录没了也不炸"。
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.logger.warning("MemoryStore", f"创建目录失败 {path.parent}: {exc}")
        return path

    def _safe_iterdir(self, directory: Path):
        """遍历目录;目录不存在时返回空 —— 不抛异常。"""
        try:
            if not directory.exists():
                return []
            return list(directory.iterdir())
        except Exception as exc:
            self.logger.warning("MemoryStore", f"遍历 {directory} 失败: {exc}")
            return []

    # ---- 审计(M3-06) ----

    def write_audit(self, kind: str, payload: Dict) -> str:
        """写一条审计记录,返回文件路径。

        自动发布 / 人工发布都必须留痕,便于事后追溯"谁在什么时候改了哪个技能"。
        """
        record = {
            "kind": kind,
            "timestamp": datetime.now().isoformat(),
            **payload,
        }
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self._ensure_parent(self.audit_dir / f"{kind}_{stamp}.json")
        try:
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

        path = self._ensure_parent(self.successes_dir / "success_index.jsonl")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

        self.logger.info("MemoryStore", f"记录成功: {trace_id}, skill={matched_skill}")
        return record

    def add_pending_patch(self, patch: SkillPatch) -> None:
        """添加待审阅的技能改进建议"""
        path = self._ensure_parent(self.patches_dir / f"{patch.id}.json")
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
        for month_dir in sorted(self._safe_iterdir(self.failures_dir), reverse=True):
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
                except Exception:
                    pass
                if len(hints) >= top_k:
                    break
        except Exception:
            pass

        return hints

    def get_pending_patches(self) -> List[SkillPatch]:
        """获取所有待审阅的改进建议"""
        patches: List[SkillPatch] = []
        for path in (self.patches_dir.glob("*.json") if self.patches_dir.exists() else []):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("status") == "pending":
                    patches.append(SkillPatch(**data))
            except Exception:
                pass
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
        for month_dir in self._safe_iterdir(self.failures_dir):
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
                except Exception:
                    pass

            # 按 success_rate 降序排序,保留 top keep_ratio
            records.sort(key=lambda x: -x[1])
            keep_count = int(len(records) * keep_ratio)
            for path, _ in records[keep_count:]:
                path.unlink(missing_ok=True)
                self.logger.info("MemoryStore", f"容量清理删除: {path.name}")

    # ---- 统计 ----

    def remove_success(self, trace_id: str) -> bool:
        """撤销一条成功记录。

        **为什么需要**:critic 先按"工具成功"记了成功,下一轮用户说不对,
        我们会用纠正信息重评这一轮 —— 这时必须把之前那条成功记录撤掉,
        否则同一轮既算成功又算失败,采纳率就失真了。

        `success_index.jsonl` 是追加写的,所以要整体重写一遍。
        """
        path = self._ensure_parent(self.successes_dir / "success_index.jsonl")
        if not path.exists():
            return False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            kept = []
            removed = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    kept.append(line)
                    continue
                if row.get("trace_id") == trace_id:
                    removed += 1
                    continue
                kept.append(line)
            if not removed:
                return False
            path.write_text(
                ("\n".join(kept) + "\n") if kept else "",
                encoding="utf-8",
            )
            self.logger.info("MemoryStore", f"撤销成功记录: {trace_id}")
            return True
        except Exception as exc:
            self.logger.warning("MemoryStore", f"撤销成功记录失败: {exc}")
            return False

    def get_adoption_rate(self) -> Dict[str, Any]:
        """答案采纳率 —— **自演化最核心的质量指标**。

        采纳率 = 1 − (被用户纠正的轮数 / 有评估记录的轮数)

        旧指标(success_rate)只看工具调没调通,一个"工具全通但答案全错"
        的系统能拿 100%。采纳率不会。
        """
        success_path = self.successes_dir / "success_index.jsonl"
        successes = 0
        if success_path.exists():
            successes = len(
                [l for l in success_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            )

        failures = 0
        corrected = 0
        for month in self._safe_iterdir(self.failures_dir):
                if not month.is_dir():
                    continue
                for f in month.glob("*.json"):
                    try:
                        row = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    failures += 1
                    if row.get("user_corrected"):
                        corrected += 1

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
        total_failures = sum(
            1 for m in self._safe_iterdir(self.failures_dir)
            if m.is_dir() for _ in m.glob("*.json")
        )
        total_successes = 0
        success_path = self.successes_dir / "success_index.jsonl"
        if success_path.exists():
            total_successes = len(success_path.read_text(encoding="utf-8").splitlines())

        pending = self.get_pending_patches()

        stats = {
            "total_failures": total_failures,
            "total_successes": total_successes,
            "pending_patches": len(pending),
            "recent_failures_by_month": {
                m.name: len(list(m.glob("*.json")))
                for m in self._safe_iterdir(self.failures_dir) if m.is_dir()
            },
        }
        stats.update(self.get_adoption_rate())
        return stats


# ---- 全局单例 ----
_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """获取 MemoryStore 全局实例"""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store
