"""上下文管理 —— 多轮对话的**工作上下文**。

职责边界(踩过坑才写清楚的):
- ``Context`` 只管内存里的东西:消息列表、常驻 system 指令、滚动摘要视图、
  最近一轮的工具计划。
- **持久化**在 ``core/session_memory.py``。``Context`` 只通过 ``sink`` 回调
  把新消息交出去,不关心怎么落库;落库失败也不能影响对话本身。

几条容易踩的规则:
- ``messages`` 里只放**可见对话**(user / assistant)。常驻 system 指令放
  ``system_messages``,早期内容的摘要放 ``summary`` —— 混进 ``messages``
  会被下游按 role 过滤掉,等于白写。
- ``rollup_cover_seq`` 是**持久化簿记**(摘要覆盖到第几条),和 ``metadata``
  里的会话态(clarify_* 等)是两回事,不能一起清。
- ``last_tool_tasks`` 必须是**独立字段**:挂在 ``messages[-1]`` 上会挂错消息
  (写的时候最后一条是用户消息),而且空列表会被当成"没有记录"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Dict, List, Optional

from infra.logger import get_logger


#: 截断标记(算长度时要把它算进去,不然"截断后"反而更长了)。
_TRUNC_MARK = "…(已截断)"

_logger = get_logger()


def _now_iso() -> str:
    """带时区的时间戳(naive 时间戳跨机器/跨时区没法比较)。"""
    return datetime.now().astimezone().isoformat()


@dataclass
class Message:
    """一条对话消息。"""

    role: str  # user / assistant
    content: str
    #: 本轮执行的工具计划。跟着助手消息一起落库 —— 会话恢复后才知道
    #: "上一轮调了哪些工具"(重试要用)。
    tool_tasks: Optional[List[Dict]] = None
    timestamp: str = field(default_factory=_now_iso)
    seq: int = 0  # 会话内绝对序号(落库后由 sink 回填);0 = 未落库


@dataclass
class Context:
    """对话上下文(多轮对话记忆)。

    - ``sink``: 持久化回调。设置后,``add_message`` 会把每条消息交给它落库
      (短期记忆)。恢复历史时**不会**走 sink,避免重复写入。
    - ``summary``: 会话早期内容的滚动摘要(由 SessionMemoryStore 维护)。
      原文仍在库里,这里只是给 LLM 用的压缩视图。
    - ``rollup_cover_seq``: 摘要覆盖到的**会话绝对序号**边界(<= 它表示
      已被摘要覆盖)。这是持久化簿记,``clear()`` 不会清它。
    - ``last_tool_tasks``: 最近一轮的工具计划。独立字段,不挂在消息上。
    """

    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    sink: Optional[Callable[[Message], None]] = None
    summary: str = ""
    #: 常驻 system 指令(人设/能力清单/用户画像等),不参与轮次交替。
    system_messages: List[str] = field(default_factory=list)
    #: 最近一轮的工具计划;``None`` = 还没有记录,``[]`` = 那一轮不需要工具。
    last_tool_tasks: Optional[List[Dict]] = None
    #: 摘要覆盖到的会话绝对序号边界(持久化簿记,不是会话态)。
    rollup_cover_seq: int = 0

    #: 滚动摘要的最大字符数。超了只保留**最近**的部分(越近的内容越相关)。
    MAX_SUMMARY_CHARS: ClassVar[int] = 1200
    #: 单条对话消息注入 LLM 时的最大字符数(超了截断)。
    MAX_MESSAGE_CHARS: ClassVar[int] = 2000
    #: 单条 system 指令的最大字符数。system 是"人设 + 能力清单 + 用户画像",
    #: 给得比对话消息宽一点,但也不能无限大。
    MAX_SYSTEM_CHARS: ClassVar[int] = 4000
    #: 无论预算多小,至少保留这么多条最近的消息(否则模型看不到任何上下文)。
    MIN_KEPT_MESSAGES: ClassVar[int] = 2

    # ----- 写入 -----
    def add_message(
        self,
        role: str,
        content: str,
        tool_tasks: Optional[List[Dict]] = None,
    ) -> Message:
        """追加一条消息,并交给 sink 落库。

        Args:
            role: ``user`` / ``assistant``
            content: 消息正文
            tool_tasks: 本轮执行的工具计划(可选,会随消息一起落库)
        """
        msg = Message(role=role, content=content, tool_tasks=tool_tasks)
        self.messages.append(msg)
        self._persist(msg)
        return msg

    def _persist(self, msg: Message):
        """把消息交给 sink 落库。

        持久化失败绝不能影响对话本身 —— 但必须留下痕迹,否则就是静默丢数据。
        """
        if self.sink is None:
            return
        try:
            self.sink(msg)
        except Exception as exc:
            _logger.warning("Context", f"消息落库失败(已忽略,对话不受影响): {exc}")

    def add_user_message(self, content: str) -> Message:
        return self.add_message("user", content)

    def add_assistant_message(
        self,
        content: str,
        tool_tasks: Optional[List[Dict]] = None,
    ) -> Message:
        """追加一条助手消息。

        ``tool_tasks`` 显式传入 —— 不能隐式继承 ``last_tool_tasks``,
        否则闲聊轮会把上一轮的工具计划当成自己的。
        """
        return self.add_message("assistant", content, tool_tasks=tool_tasks)

    def add_system_message(self, content: str) -> None:
        """追加一条常驻 system 指令。

        **不进 ``messages``** —— 放进去会被下游按 role 过滤掉,等于白写。
        """
        text = (content or "").strip()
        if text:
            self.system_messages.append(text)

    # ----- tool_tasks 存储 -----
    def set_last_tool_tasks(self, tasks: Optional[List[Dict]]):
        """记录最近一轮的工具计划。

        ``tasks=[]`` 是**合法状态**(那一轮不需要工具),和"没有记录"是两回事 ——
        以前用 ``if msg.tool_tasks:` 判断,空列表会被跳过,重试就会重跑
        更早一轮的旧工具。
        """
        self.last_tool_tasks = list(tasks) if tasks is not None else None

    def get_last_tool_tasks(self) -> Optional[List[Dict]]:
        """取最近一轮的工具计划。``None`` = 没有记录,``[]`` = 不需要工具。"""
        return self.last_tool_tasks

    # ----- 读取 -----
    def get_last_user_message(self) -> Optional[str]:
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg.content
        return None

    def get_recent(self, n: int) -> List[Message]:
        """取最近 n 条消息。

        ``n <= 0`` 返回空列表 —— 注意 ``messages[-0:]`` 等于整个列表,
        直接切片会把这个边界坑暴露出来。
        """
        if n <= 0:
            return []
        return self.messages[-n:]

    def history_messages(self, exclude_content: str = "") -> List[Message]:
        """取历史对话(不含本轮输入)。

        Args:
            exclude_content: 本轮用户输入的正文。命中就**从后往前**剔除
                最后一条内容相同的用户消息。

        为什么不能简单地 ``messages[:-1]``:子智能体并发跑的时候,
        上下文可能已经被别的分支追加过,最后一条未必是本轮输入 ——
        直接砍最后一条会砍掉真正的历史,或者把本轮输入留在"历史"里。
        """
        msgs = list(self.messages)
        target = (exclude_content or "").strip()
        if target:
            for i in range(len(msgs) - 1, -1, -1):
                m = msgs[i]
                if m.role == "user" and (m.content or "").strip() == target:
                    del msgs[i]
                    break
        return msgs

    # ----- LLM 格式 -----
    def set_summary(self, text: str):
        """设置会话早期内容的滚动摘要(超长自动截断,只保留最近的部分)。

        越近的内容越相关,所以超长时砍掉**最早**的部分。
        """
        text = (text or "").strip()
        if len(text) > self.MAX_SUMMARY_CHARS:
            text = "…(更早的内容已省略)\n" + text[-self.MAX_SUMMARY_CHARS:]
        self.summary = text

    def get_summary(self) -> str:
        return self.summary

    def summary_block(self) -> str:
        """给 LLM 看的摘要块(为空则返回空串)。"""
        if not self.summary:
            return ""
        return f"【本次会话早期内容摘要】\n{self.summary}"

    def system_block(self) -> str:
        """给 LLM 看的常驻指令块(为空则返回空串)。"""
        parts = [s for s in self.system_messages if (s or "").strip()]
        if not parts:
            return ""
        return "\n".join(parts)

    def to_llm_format(self, messages: Optional[List[Message]] = None) -> List[Dict]:
        """转成 LLM 的 messages 格式(system 指令在最前)。

        Args:
            messages: 要转换的消息;默认是整个 ``messages``。
        """
        msgs = self.messages if messages is None else messages
        result: List[Dict] = [
            {"role": "system", "content": s} for s in self.system_messages
        ]
        for msg in msgs:
            result.append({"role": msg.role, "content": msg.content})
        return result

    def to_llm_messages(
        self,
        max_tokens: int = 4000,
        exclude_content: str = "",
    ) -> List[Dict]:
        """生成 LLM 输入,token 超限时压缩早期消息。

        Args:
            max_tokens: 字符预算(按 ``max_tokens * 2`` 估算)
            exclude_content: 本轮用户输入。命中就从历史里剔除 ——
                不能靠"砍掉最后一条",并发时最后一条未必是本轮输入。
        """
        msgs = self.history_messages(exclude_content=exclude_content)
        return self._truncate(self.to_llm_format(msgs), max_tokens)

    def _truncate(self, msgs: List[Dict], max_tokens: int) -> List[Dict]:
        """按字符预算裁剪消息列表。

        规则:
        1. system 永远保留,但**单条超长也要截** —— 否则一条巨大的 system
           就能把整个预算击穿。
        2. 从最新往回收,收满预算为止;至少保留 ``MIN_KEPT_MESSAGES`` 条。
        3. **预算必须真的守住**:单条巨无霸消息也要被压到预算内,不能靠
           "反正要留两条"把预算撑爆。
        """
        if not msgs:
            return msgs
        budget = max(0, int(max_tokens) * 2)

        system_msgs = [
            self._clip(m, self.MAX_SYSTEM_CHARS)
            for m in msgs if m.get("role") == "system"
        ]
        others = [m for m in msgs if m.get("role") != "system"]

        used = sum(len(m.get("content") or "") for m in system_msgs)
        keep: List[Dict] = []
        for m in reversed(others):
            c = len(m.get("content") or "")
            if used + c > budget and len(keep) >= self.MIN_KEPT_MESSAGES:
                break
            keep.append(m)
            used += c
        keep.reverse()

        out = system_msgs + [self._clip(m, self.MAX_MESSAGE_CHARS) for m in keep]
        return self._shrink_to_budget(out, budget)

    @staticmethod
    def _clip(item: Dict, max_chars: int) -> Dict:
        """截断单条消息的 content(超长时保留头部并标注)。

        截断标记本身也占字符 —— 切片长度要把它算进去,不然"截断后"
        反而比上限还长。
        """
        content = item.get("content") or ""
        if len(content) <= max_chars:
            return item
        clipped = dict(item)
        keep_len = max(0, max_chars - len(_TRUNC_MARK))
        clipped["content"] = content[:keep_len] + _TRUNC_MARK
        return clipped

    @staticmethod
    def _shrink_to_budget(items: List[Dict], budget: int) -> List[Dict]:
        """总量仍然超预算时,从**最老**的消息开始削。

        优先级:先削/丢最老的对话消息(保最近的内容),实在不够再削 system
        指令 —— 指令也不能无限大。
        """
        out: List[Optional[Dict]] = [dict(m) for m in items]

        def total() -> int:
            return sum(len(m.get("content") or "") for m in out if m is not None)

        def non_system_left() -> int:
            return sum(1 for m in out if m is not None and m.get("role") != "system")

        # 第一轮:最老的对话消息优先让位
        for i in range(len(out)):
            if total() <= budget:
                break
            m = out[i]
            if m is None or m.get("role") == "system":
                continue
            content = m.get("content") or ""
            over = total() - budget
            if len(content) <= over and non_system_left() > 1:
                out[i] = None  # 整条丢掉
                continue
            keep_len = max(0, len(content) - over - len(_TRUNC_MARK))
            m["content"] = content[:keep_len] + _TRUNC_MARK

        # 第二轮:还超就把最老的 system 指令也削一点
        for i in range(len(out)):
            if total() <= budget:
                break
            m = out[i]
            if m is None or m.get("role") != "system":
                continue
            content = m.get("content") or ""
            over = total() - budget
            keep_len = max(0, len(content) - over - len(_TRUNC_MARK))
            m["content"] = content[:keep_len] + _TRUNC_MARK

        return [m for m in out if m is not None]

    # ----- 维护 -----
    def clear(self):
        """清空工作上下文(会话内容本身仍在库里,可检索)。

        **不清** ``rollup_cover_seq`` —— 它是持久化簿记,不是会话态。
        清掉它会让下次滚动摘要把已经摘要过的内容重新摘要一遍。

        **不清** ``system_messages`` —— 那是常驻指令(人设/能力清单),
        换的是话题不是人设。
        """
        self.messages.clear()
        self.metadata.clear()
        self.summary = ""
        self.last_tool_tasks = None

    def snapshot(self) -> "Context":
        """返回一个**只读快照**。

        并发场景(子智能体 ``asyncio.gather``)下共享同一个 Context,
        各分支会看到互相穿插的消息列表。快照让每个并发分支看到一致的
        视图,而且不带 sink —— 快照里的消息不应该再被落库。
        """
        return Context(
            messages=list(self.messages),
            metadata=dict(self.metadata),
            sink=None,
            summary=self.summary,
            system_messages=list(self.system_messages),
            last_tool_tasks=self.last_tool_tasks,
            rollup_cover_seq=self.rollup_cover_seq,
        )

    def __len__(self):
        return len(self.messages)
