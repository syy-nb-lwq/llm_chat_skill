"""LLMClient 的 provider 必须是 per-call，不能污染默认值。

回归背景：`chat(provider=...)` 曾经写回 `self._provider`，把这个 client 的默认
provider 永久改掉。一旦"意图识别用小模型、画像抽取用另一家"落地，两次调用会
互相污染，症状是"偶发答得差"，极难定位。
"""
from __future__ import annotations

import pytest

from infra import llm as llm_mod
from infra.config import config
from infra.providers.base import ChatDelta


class _FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.calls = 0

    async def chat(self, messages, model=None, temperature=None):
        self.calls += 1
        return f"{self.name}:{messages[-1]['content']}"

    def chat_stream(self, messages, model=None, temperature=None):
        self.calls += 1

        async def _gen():
            yield ChatDelta(content=self.name, finish_reason="stop")

        return _gen()


class _FakeManager:
    def __init__(self, names):
        self.instances = {n: _FakeProvider(n) for n in names}

    def list_providers(self):
        return list(self.instances)

    def get(self, name=None):
        return self.instances[name or config.default_provider]


@pytest.fixture
def fake_providers(monkeypatch):
    names = [config.default_provider, "other"]
    manager = _FakeManager(names)
    monkeypatch.setattr(llm_mod, "get_provider_manager", lambda: manager)
    return manager


@pytest.mark.asyncio
async def test_provider_is_per_call_not_sticky(fake_providers):
    client = llm_mod.LLMClient()
    msgs = [{"role": "user", "content": "hi"}]

    assert await client.chat(msgs) == f"{config.default_provider}:hi"
    assert await client.chat(msgs, provider="other") == "other:hi"

    # 关键断言：下一次不传 provider，必须回到默认，而不是继续走 other
    assert await client.chat(msgs) == f"{config.default_provider}:hi"


@pytest.mark.asyncio
async def test_per_call_provider_does_not_change_instance_state(fake_providers):
    client = llm_mod.LLMClient()
    msgs = [{"role": "user", "content": "x"}]

    default_before = client._provider
    await client.chat(msgs, provider="other")

    assert client._provider is default_before
    assert client._provider_name is None


@pytest.mark.asyncio
async def test_stream_with_provider_uses_it_without_leaking(fake_providers):
    client = llm_mod.LLMClient()
    msgs = [{"role": "user", "content": "x"}]

    stream = await client.chat(msgs, provider="other", stream=True)
    chunks = [c async for c in stream]
    assert chunks == ["other"]

    # 流式用完，默认 provider 仍是默认
    assert await client.chat(msgs) == f"{config.default_provider}:x"


@pytest.mark.asyncio
async def test_chat_with_retry_keeps_provider_scoped(fake_providers):
    client = llm_mod.LLMClient()
    msgs = [{"role": "user", "content": "y"}]

    out = await client.chat_with_retry(msgs, provider="other")
    assert out == "other:y"
    assert await client.chat(msgs) == f"{config.default_provider}:y"
