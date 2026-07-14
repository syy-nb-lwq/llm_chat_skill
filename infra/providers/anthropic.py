"""Anthropic Provider (Claude)"""
from typing import List, Dict, Optional, AsyncIterator
import anthropic

from infra.providers.base import BaseProvider, ChatDelta


class AnthropicProvider(BaseProvider):
    """Anthropic Claude API Provider"""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        default_model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
    ):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.default_model = default_model
        self.max_tokens = max_tokens

    def _convert_messages(self, messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """OpenAI 格式转 Anthropic 格式"""
        converted = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                # Anthropic 使用单独的 system 参数
                continue
            elif role == "user":
                converted.append({"role": "user", "content": msg.get("content", "")})
            elif role == "assistant":
                converted.append({"role": "assistant", "content": msg.get("content", "")})
        return converted

    async def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> str:
        model = model or self.default_model
        max_tokens = max_tokens or self.max_tokens

        # 分离 system message
        system = ""
        converted_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system = msg.get("content", "")
            else:
                converted_messages.append(msg)

        resp = await self.client.messages.create(
            model=model,
            system=system,
            messages=converted_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return resp.content[0].text if resp.content else ""

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs,
    ) -> AsyncIterator[ChatDelta]:
        model = model or self.default_model
        max_tokens = max_tokens or self.max_tokens

        # 分离 system message
        system = ""
        converted_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system = msg.get("content", "")
            else:
                converted_messages.append(msg)

        async with self.client.messages.stream(
            model=model,
            system=system,
            messages=converted_messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        ) as stream:
            async for chunk in stream:
                if chunk.type == "content_block_delta" and chunk.delta.type == "text_delta":
                    yield ChatDelta(content=chunk.delta.text)
                elif chunk.type == "message_stop":
                    yield ChatDelta(content="", finish_reason="stop")

    def supports_function_calling(self) -> bool:
        # Claude 3.5+ 支持 tool use
        return True
