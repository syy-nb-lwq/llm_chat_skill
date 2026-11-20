"""审计目录与统一事件名 —— 让"一轮发生了什么"可以被拼出来。

## 为什么需要它

编排型架构下，一轮任务会散成很多段：
主智能体派了 3 个子智能体 → A 造了个工具 → B 问 A 要了个参数 → C 失败了。

这些信息现在散在：
- `memory/audit/traces.jsonl`（耗时）
- `memory/tools/proposals/*.json`（工具提案）
- `memory/failures/*.json`（失败）
- 前端事件流（实时，但不留痕）

**出问题时没人能回答"那一刻到底发生了什么"。** 这个模块提供：
1. 统一的事件名常量（前后端、审计文件都用同一套）；
2. 统一的落盘辅助（JSONL 追加 / JSON 覆盖），写失败只记日志不抛。

## 事件名约定

事件名分两类：

- **流程事件**（推给前端）：`agent_*` / `task_*` / `message` / `blackboard_*` /
  `acceptance_check` / `budget_exhausted` / `tool_*`
- **旧事件名**（保留兼容）：`subagent_spawned` / `subagent_progress` /
  `subagent_done` —— 前端还在用，不删。

## 用法

```python
from core.audit import EVENT_TOOL_PUBLISHED, append_jsonl

append_jsonl("tools.jsonl", {"event": EVENT_TOOL_PUBLISHED, "name": "bmi_calc"})
```
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from infra.logger import get_logger
from core.exception_policy import downgrade

# ===== 统一事件名 =====

# --- 智能体生命周期 ---
EVENT_AGENT_SPAWNED = "agent_spawned"
EVENT_AGENT_PROGRESS = "agent_progress"
EVENT_AGENT_DONE = "agent_done"

# --- 编排 ---
EVENT_TASK_BRIEF = "task_brief"          # 主智能体派发了什么
EVENT_TASK_UPDATE = "task_update"        # 纠偏/补充信息
EVENT_TASK_CANCEL = "task_cancel"        # 取消
EVENT_BLACKBOARD_WRITE = "blackboard_write"
EVENT_ACCEPTANCE_CHECK = "acceptance_check"
EVENT_BUDGET_EXHAUSTED = "budget_exhausted"

# --- 通信 ---
EVENT_MESSAGE = "message"

# --- 工具 ---
EVENT_TOOL_PROPOSED = "tool_proposed"
EVENT_TOOL_PUBLISHED = "tool_published"

# --- 旧事件名（前端还在用，保留） ---
EVENT_SUBAGENT_SPAWNED = "subagent_spawned"
EVENT_SUBAGENT_PROGRESS = "subagent_progress"
EVENT_SUBAGENT_DONE = "subagent_done"


def audit_dir() -> Path:
    """审计目录（`memory/audit/`）。不存在就建。"""
    root = Path(__file__).resolve().parent.parent / "memory" / "audit"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        downgrade(exc, where='core/audit.py')
    return root


def append_jsonl(filename: str, payload: Dict[str, Any]) -> bool:
    """往审计文件追加一行 JSON。

    **写失败只记日志** —— 审计坏了不能拖垮业务。
    返回是否写成功。
    """
    try:
        path = audit_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as exc:
        try:
            get_logger().warning("Audit", f"写审计文件失败 {filename}: {exc}")
        except Exception as exc:
            downgrade(exc, where='core/audit.py')
        return False


def write_json(filename: str, payload: Dict[str, Any]) -> Optional[Path]:
    """写一个 JSON 文件（覆盖）。失败返回 None。"""
    try:
        path = audit_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path
    except Exception as exc:
        try:
            get_logger().warning("Audit", f"写审计文件失败 {filename}: {exc}")
        except Exception as exc:
            downgrade(exc, where='core/audit.py')
        return None


def now() -> float:
    """当前时间戳（统一口径，方便排序）。"""
    return time.time()


def event(
    name: str,
    *,
    exec_id: str = "",
    agent_id: str = "",
    user_id: str = "",
    session_id: str = "",
    **extra: Any,
) -> Dict[str, Any]:
    """构造一条标准审计事件。

    字段固定为：`event / ts / exec_id / agent_id / user_id / session_id` + 业务字段。
    固定字段是"能拼出一轮任务"的关键 —— 不能各写各的。
    """
    payload: Dict[str, Any] = {
        "event": name,
        "ts": round(now(), 3),
        "exec_id": exec_id,
        "agent_id": agent_id,
        "user_id": user_id,
        "session_id": session_id,
    }
    payload.update(extra)
    return payload


def current_identity() -> Dict[str, str]:
    """从执行上下文取当前身份（给 event() 填固定字段用）。"""
    try:
        from core.exec_context import get_exec_context

        ctx = get_exec_context()
        return {
            "exec_id": ctx.execution_id,
            "agent_id": ctx.agent_id,
            "session_id": ctx.session_id,
        }
    except Exception:
        return {"exec_id": "", "agent_id": "", "user_id": "", "session_id": ""}
