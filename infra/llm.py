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
        """获取当前 Provider

        **懒初始化**:如果一个 Provider 都没注册,就地按配置注册一遍。
        以前注册逻辑只写在 FastAPI 的 lifespan 里,于是独立入口
        (nightly 评估 / CLI / Streamlit)全都在第一次 LLM 调用时炸掉。
        放到这里之后,任何入口都不可能"忘记"注册。
        """
        if self._provider is None:
            manager = self._ensure_manager()
            name = self._provider_name or config.default_provider
            self._provider = manager.get(name)
        return self._provider

    def _ensure_manager(self):
        """拿到 provider 管理器,并保证至少注册过一个 provider。"""
        manager = get_provider_manager()
        if not manager.list_providers():
            from infra.providers.registry import init_providers_from_config

            init_providers_from_config()
        return manager

    def _resolve_provider(self, provider: Optional[str]) -> BaseProvider:
        """解析本次调用要用的 Provider。

        **只解析、不缓存** —— 见 `chat()` 里关于"为什么不能写回 self._provider"
        的说明。provider 为空时返回实例的默认 provider。
        """
        if not provider:
            return self.provider
        return self._ensure_manager().get(provider)

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

        `provider` 是**本次调用**的选择,不会改变这个 client 的默认 provider。

        为什么强调这点：过去这里写的是 `self._provider = manager.get(provider)`，
        于是"这一次用便宜的小模型"会把单例的默认 provider 永久改掉。等真出现
        "意图识别用小模型、画像抽取用另一家"时，两边会互相污染，而且症状是
        "偶发答得差"，极难定位。要固定某个用途的 provider，请用
        `LLMClient(provider_name=...)` 建独立实例。
        """
        temp = temperature if temperature is not None else config.temperature

        if provider:
            client = self._resolve_provider(provider)
            if stream:
                return self._stream_with(client, messages, temp, model)
            return await self._complete_with(client, messages, temp, model)

        if stream:
            return self._stream(messages, temp, model)
        return await self._complete(messages, temp, model)

    async def _complete(
        self, 
        messages: List[Dict[str, str]], 
        temperature: float,
        model: Optional[str] = None,
    ) -> str:
        return await self._complete_with(self.provider, messages, temperature, model)

    async def _complete_with(
        self,
        client: BaseProvider,
        messages: List[Dict[str, str]],
        temperature: float,
        model: Optional[str] = None,
    ) -> str:
        return await client.chat(
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
        async for chunk in self._stream_with(self.provider, messages, temperature, model):
            yield chunk

    async def _stream_with(
        self,
        client: BaseProvider,
        messages: List[Dict[str, str]],
        temperature: float,
        model: Optional[str] = None,
    ) -> AsyncIterator[str]:
        async for delta in client.chat_stream(
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
        # 用 is not None 判断：temperature=0.0 是合法的确定性请求，
        # 用 `or` 会把 0.0 当成假值、悄悄退回 config.temperature（默认 0.7）。
        temp = temperature if temperature is not None else config.temperature
        async for token in self._stream(messages, temp, model):
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
        """切换**这个 client 的默认** Provider。

        注意：这是显式改全局默认，与 `chat(provider=...)` 的一次性选择不同。
        只应该在"用户主动切换模型"这类设置场景调用。
        """
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
