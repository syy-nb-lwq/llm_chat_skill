"""消息总线 —— 子智能体之间（以及与主智能体之间）说话的唯一通道。

## 为什么不做"自由通信"

子智能体一旦可以随便聊，就会出现：互相问来问去、上下文互相污染、
没人知道谁说了什么、出了事没法复盘。`docs/15` §6 当初直接禁掉了子智能体
互传，理由是"拓扑一复杂就没法调试" —— 这个判断是对的。

所以这里做**受控通信**：

| 规则 | 内容 |
|---|---|
| 定向 | `to_agent` 必须是具体 agent，**不支持广播** |
| 有边 | 子→子只在编排图里**存在依赖边**时允许；没有依赖边 → 必须经主智能体转发 |
| 有限额 | 每个 agent 的消息条数、每条消息的字节数都有上限 |
| 有保序 | 同一对 agent 之间 FIFO |
| 有审计 | 每条消息落 `memory/audit/messages.jsonl` |
| 有失败语义 | 对方已完成 / 正在跑 / 已失败，三种情况分别怎么回 |

## 失败语义（最容易漏、也最容易出事的地方）

子智能体 A 问 B 一个问题，但 B 可能：

- **已经做完了** → 直接从 B 的报告包里找答案回 A，**不要让 B 重做一遍**；
- **正在跑** → 排队，等 B 结束时回一次；超时不回就告诉 A "对方未响应"；
- **已经失败/超时** → 明确告诉 A"对方已失败"，并让 A 知道没有答案。

含糊处理（沉默等待）会让 A 一直等下去 —— 这是最坏的一种失败。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from infra.logger import get_logger

# ===== 消息类型 =====


class MessageKind:
    """消息类型。"""

    TASK_ASSIGN = "task_assign"      # 主 → 子：下达任务
    TASK_UPDATE = "task_update"      # 主 → 子：补充信息 / 纠偏 / 缩范围
    TASK_CANCEL = "task_cancel"      # 主 → 子：取消
    REPORT = "report"                # 子 → 主：交活
    QUERY = "query"                  # 任意 → 任意：问一个具体问题
    ANSWER = "answer"                # 任意 → 提问者：回答
    HANDOFF = "handoff"              # 子 → 子：交接
    ESCALATE = "escalate"            # 子 → 主：我搞不定


#: 允许的消息类型
KINDS: Tuple[str, ...] = (
    MessageKind.TASK_ASSIGN,
    MessageKind.TASK_UPDATE,
    MessageKind.TASK_CANCEL,
    MessageKind.REPORT,
    MessageKind.QUERY,
    MessageKind.ANSWER,
    MessageKind.HANDOFF,
    MessageKind.ESCALATE,
)

#: 主智能体的 agent_id（没有 agent 上下文时也算）
MAIN_AGENT_IDS = ("", "main")


class MessageRejected(Exception):
    """消息被拒（路由不允许 / 超预算 / 载荷过大）。"""


@dataclass
class Message:
    """一条消息。"""

    kind: str
    from_agent: str
    to_agent: str
    payload: Dict[str, Any] = field(default_factory=dict)
    thread_id: str = ""
    reply_to: str = ""
    refs: List[str] = field(default_factory=list)
    msg_id: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.msg_id:
            self.msg_id = f"msg_{uuid.uuid4().hex[:10]}"
        if not self.thread_id:
            self.thread_id = self.msg_id

    @property
    def size(self) -> int:
        """消息大小（粗略字符数，用于预算）。"""
        import json

        try:
            return len(json.dumps(self.payload, ensure_ascii=False, default=str))
        except Exception:
            return len(str(self.payload))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "kind": self.kind,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "thread_id": self.thread_id,
            "reply_to": self.reply_to,
            "payload": dict(self.payload),
            "refs": list(self.refs),
            "created_at": self.created_at,
            "size": self.size,
        }


@dataclass
class _AgentState:
    """一个 agent 在总线上的状态。"""

    agent_id: str
    max_messages: int = 8
    sent: int = 0
    received: int = 0
    alive: bool = True
    done: bool = False
    report: Optional[Dict[str, Any]] = None
    inbox: List[Message] = field(default_factory=list)


class MessageBus:
    """受控消息总线。"""

    def __init__(
        self,
        *,
        max_payload_chars: int = 2000,
        default_max_messages: int = 8,
        audit: bool = True,
    ):
        self.logger = get_logger()
        self.max_payload_chars = max_payload_chars
        self.default_max_messages = default_max_messages
        self.audit_enabled = audit
        self._agents: Dict[str, _AgentState] = {}
        #: 允许的通信对（无向）：{(a, b)} —— 由编排图的依赖边注册
        self._allowed_pairs: Set[Tuple[str, str]] = set()
        #: 全部消息（按时间序）
        self._messages: List[Message] = []
        #: 待回答的查询：query_id → 提问者
        self._pending_queries: Dict[str, str] = {}

    # ===== 参与者 =====

    def register_agent(
        self, agent_id: str, *, max_messages: Optional[int] = None
    ) -> _AgentState:
        """登记一个参与者（主智能体也要登记，否则收不到消息）。"""
        state = self._agents.get(agent_id)
        if state is None:
            state = _AgentState(
                agent_id=agent_id,
                max_messages=(
                    max_messages if max_messages is not None
                    else self.default_max_messages
                ),
            )
            self._agents[agent_id] = state
        elif max_messages is not None:
            state.max_messages = max_messages
        return state

    def mark_done(
        self, agent_id: str, report: Optional[Dict[str, Any]] = None
    ) -> None:
        """标记某个 agent 已完成（带报告包）。"""
        state = self.register_agent(agent_id)
        state.done = True
        state.alive = False
        state.report = report

    def mark_failed(self, agent_id: str, reason: str = "") -> None:
        state = self.register_agent(agent_id)
        state.done = True
        state.alive = False
        state.report = {"status": "failed", "summary": reason}

    def is_alive(self, agent_id: str) -> bool:
        state = self._agents.get(agent_id)
        return bool(state and state.alive)

    # ===== 路由表 =====

    def allow_pair(self, a: str, b: str) -> None:
        """登记一对**允许直接通信**的 agent（编排图里的依赖边）。"""
        if not a or not b or a == b:
            return
        self._allowed_pairs.add((a, b))
        self._allowed_pairs.add((b, a))

    def allow_pairs_from_edges(self, edges: List[Tuple[str, str]]) -> None:
        """按编排图的依赖边批量登记。"""
        for a, b in edges or []:
            self.allow_pair(a, b)

    def _route_allowed(self, src: str, dst: str) -> bool:
        """这条通信是否被允许。"""
        if src in MAIN_AGENT_IDS or dst in MAIN_AGENT_IDS:
            return True    # 主 ↔ 子 不限
        return (src, dst) in self._allowed_pairs

    # ===== 发送 =====

    def send(
        self,
        *,
        kind: str,
        from_agent: str,
        to_agent: str,
        payload: Optional[Dict[str, Any]] = None,
        thread_id: str = "",
        reply_to: str = "",
        refs: Optional[List[str]] = None,
    ) -> Message:
        """发一条消息。被拒时抛 `MessageRejected`（**不静默丢**）。"""
        if kind not in KINDS:
            raise MessageRejected(f"未知消息类型: {kind}")
        if not to_agent:
            raise MessageRejected("to_agent 不能为空（本总线不支持广播）")
        if to_agent == "*" or to_agent.lower() == "broadcast":
            raise MessageRejected("不支持广播：消息必须发给具体的 agent")
        if not self._route_allowed(from_agent, to_agent):
            raise MessageRejected(
                f"不允许的通信: {from_agent} → {to_agent}"
                f"（编排图里没有这条依赖边，请经主智能体转发）"
            )

        msg = Message(
            kind=kind,
            from_agent=from_agent or "main",
            to_agent=to_agent,
            payload=payload or {},
            thread_id=thread_id,
            reply_to=reply_to,
            refs=list(refs or []),
        )
        if msg.size > self.max_payload_chars:
            raise MessageRejected(
                f"消息过大（{msg.size} > {self.max_payload_chars} 字符）"
                f"：请改成传引用（refs），别塞全文"
            )

        src = self.register_agent(from_agent)
        dst = self.register_agent(to_agent)
        if src.sent >= src.max_messages:
            raise MessageRejected(
                f"{from_agent} 的消息预算已用尽（{src.max_messages} 条）"
            )

        src.sent += 1
        dst.received += 1
        dst.inbox.append(msg)
        self._messages.append(msg)

        if kind == MessageKind.QUERY:
            self._pending_queries[msg.msg_id] = from_agent

        self._write_audit(msg)
        return msg

    # ===== 收 =====

    def inbox(self, agent_id: str) -> List[Message]:
        """看收件箱（不消费）。"""
        state = self._agents.get(agent_id)
        return list(state.inbox) if state else []

    def drain(self, agent_id: str) -> List[Message]:
        """取走收件箱里的消息（按到达顺序）。"""
        state = self._agents.get(agent_id)
        if not state:
            return []
        msgs = list(state.inbox)
        state.inbox.clear()
        return msgs

    def messages(self) -> List[Message]:
        """全部消息（审计用）。"""
        return list(self._messages)

    # ===== 问与答 =====

    def ask(
        self,
        *,
        from_agent: str,
        to_agent: str,
        question: str,
        thread_id: str = "",
    ) -> Dict[str, Any]:
        """问对方一个问题，并**拿到答复**。

        三种对方状态分别怎么处理：

        - **已完成** → 直接从它的报告包里找答案（**不让它重做**）；
        - **正在跑** → 发 QUERY 并排队，等它结束时由它回；
        - **已失败/不存在** → 立刻回"对方不可用"，不让提问方干等。

        Returns:
            `{"answered": bool, "answer": str, "state": "answered|queued|unavailable", ...}`
        """
        state = self._agents.get(to_agent)
        if state is None:
            return {
                "answered": False,
                "state": "unavailable",
                "answer": f"对方 {to_agent} 不存在",
            }
        if state.done and state.report:
            report_status = str(state.report.get("status") or "").lower()
            if report_status in ("failed", "timeout", "cancelled", "blocked"):
                # 对方是**失败**的 → 没有结论可用，要如实说，不要硬凑
                return {
                    "answered": False,
                    "state": "unavailable",
                    "answer": (
                        f"对方 {to_agent} 不可用：没有完成"
                        f"（{state.report.get('summary') or report_status}），"
                        f"没有可用结论"
                    ),
                }
            # 已完成且留了报告 → 直接从报告里回答（不让它重做）
            answer = self._answer_from_report(state.report, question)
            return {
                "answered": True,
                "state": "answered",
                "answer": answer,
                "source": "report",
            }
        if not state.alive:
            return {
                "answered": False,
                "state": "unavailable",
                "answer": f"对方 {to_agent} 不可用（已结束但没有留下结论）",
            }

        msg = self.send(
            kind=MessageKind.QUERY,
            from_agent=from_agent,
            to_agent=to_agent,
            payload={"question": question},
            thread_id=thread_id,
        )
        return {
            "answered": False,
            "state": "queued",
            "answer": "",
            "msg_id": msg.msg_id,
        }

    def answer(
        self,
        *,
        from_agent: str,
        to_agent: str,
        question_msg_id: str,
        answer: str,
        thread_id: str = "",
    ) -> Message:
        """回答一个问题（对方跑完时回）。"""
        return self.send(
            kind=MessageKind.ANSWER,
            from_agent=from_agent,
            to_agent=to_agent,
            payload={"answer": answer},
            thread_id=thread_id,
            reply_to=question_msg_id,
        )

    def pending_queries_for(self, agent_id: str) -> List[Message]:
        """还没被回答的提问（发给这个 agent 的）。"""
        return [
            m for m in self._agents.get(agent_id, _AgentState(agent_id)).inbox
            if m.kind == MessageKind.QUERY
        ]

    # ===== 内部 =====

    @staticmethod
    def _answer_from_report(report: Dict[str, Any], question: str) -> str:
        """从报告包里找答案。

        **找不到就说找不到** —— 编一个答案比没有答案更糟
        （下游会拿它当事实用）。
        """
        if not report:
            return "对方没有留下可用结论"
        findings = report.get("findings") or []
        summary = report.get("summary") or ""
        if question and question in (summary or ""):
            return summary
        if findings:
            return "\n".join(f"- {f}" for f in findings[:5])
        return summary or "对方已完成，但没有与该问题相关的结论"

    def _write_audit(self, msg: Message) -> None:
        if not self.audit_enabled:
            return
        try:
            from core.audit import append_jsonl

            append_jsonl("messages.jsonl", msg.to_dict())
        except Exception:
            pass

    def stats(self) -> Dict[str, Any]:
        """总线统计（观测用）。"""
        return {
            "agents": {
                aid: {
                    "sent": st.sent,
                    "received": st.received,
                    "max_messages": st.max_messages,
                    "done": st.done,
                }
                for aid, st in self._agents.items()
            },
            "messages": len(self._messages),
            "allowed_pairs": len(self._allowed_pairs),
        }


# ===== 全局单例 =====

_bus: Optional[MessageBus] = None


def get_message_bus() -> MessageBus:
    global _bus
    if _bus is None:
        _bus = MessageBus()
    return _bus


def reset_message_bus() -> None:
    global _bus
    _bus = None
