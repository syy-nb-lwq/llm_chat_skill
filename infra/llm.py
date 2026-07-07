"""LLM 客户端封装"""
import asyncio
from typing import List, Dict, Optional, AsyncIterator, Union
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError

from infra.config import config


class LLMClient:
    """LLM 客户端(异步,支持重试、流式、token 计数)"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            timeout=config.request_timeout_s,
        )
        self.model = config.llm_model
        self.default_temperature = config.temperature

    async def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        stream: bool = False,
    ) -> Union[str, AsyncIterator[str]]:
        """统一入口:
        - stream=False: 返回完整字符串
        - stream=True:  返回 async iterator,每个 yield 是一个 token 字符串
        """
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "stream": stream,
        }
        if stream:
            return self._stream(messages, temperature)
        return await self._complete(messages, temperature)

    async def _complete(self, messages, temperature) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.default_temperature,
        )
        return resp.choices[0].message.content or ""

    async def _stream(self, messages, temperature) -> AsyncIterator[str]:
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature if temperature is not None else self.default_temperature,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def chat_with_retry(
        self,
        messages: List[Dict[str, str]],
        *,
        max_retries: int = 3,
        temperature: Optional[float] = None,
    ) -> str:
        """对 RateLimitError / APITimeoutError 指数退避重试"""
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                return await self._complete(messages, temperature)
            except (RateLimitError, APITimeoutError) as e:
                last_err = e
                if attempt >= max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 10))
            except APIError as e:
                # 其他 API 错误不重试
                raise
        raise RuntimeError(f"LLM 调用失败(已重试 {max_retries} 次): {last_err}")

    async def stream(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]:
        """便捷流式"""
        async for token in self._stream(messages, temperature):
            yield token

    def count_tokens(self, messages: List[Dict[str, str]]) -> int:
        """估算 token 数(优先 tiktoken,失败回退到字符估算)"""
        try:
            import tiktoken
            try:
                enc = tiktoken.encoding_for_model(self.model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            total = 0
            for m in messages:
                total += len(enc.encode(m.get("content") or ""))
                total += 4  # role + 结构开销
            return total
        except Exception:
            return sum(len(m.get("content") or "") for m in messages) // 2


# 全局单例
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def reset_llm_client():
    """重置单例(用于切换配置后)"""
    global _llm_client
    _llm_client = None