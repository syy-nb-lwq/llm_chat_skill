"""事件契约 —— 前后端的**唯一真相**。

## 为什么单独立一个模块

以前事件名散在十几个地方各写一遍字面量：`agent.py` 发 `"thinking"`、
`hub.py` 发 `"tool_call"`、`App.vue` 监听 `"thinking"`。
改一个名字要全仓搜，漏掉任何一端都不会报错 —— 只是那个面板从此不动了。

现在：
- 后端**只能**从这里取常量（`from core.events import EV_THINKING`）；
- 前端由 `scripts/gen_events_js.py` 生成本文件对应的 `frontend/src/events.js`；
- `tests/test_event_contract.py` 校验三件事：
  1. 后端出现的字面量事件名都在本模块里；
  2. 前端监听的事件名都在 `events.js` 里；
  3. `events.js` 与本模块完全同源（重新生成后无差异）。

任何一端偷写新名字，测试立刻红。

## 两类事件

- **STREAM**：发给前端的事件（websocket 推送），前端可以监听；
- **AUDIT**：只落 `memory/audit/*.jsonl`，用来事后拼出"那一刻发生了什么"，不推前端。

`log` 是例外：它走 `log/<client_id>` 主题（不走 `events/<client_id>`），
但语义上仍属 STREAM，前端同样监听。
"""
from __future__ import annotations

from typing import Dict, FrozenSet

# ===== STREAM：推给前端 =====

# --- 连接与身份 ---
EV_CONNECTED = "connected"
EV_DISCONNECTED = "disconnected"      # 前端本地合成（ws 关闭时），服务端不发
EV_PONG = "pong"
EV_RESET_ACK = "reset_ack"
EV_FEEDBACK_ACK = "feedback_ack"
EV_LOG = "log"

# --- 一轮对话的主干 ---
EV_THINKING = "thinking"
EV_PLAN = "plan"
EV_TOOL_CALL = "tool_call"
EV_TOOL_RESULT = "tool_result"
EV_TOOL_ERROR = "tool_error"
EV_MESSAGE_DELTA = "message_delta"
EV_MESSAGE_FINAL = "message_final"
EV_ERROR = "error"

# --- 澄清与教学 ---
EV_CLARIFICATION_REQUEST = "clarification_request"
EV_TEACHING_INTERACTIVE = "teaching_interactive"
EV_SKILL_LEARNED = "skill_learned"

# --- 目标循环（goal loop） ---
EV_GOAL_ROUND = "goal_round"
EV_GOAL_CONTINUE = "goal_continue"
EV_GOAL_STOP = "goal_stop"
EV_GOAL_PREP_ONLY = "goal_prep_only"

# --- 子智能体 ---
EV_SUBAGENT_SPAWNED = "subagent_spawned"
EV_SUBAGENT_PROGRESS = "subagent_progress"
EV_SUBAGENT_DONE = "subagent_done"
EV_MESSAGE = "message"

# ===== AUDIT：只落盘，不推前端 =====

EV_AGENT_SPAWNED = "agent_spawned"
EV_AGENT_PROGRESS = "agent_progress"
EV_AGENT_DONE = "agent_done"
EV_TASK_BRIEF = "task_brief"
EV_TASK_UPDATE = "task_update"
EV_TASK_CANCEL = "task_cancel"
EV_BLACKBOARD_WRITE = "blackboard_write"
EV_ACCEPTANCE_CHECK = "acceptance_check"
EV_BUDGET_EXHAUSTED = "budget_exhausted"
EV_TOOL_PROPOSED = "tool_proposed"
EV_TOOL_PUBLISHED = "tool_published"

# ===== 集合（测试与生成器用） =====

STREAM_EVENTS: FrozenSet[str] = frozenset({
    EV_CONNECTED,
    EV_DISCONNECTED,
    EV_PONG,
    EV_RESET_ACK,
    EV_FEEDBACK_ACK,
    EV_LOG,
    EV_THINKING,
    EV_PLAN,
    EV_TOOL_CALL,
    EV_TOOL_RESULT,
    EV_TOOL_ERROR,
    EV_MESSAGE_DELTA,
    EV_MESSAGE_FINAL,
    EV_ERROR,
    EV_CLARIFICATION_REQUEST,
    EV_TEACHING_INTERACTIVE,
    EV_SKILL_LEARNED,
    EV_GOAL_ROUND,
    EV_GOAL_CONTINUE,
    EV_GOAL_STOP,
    EV_GOAL_PREP_ONLY,
    EV_SUBAGENT_SPAWNED,
    EV_SUBAGENT_PROGRESS,
    EV_SUBAGENT_DONE,
    EV_MESSAGE,
})

AUDIT_EVENTS: FrozenSet[str] = frozenset({
    EV_AGENT_SPAWNED,
    EV_AGENT_PROGRESS,
    EV_AGENT_DONE,
    EV_TASK_BRIEF,
    EV_TASK_UPDATE,
    EV_TASK_CANCEL,
    EV_BLACKBOARD_WRITE,
    EV_ACCEPTANCE_CHECK,
    EV_BUDGET_EXHAUSTED,
    EV_TOOL_PROPOSED,
    EV_TOOL_PUBLISHED,
})

ALL_EVENTS: FrozenSet[str] = STREAM_EVENTS | AUDIT_EVENTS

#: `EV_*` 常量名 → 值。生成前端常量文件时按它输出（保证顺序稳定）。
EVENT_CONSTANTS: Dict[str, str] = {
    name: value
    for name, value in sorted(globals().items())
    if name.startswith("EV_") and isinstance(value, str)
}
