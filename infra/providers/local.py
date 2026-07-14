"""Local Provider (Ollama)"""
from typing import List, Dict, Optional, AsyncIterator
import httpx

from infra.providers.base import BaseProvider, ChatDelta


class LocalProvider(BaseProvider):
    """Ollama / 本地模型 Provider"""

    name = "local"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "llama3.2",
        timeout: int = 120,
    ):
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout

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

        # Ollama Chat API 格式
        ollama_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                # Ollama 支持 system 角色
                ollama_messages.append({
                    "role": "system",
                    "content": msg.get("content", ""),
                })
            else:
                ollama_messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": ollama_messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")

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

        ollama_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                ollama_messages.append({
                    "role": "system",
                    "content": msg.get("content", ""),
                })
            else:
                ollama_messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": ollama_messages,
                    "stream": True,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line:
                        import json
                        try:
                            chunk = json.loads(line)
                            if chunk.get("done"):
                                yield ChatDelta(content="", finish_reason="stop")
                            else:
                                content = chunk.get("message", {}).get("content", "")
                                if content:
                                    yield ChatDelta(content=content)
                        except json.JSONDecodeError:
                            continue

    def supports_function_calling(self) -> bool:
        # Ollama 部分模型支持 tool calling (如 llama3.2 + function calls)
        return False  # 默认不支持,可配置
