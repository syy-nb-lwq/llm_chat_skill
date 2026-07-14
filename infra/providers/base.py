"""Base Provider 抽象接口"""
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, AsyncIterator, Any
from dataclasses import dataclass


@dataclass
class ChatMessage:
    """聊天消息"""
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ChatDelta:
    """流式响应的 delta"""
    content: str
    finish_reason: Optional[str] = None


class BaseProvider(ABC):
    """LLM Provider 抽象基类"""

    name: str = "base"  # Provider 标识
    supports_streaming: bool = True

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str:
        """同步聊天,返回完整响应"""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatDelta]:
        """流式聊天,返回 delta 迭代器"""
        ...

    def supports_function_calling(self) -> bool:
        """是否支持 function calling / tool use"""
        return True

    async def chat_with_retry(
        self,
        messages: List[Dict[str, str]],
        *,
        max_retries: int = 3,
        model: Optional[str] = None,
        temperature: float = 0.7,
    ) -> str:
        """带重试的聊天(默认实现)"""
        import asyncio
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                return await self.chat(messages, model=model, temperature=temperature)
            except Exception as e:
                last_err = e
                if attempt >= max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"Provider {self.name} 调用失败(已重试 {max_retries} 次): {last_err}")
