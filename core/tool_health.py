"""工具健康度 —— 让助自己能发现"这个工具不好用了"。

## 问题的本质

`create_tool` 只解决了**造**：校验、测试、发布。
但没有解决**用坏了怎么办**：

- 工具跑一次失败 → 没人记账
- 失败第 5 次 → 还是没人管
- 工具压根不存在（agent 想要的能力没有）→ 它会**绕过**，比如把一段视频
  复制成"成片"交差，而框架完全不觉得有问题

实机踩过：`ai_drama_final.mp4` 只有 5.6 秒（三个镜头合计 16.9 秒），
根本没拼接 —— 因为 ffmpeg 不存在、`subprocess` 又被禁。
框架对此毫无反应，用户只能自己发现。

## 这个模块做三件事

1. **记账**：每次工具调用落一条流水（成功/失败/错误/耗时）
2. **算指标**：滑动窗口的成功率、连续失败次数、最近的错误样本
3. **给判断**：哪些工具该判为"不健康"、该提示 agent 别再用

## 关键设计：降级是**算出来的**，不是写死的

和技能降级（`core/skill_quality.py`）同一个原则：
状态每次从流水重算，所以工具修好了会**自动恢复**。
不然后台一次误判就永久废掉一个工具。

## 两种不健康，处理方式不同

| 类型 | 判据 | 处理 |
|---|---|---|
| **坏了** | 失败率高 / 连续失败 | 自造工具 → 尝试自动出 v2；内置工具 → 提示 agent 别用 + 给人出报告 |
| **缺了** | agent 主动申报，或反复出现 Unknown tool | 记为"能力缺口"，喂给工具自增长 |

内置工具**绝不允许自动改写源码** —— 那是我们的代码，不是 agent 的产物。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from core.exception_policy import downgrade


#: 至少要有这么多样本，才允许判"不健康"（避免用两次失败就下结论）
MIN_SAMPLES = 3
#: 窗口内失败率超过这个值 → 不健康
FAIL_RATE_THRESHOLD = 0.5
#: 连续失败达到这个次数 → 不健康（比失败率更早发现"彻底坏了"）
CONSECUTIVE_FAIL_LIMIT = 3
#: 计算成功率时看最近多少条
WINDOW = 50
#: 给规划层提示时最多列几个工具
MAX_PROMPT_TOOLS = 5


@dataclass
class ToolCallRecord:
    ts: float
    tool: str
    ok: bool
    error: str = ""
    duration_ms: float = 0.0
    source: str = ""          # builtin / created / mcp

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class ToolHealth:
    """某个工具的当前健康画像。"""

    tool: str
    total: int = 0
    ok: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    recent_errors: List[str] = field(default_factory=list)
    avg_ms: float = 0.0
    healthy: bool = True
    reason: str = ""

    @property
    def fail_rate(self) -> float:
        return (self.failed / self.total) if self.total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "fail_rate": round(self.fail_rate, 3),
            "consecutive_failures": self.consecutive_failures,
            "avg_ms": round(self.avg_ms, 1),
            "healthy": self.healthy,
            "reason": self.reason,
            "recent_errors": self.recent_errors[:3],
        }


class ToolHealthStore:
    """工具调用流水（append-only JSONL）。"""

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory" / "tools"
        self.base = Path(base_path)
        self.ledger = self.base / "health.jsonl"
        self.gaps = self.base / "gaps.jsonl"
        self.logger = get_logger()

    # ---------- 记账 ----------

    def record(
        self,
        tool: str,
        ok: bool,
        *,
        error: str = "",
        duration_ms: float = 0.0,
        source: str = "",
    ) -> None:
        """落一条流水。**永远不抛异常** —— 观测坏了不能拖垮工具调用。"""
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            rec = ToolCallRecord(
                ts=time.time(),
                tool=str(tool)[:80],
                ok=bool(ok),
                error=str(error or "")[:500],
                duration_ms=float(duration_ms or 0.0),
                source=str(source or "")[:40],
            )
            with self.ledger.open("a", encoding="utf-8") as f:
                f.write(rec.to_json() + "\n")
        except Exception as exc:
            try:
                self.logger.warning("ToolHealth", f"记录失败: {exc}")
            except Exception as exc:
                downgrade(exc, where='core/tool_health.py')
    # ---------- 读流水 ----------

    def _read_records(self, limit_per_tool: int = WINDOW) -> Dict[str, List[ToolCallRecord]]:
        """按工具分组读最近若干条。文件不存在/坏了都返回空。"""
        grouped: Dict[str, List[ToolCallRecord]] = {}
        if not self.ledger.exists():
            return grouped
        try:
            lines = self.ledger.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return grouped

        for line in lines[-5000:]:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                rec = ToolCallRecord(**{
                    k: raw.get(k) for k in
                    ("ts", "tool", "ok", "error", "duration_ms", "source")
                })
            except Exception:
                continue
            bucket = grouped.setdefault(rec.tool, [])
            bucket.append(rec)

        # 每个工具只留最近 N 条
        return {k: v[-limit_per_tool:] for k, v in grouped.items()}

    # ---------- 算健康 ----------

    def health(self, tool: str) -> Optional[ToolHealth]:
        grouped = self._read_records()
        records = grouped.get(tool)
        if not records:
            return None
        return _compute(tool, records)

    def snapshot(self) -> List[ToolHealth]:
        grouped = self._read_records()
        out = [_compute(name, recs) for name, recs in grouped.items() if recs]
        out.sort(key=lambda h: (h.healthy, -h.fail_rate, -h.total))
        return out

    def unhealthy(self) -> List[ToolHealth]:
        return [h for h in self.snapshot() if not h.healthy]

    # ---------- 能力缺口 ----------

    def record_gap(
        self,
        need: str,
        *,
        tried: str = "",
        blocked_by: str = "",
        user_input: str = "",
    ) -> None:
        """记录"我需要一个能力但没有"。"""
        try:
            self.base.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": time.time(),
                "at": datetime.now().isoformat(timespec="seconds"),
                "need": str(need or "")[:500],
                "tried": str(tried or "")[:500],
                "blocked_by": str(blocked_by or "")[:500],
                "user_input": str(user_input or "")[:500],
            }
            with self.gaps.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            try:
                self.logger.warning("ToolHealth", f"记录能力缺口失败: {exc}")
            except Exception as exc:
                downgrade(exc, where='core/tool_health.py')
    def list_gaps(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.gaps.exists():
            return []
        try:
            lines = self.gaps.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))

    # ---------- 给规划层看的摘要 ----------

    def health_block(self, max_tools: int = MAX_PROMPT_TOOLS) -> str:
        """把"哪些工具不好用"压成一小段，注入规划提示词。

        只列**不健康**的 —— 健康的工具不需要提醒，列全了只会挤爆上下文。
        """
        bad = self.unhealthy()[:max_tools]
        if not bad:
            return ""
        lines = []
        for h in bad:
            err = h.recent_errors[0][:100] if h.recent_errors else ""
            lines.append(
                f"- {h.tool}：{h.failed}/{h.total} 次失败"
                + (f"，最近错误「{err}」" if err else "")
            )
        return (
            "## 这些工具最近不可靠（数据来自真实调用记录）\n"
            + "\n".join(lines)
            + "\n\n**优先换一条路**：换工具、自己写代码实现、或用 create_tool 造一个新的。"
            "不要反复重试同一个已经坏掉的工具。"
            "如果确实没有可用的替代，用 report_capability_gap 说明缺什么。"
        )


def _compute(tool: str, records: List[ToolCallRecord]) -> ToolHealth:
    h = ToolHealth(tool=tool)
    h.total = len(records)
    h.ok = sum(1 for r in records if r.ok)
    h.failed = h.total - h.ok
    h.avg_ms = sum(r.duration_ms for r in records) / h.total if h.total else 0.0

    # 连续失败（从末尾往前数）
    for r in reversed(records):
        if r.ok:
            break
        h.consecutive_failures += 1

    # 最近几条错误（去重，保留顺序）
    seen = set()
    for r in reversed(records):
        if r.ok or not r.error:
            continue
        snippet = r.error.strip().replace("\n", " ")[:160]
        if snippet and snippet not in seen:
            seen.add(snippet)
            h.recent_errors.append(snippet)
        if len(h.recent_errors) >= 3:
            break

    # 判定（样本不够不下结论 —— 避免用两次失败就判死刑）
    if h.total < MIN_SAMPLES:
        h.healthy = True
        h.reason = f"样本不足（{h.total} < {MIN_SAMPLES}），暂不判定"
    elif h.consecutive_failures >= CONSECUTIVE_FAIL_LIMIT:
        h.healthy = False
        h.reason = f"连续失败 {h.consecutive_failures} 次"
    elif h.fail_rate > FAIL_RATE_THRESHOLD:
        h.healthy = False
        h.reason = f"失败率 {h.fail_rate:.0%}（{h.failed}/{h.total}）"
    else:
        h.healthy = True
        h.reason = f"失败率 {h.fail_rate:.0%}，可接受"

    return h


# ===== 单例 =====

_store: Optional[ToolHealthStore] = None


def get_health_store() -> ToolHealthStore:
    global _store
    if _store is None:
        _store = ToolHealthStore()
    return _store


def reset_health_store() -> None:
    global _store
    _store = None
