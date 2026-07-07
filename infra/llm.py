"""LLM 客户端封装"""
from typing import List, Dict, Optional
from openai import OpenAI

from infra.config import config


class LLMClient:
    """LLM 客户端"""

    def __init__(self):
        self.client = OpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url
        )
        self.model = config.llm_model
        self.temperature = config.temperature

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        stream: bool = False
    ) -> str:
        """发送对话请求"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature or self.temperature,
            stream=stream
        )
        return response.choices[0].message.content

    def complete(
        self,
        prompt: str,
        temperature: Optional[float] = None
    ) -> str:
        """发送补全请求"""
        messages = [{"role": "user", "content": prompt}]
        return self.chat(messages, temperature)


# 全局单例
_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    """获取 LLM 客户端单例"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
