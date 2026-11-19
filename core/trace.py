"""每轮对话的分阶段耗时记录(产品化的最低观测要求)

没有这个,"回答慢"只能靠猜:
是 L1 多打了一次模型?还是工具超时?还是首 token 迟迟不来?

设计原则:
- **不侵入**:任何异常都不能影响正常回答,写盘失败只记日志
- **一行一轮**:JSONL,方便直接 `jq` / pandas 分析
- **只记事实**:不带业务判断,延迟与规模都是客观量
"""
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


#: 阶段名常量(避免各处写字符串写错)
STAGE_PLAN = "plan"          # L0+L1+L2 整段规划
STAGE_TOOLS = "tools"        # 工具执行
STAGE_GENERATE = "generate"  # 回答生成(从发起到最后 token)
STAGE_FIRST_TOKEN = "first_token"  # 从本轮开始到第一个 delta
STAGE_CRITIC = "critic"      # 执行批评(异步)
STAGE_TOTAL = "total"        # 整轮


@dataclass
class TurnTrace:
    """一轮对话的耗时与规模"""

    trace_id: str
    session_id: str = ""
    turn_id: str = ""
    #: 执行隔离作用域（user+session 的哈希目录名，docs/18 F0-1）
    scope: str = ""
    #: 执行者：主智能体是 `main`，子智能体是自己的 id
    agent_id: str = ""
    intent_layer: str = ""       # L0 / L1 / L2
    intent: str = ""
    intent_confidence: float = 0.0
    tool_count: int = 0
    tool_failed: int = 0
    profile_chars: int = 0       # 注入的画像规模(0 = 没有画像)
    reply_chars: int = 0
    stage_ms: Dict[str, float] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    error: str = ""

    def mark(self, stage: str, ms: float) -> None:
        """记一个阶段的耗时(毫秒,累加)"""
        if ms < 0:
            ms = 0.0
        self.stage_ms[stage] = round(self.stage_ms.get(stage, 0.0) + ms, 1)

    def since(self, stage: str, start_ts: float) -> None:
        """从某个 perf_counter 起点记到现在的耗时"""
        self.mark(stage, (time.perf_counter() - start_ts) * 1000)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "scope": self.scope,
            "agent_id": self.agent_id,
            "intent_layer": self.intent_layer,
            "intent": self.intent,
            "intent_confidence": round(self.intent_confidence, 3),
            "tool_count": self.tool_count,
            "tool_failed": self.tool_failed,
            "profile_chars": self.profile_chars,
            "reply_chars": self.reply_chars,
            "stage_ms": dict(self.stage_ms),
            "error": self.error,
        }


class TraceRecorder:
    """把 TurnTrace 落到 JSONL

    `enabled=False` 时所有方法都是空操作 —— 调用方不需要到处判空。
    """

    def __init__(self, path: Optional[Path] = None, enabled: bool = True):
        if path is None:
            path = Path(__file__).resolve().parent.parent / "memory" / "audit" / "traces.jsonl"
        self.path = path
        self.enabled = bool(enabled)
        self.logger = get_logger()

    def record(self, trace: TurnTrace) -> None:
        """写一行。任何失败都吞掉 —— 观测不能拖垮业务。"""
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(trace.to_dict(), ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as exc:
            self.logger.warning("Trace", f"写 trace 失败: {exc}")

    def read(self, limit: int = 0) -> List[Dict[str, Any]]:
        """读回 trace(给汇总脚本/接口用)"""
        if not self.path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rows.append(json.loads(line))
        except Exception as exc:
            self.logger.warning("Trace", f"读 trace 失败: {exc}")
            return []
        if limit and limit > 0:
            return rows[-limit:]
        return rows


# ---- 全局单例 ----

_recorder: Optional[TraceRecorder] = None


def get_trace_recorder() -> TraceRecorder:
    global _recorder
    if _recorder is None:
        from infra.config import config

        _recorder = TraceRecorder(
            enabled=bool(getattr(config, "trace_enabled", True)),
        )
    return _recorder


def reset_trace_recorder() -> None:
    global _recorder
    _recorder = None
