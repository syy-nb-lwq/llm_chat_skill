"""上下文管理"""
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Message:
    """消息"""
    role: str  # user / assistant / system / tool
    content: str
    tool_call: Optional[Dict] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class Context:
    """上下文"""
    messages: List[Message] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def add_message(self, role: str, content: str, tool_call: Optional[Dict] = None):
        """添加消息"""
        self.messages.append(Message(
            role=role,
            content=content,
            tool_call=tool_call
        ))
    
    def add_user_message(self, content: str):
        """添加用户消息"""
        self.add_message("user", content)
    
    def add_assistant_message(self, content: str, tool_call: Optional[Dict] = None):
        """添加助手消息"""
        self.add_message("assistant", content, tool_call)
    
    def add_system_message(self, content: str):
        """添加系统消息"""
        self.add_message("system", content)
    
    def add_tool_message(self, content: str):
        """添加工具结果"""
        self.add_message("tool", content)
    
    def to_llm_format(self) -> List[Dict]:
        """转换为 LLM 消息格式"""
        result = []
        for msg in self.messages:
            item = {"role": msg.role, "content": msg.content}
            if msg.tool_call:
                item["tool_calls"] = [msg.tool_call]
            result.append(item)
        return result
    
    def get_last_user_message(self) -> Optional[str]:
        """获取最后一条用户消息"""
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg.content
        return None
    
    def clear(self):
        """清空上下文"""
        self.messages.clear()
        self.metadata.clear()
