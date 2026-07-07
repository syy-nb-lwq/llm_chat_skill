"""上下文管理"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Message:
    """消息"""
    role: str  # user / assistant / system / tool
    content: str
    tool_call: Optional[Dict] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Context:
    """对话上下文(多轮对话记忆)"""

    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ----- 写入 -----
    def add_message(self, role: str, content: str, tool_call: Optional[Dict] = None):
        self.messages.append(Message(role=role, content=content, tool_call=tool_call))

    def add_user_message(self, content: str):
        self.add_message("user", content)

    def add_assistant_message(self, content: str, tool_call: Optional[Dict] = None):
        self.add_message("assistant", content, tool_call)

    def add_system_message(self, content: str):
        self.add_message("system", content)

    def add_tool_message(self, content: str):
        self.add_message("tool", content)

    # ----- 读取 -----
    def get_last_user_message(self) -> Optional[str]:
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg.content
        return None

    def get_recent(self, n: int) -> List[Message]:
        return self.messages[-n:]

    # ----- LLM 格式 -----
    def to_llm_format(self) -> List[Dict]:
        result = []
        for msg in self.messages:
            item = {"role": msg.role, "content": msg.content}
            if msg.tool_call:
                item["tool_calls"] = [msg.tool_call]
            result.append(item)
        return result

    def to_llm_messages(self, max_tokens: int = 4000) -> List[Dict]:
        """生成 LLM 输入,token 超限时压缩早期消息"""
        msgs = self.to_llm_format()
        return self._truncate(msgs, max_tokens)

    def _truncate(self, msgs: List[Dict], max_tokens: int) -> List[Dict]:
        """简单按字符长度估算(避免引入 tiktoken 依赖在这里)
        策略:保留 system + 最近 N 条;总字符数不超过 max_tokens*2(经验值)。
        """
        if not msgs:
            return msgs
        char_budget = max_tokens * 2
        total = sum(len(m.get("content") or "") for m in msgs)
        if total <= char_budget:
            return msgs
        # 保留 system 和最后若干条
        system_msgs = [m for m in msgs if m.get("role") == "system"]
        others = [m for m in msgs if m.get("role") != "system"]
        keep = []
        cur_chars = sum(len(m.get("content") or "") for m in system_msgs)
        for m in reversed(others):
            c = len(m.get("content") or "")
            if cur_chars + c > char_budget and len(keep) >= 2:
                break
            keep.append(m)
            cur_chars += c
        return system_msgs + list(reversed(keep))

    # ----- 维护 -----
    def clear(self):
        self.messages.clear()
        self.metadata.clear()

    def __len__(self):
        return len(self.messages)