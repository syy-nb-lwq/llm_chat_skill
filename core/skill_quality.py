"""技能质量账本 —— 让「这个技能到底靠不靠谱」有答案(docs/14 §2.4)

## 现状的问题

一个"技能" = `Skill.method` 一段文本 + `steps` 列表。
回归样例(`Skill.regression`)是有的,但:

- 只在**发布/升级那一刻**跑一次,结果**不落库** → 没人知道后来怎么样了
- 样例是补丁提出者自己给的 → 只能证明"这一版没退化",
  不能证明"这个技能在真实使用中靠谱"

于是技能会越积越多,却回答不了"我该信哪个"。

## 本模块的做法

把两类信号合到**一张账本**上:

| 信号 | 来源 | 性质 |
|---|---|---|
| `sample` | 发布/升级时跑回归样例(`ResultValidator`) | 离线、确定、可复现 |
| `usage` | 技能被真实使用后,用户有没有纠正这一轮 | **线上、真实** |

`usage` 这一路来自 A-06 的纠正链路 —— 那才是真正的 ground truth。
"工具全调通了但用户说不对"这种情况,只有 `usage` 能抓到。

## 降级

通过率低于阈值 → 该技能**自动降级**:不再被 planner 选中。
降级是**推导出来的**,不是贴上去的标签 —— 数据变好就自动恢复,
不需要人工去解封(免得忘了又变成一潭死水)。

## 账本格式

追加写的 JSONL(`memory/skills/quality.jsonl`):

```json
{"ts": 1710000000.0, "skill": "日报", "version": "1.0.0", "kind": "sample", "total": 3, "passed": 3}
{"ts": 1710000001.0, "skill": "日报", "version": "1.0.0", "kind": "usage", "ok": true}
```
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from infra.logger import get_logger


# ===== 降级阈值(可配) =====

#: 至少跑过这么多次验收样例,才允许按样例通过率降级
DEFAULT_MIN_SAMPLE_RUNS = 2
#: 验收样例通过率低于此值 → 降级
DEFAULT_MIN_SAMPLE_PASS_RATE = 0.6
#: 至少被真实使用过这么多次,才允许按线上采纳率降级
DEFAULT_MIN_USAGE = 5
#: 线上采纳率低于此值 → 降级(被纠正率 > 50%)
DEFAULT_MIN_USAGE_PASS_RATE = 0.5

#: 计算通过率时只看最近这么多条记录(滑动窗口)
DEFAULT_SAMPLE_WINDOW = 20
DEFAULT_USAGE_WINDOW = 50


@dataclass
class SkillQuality:
    """一个技能的质量画像。"""

    skill: str
    version: str = ""
    # 验收样例
    sample_runs: int = 0
    sample_total: int = 0
    sample_passed: int = 0
    # 线上使用
    usage_total: int = 0
    usage_ok: int = 0
    usage_failed: int = 0
    # 最近一次样例运行的失败用例名(给前端看)
    failed_cases: List[str] = field(default_factory=list)
    updated_at: float = 0.0
    #: 每次样例运行的 (总数, 通过数) —— 降级判据用最近几次
    _sample_runs: List[tuple] = field(default_factory=list, repr=False)

    # ===== 指标 =====

    @property
    def sample_pass_rate(self) -> Optional[float]:
        """验收样例通过率(窗口内聚合);没跑过返回 None(不是 0)。"""
        if self.sample_total <= 0:
            return None
        return self.sample_passed / self.sample_total

    @property
    def recent_sample_pass_rate(self) -> Optional[float]:
        """**最近几次**验收的通过率 —— 降级判据看的是它,不是聚合值。

        为什么不用聚合值:一个技能早期失败、后来修好了,
        聚合通过率会被历史拖住,永远降级状态解不掉。
        "最近几次"才反映"它现在到底行不行"。
        """
        if not self._sample_runs:
            return None
        recent = self._sample_runs[-DEFAULT_MIN_SAMPLE_RUNS:]
        total = sum(t for t, _ in recent)
        if total <= 0:
            return None
        return sum(p for _, p in recent) / total

    @property
    def usage_pass_rate(self) -> Optional[float]:
        """线上采纳率;没用过返回 None。"""
        if self.usage_total <= 0:
            return None
        return self.usage_ok / self.usage_total

    @property
    def overall_pass_rate(self) -> Optional[float]:
        """综合通过率(样本 + 线上,按次数加权)。

        没有数据时返回 None —— "没数据"和"通过率 0"是两回事,
        混为一谈会让没被验证过的技能看起来"很不可靠"。
        """
        rates = []
        if self.sample_pass_rate is not None:
            rates.append((self.sample_pass_rate, self.sample_total))
        if self.usage_pass_rate is not None:
            rates.append((self.usage_pass_rate, self.usage_total))
        if not rates:
            return None
        num = sum(r * w for r, w in rates)
        den = sum(w for _, w in rates)
        return num / den if den else None

    def is_demoted(
        self,
        *,
        min_sample_runs: int = DEFAULT_MIN_SAMPLE_RUNS,
        min_sample_pass_rate: float = DEFAULT_MIN_SAMPLE_PASS_RATE,
        min_usage: int = DEFAULT_MIN_USAGE,
        min_usage_pass_rate: float = DEFAULT_MIN_USAGE_PASS_RATE,
    ) -> bool:
        """是否该降级。

        两条独立判据,满足任一即降级:

        1. **验收样例**:最近几次跑,通过率仍然不达标
           → 这个技能连自己声明的验收标准都过不了
        2. **线上使用**:被真实用过够多次,采纳率不达标
           → 用户一直在纠正它

        样本量不够时**不降级** —— 一两次失败不足以否定一个技能。
        """
        rate = self.recent_sample_pass_rate
        if (
            rate is not None
            and self.sample_runs >= min_sample_runs
            and rate < min_sample_pass_rate
        ):
            return True

        u_rate = self.usage_pass_rate
        if (
            u_rate is not None
            and self.usage_total >= min_usage
            and u_rate < min_usage_pass_rate
        ):
            return True

        return False

    def demote_reason(
        self,
        *,
        min_sample_runs: int = DEFAULT_MIN_SAMPLE_RUNS,
        min_sample_pass_rate: float = DEFAULT_MIN_SAMPLE_PASS_RATE,
        min_usage: int = DEFAULT_MIN_USAGE,
        min_usage_pass_rate: float = DEFAULT_MIN_USAGE_PASS_RATE,
    ) -> str:
        """降级原因(中文,直接给用户看)。"""
        rate = self.recent_sample_pass_rate
        if (
            rate is not None
            and self.sample_runs >= min_sample_runs
            and rate < min_sample_pass_rate
        ):
            return (
                f"验收样例通过率 {rate:.0%} < {min_sample_pass_rate:.0%}"
                f"(最近 {min_sample_runs} 次)"
            )
        u_rate = self.usage_pass_rate
        if (
            u_rate is not None
            and self.usage_total >= min_usage
            and u_rate < min_usage_pass_rate
        ):
            return (
                f"线上采纳率 {u_rate:.0%} < {min_usage_pass_rate:.0%}"
                f"(被用了 {self.usage_total} 次,{self.usage_failed} 次被纠正)"
            )
        return ""

    def to_dict(self) -> Dict:
        return {
            "skill": self.skill,
            "version": self.version,
            "sample_runs": self.sample_runs,
            "sample_total": self.sample_total,
            "sample_passed": self.sample_passed,
            "sample_pass_rate": (
                round(self.sample_pass_rate, 4)
                if self.sample_pass_rate is not None
                else None
            ),
            "recent_sample_pass_rate": (
                round(self.recent_sample_pass_rate, 4)
                if self.recent_sample_pass_rate is not None
                else None
            ),
            "usage_total": self.usage_total,
            "usage_ok": self.usage_ok,
            "usage_failed": self.usage_failed,
            "usage_pass_rate": (
                round(self.usage_pass_rate, 4)
                if self.usage_pass_rate is not None
                else None
            ),
            "overall_pass_rate": (
                round(self.overall_pass_rate, 4)
                if self.overall_pass_rate is not None
                else None
            ),
            "demoted": self.is_demoted(),
            "demote_reason": self.demote_reason(),
            "failed_cases": list(self.failed_cases),
            "updated_at": self.updated_at,
        }


class SkillQualityStore:
    """技能质量账本(追加写的 JSONL)。"""

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            root = Path(__file__).parent.parent
            base_path = root / "memory" / "skills"
        self.base_path = Path(base_path)
        self.file = self.base_path / "quality.jsonl"
        self.logger = get_logger()
        self._cache: Optional[Dict[str, SkillQuality]] = None

    # ===== 写入 =====

    def record_sample_run(
        self,
        skill: str,
        *,
        version: str = "",
        total: int = 0,
        passed: int = 0,
        failed_cases: Optional[List[str]] = None,
    ) -> None:
        """记一次验收样例运行结果。"""
        if not skill:
            return
        self._append(
            {
                "ts": time.time(),
                "skill": skill,
                "version": version,
                "kind": "sample",
                "total": int(total),
                "passed": int(passed),
                "failed_cases": list(failed_cases or []),
            }
        )

    def record_usage(
        self,
        skill: str,
        *,
        version: str = "",
        ok: bool = True,
        execution_id: str = "",
    ) -> None:
        """记一次真实使用的结果。

        `ok=False` 表示这一轮被用户纠正了(或整体失败)。

        `execution_id` 让同一次执行**只算一次**:
        一轮先被记成"成功",用户下一轮纠正时会被重评为"失败" ——
        如果两次都留着,这一轮就既算成功又算失败,通过率被抹平。
        带上 execution_id 之后,后写的记录覆盖先写的。
        """
        if not skill:
            return
        self._append(
            {
                "ts": time.time(),
                "skill": skill,
                "version": version,
                "kind": "usage",
                "ok": bool(ok),
                "exec": execution_id or "",
            }
        )

    def _append(self, record: Dict) -> None:
        try:
            self.base_path.mkdir(parents=True, exist_ok=True)
            with self.file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cache = None
        except Exception as exc:
            self.logger.warning("SkillQuality", f"写账本失败: {exc}")

    # ===== 读取 =====

    def _records(self) -> List[Dict]:
        if not self.file.exists():
            return []
        out = []
        try:
            with self.file.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue  # 坏行跳过,不因为一行坏数据整个账本不可用
        except Exception as exc:
            self.logger.warning("SkillQuality", f"读账本失败: {exc}")
        return out

    def get(self, skill: str) -> SkillQuality:
        """某个技能的当前质量画像。"""
        qualities = self._build()
        return qualities.get(
            skill, SkillQuality(skill=skill)
        )

    def all(self) -> Dict[str, SkillQuality]:
        return self._build()

    def demoted_skills(self) -> Set[str]:
        """当前应该被降级(不再被 planner 选中)的技能名。"""
        return {name for name, q in self._build().items() if q.is_demoted()}

    def _build(self) -> Dict[str, SkillQuality]:
        if self._cache is not None:
            return self._cache

        qualities: Dict[str, SkillQuality] = {}
        samples: Dict[str, List[Dict]] = {}
        usages: Dict[str, List[Dict]] = {}

        for rec in self._records():
            name = rec.get("skill")
            if not name:
                continue
            q = qualities.setdefault(name, SkillQuality(skill=name))
            q.updated_at = max(q.updated_at, float(rec.get("ts") or 0))
            if rec.get("version"):
                q.version = rec["version"]
            kind = rec.get("kind")
            if kind == "sample":
                samples.setdefault(name, []).append(rec)
            elif kind == "usage":
                usages.setdefault(name, []).append(rec)

        for name, recs in samples.items():
            q = qualities[name]
            window = recs[-DEFAULT_SAMPLE_WINDOW:]
            q.sample_runs = len(window)
            q.sample_total = sum(int(r.get("total") or 0) for r in window)
            q.sample_passed = sum(int(r.get("passed") or 0) for r in window)
            q._sample_runs = [
                (int(r.get("total") or 0), int(r.get("passed") or 0)) for r in window
            ]
            last = window[-1] if window else {}
            q.failed_cases = list(last.get("failed_cases") or [])

        for name, recs in usages.items():
            q = qualities[name]
            # 同一 execution_id 只保留最后一条 ——
            # 一轮被纠正时,前一次"成功"的记录要被后一次"失败"覆盖
            latest: Dict[str, Dict] = {}
            anonymous: List[Dict] = []
            for r in recs:
                exec_id = r.get("exec") or ""
                if exec_id:
                    latest[exec_id] = r
                else:
                    anonymous.append(r)
            window = (list(latest.values()) + anonymous)[-DEFAULT_USAGE_WINDOW:]
            q.usage_total = len(window)
            q.usage_ok = sum(1 for r in window if r.get("ok"))
            q.usage_failed = q.usage_total - q.usage_ok

        self._cache = qualities
        return qualities

    def reset(self) -> None:
        """清空账本(测试用)。"""
        try:
            if self.file.exists():
                self.file.unlink()
        except Exception:
            pass
        self._cache = None


# ===== 全局单例 =====

_store: Optional[SkillQualityStore] = None


def get_skill_quality_store() -> SkillQualityStore:
    global _store
    if _store is None:
        _store = SkillQualityStore()
    return _store


def reset_skill_quality_store() -> None:
    global _store
    _store = None
