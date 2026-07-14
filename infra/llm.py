"""LLM 客户端封装 - 支持多 Provider"""
import asyncio
from typing import List, Dict, Optional, AsyncIterator, Union

from infra.config import config
from infra.providers import get_provider_manager, BaseProvider


class LLMClient:
    """LLM 客户端(支持多 Provider)"""

    def __init__(self, provider_name: Optional[str] = None):
        """初始化 LLMClient
        
        Args:
            provider_name: 指定 Provider 名称,默认使用配置的默认 Provider
        """
        self._provider_name = provider_name
        self._provider: Optional[BaseProvider] = None

    @property
    def provider(self) -> BaseProvider:
        """获取当前 Provider"""
        if self._provider is None:
            manager = get_provider_manager()
            name = self._provider_name or config.default_provider
            self._provider = manager.get(name)
        return self._provider

    async def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        stream: bool = False,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> Union[str, AsyncIterator[str]]:
        """统一入口:
        - stream=False: 返回完整字符串
        - stream=True:  返回 async iterator,每个 yield 是一个 token 字符串
        """
        # 切换 Provider
        if provider:
            self._provider = get_provider_manager().get(provider)
        
        temp = temperature if temperature is not None else config.temperature
        
        if stream:
            return self._stream(messages, temp, model)
        return await self._complete(messages, temp, model)

    async def _complete(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float,
        model: Optional[str] = None,
    ) -> str:
        return await self.provider.chat(
            messages, 
            model=model, 
            temperature=temperature,
        )

    async def _stream(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float,
        model: Optional[str] = None,
    ) -> AsyncIterator[str]:
        async for delta in self.provider.chat_stream(
            messages,
            model=model,
            temperature=temperature,
        ):
            yield delta.content

    async def chat_with_retry(
        self,
        messages: List[Dict[str, str]],
        *,
        max_retries: int = 3,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> str:
        """对错误指数退避重试"""
        last_err = None
        for attempt in range(1, max_retries + 1):
            try:
                return await self.chat(
                    messages, 
                    temperature=temperature, 
                    model=model,
                    provider=provider,
                )
            except Exception as e:
                last_err = e
                if attempt >= max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"LLM 调用失败(已重试 {max_retries} 次): {last_err}")

    async def stream(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """便捷流式"""
        async for token in self._stream(messages, temperature or config.temperature, model):
            yield token

    def count_tokens(self, messages: List[Dict[str, str]]) -> int:
        """估算 token 数(优先 tiktoken,失败回退到字符估算)"""
        try:
            import tiktoken
            model = config.openai_model
            try:
                enc = tiktoken.encoding_for_model(model)
            except KeyError:
                enc = tiktoken.get_encoding("cl100k_base")
            total = 0
            for m in messages:
                total += len(enc.encode(m.get("content") or ""))
                total += 4  # role + 结构开销
            return total
        except Exception:
            return sum(len(m.get("content") or "") for m in messages) // 2

    def switch_provider(self, name: str) -> None:
        """切换 Provider"""
        self._provider = None  # 强制重新加载
        self._provider_name = name


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
