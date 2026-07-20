"""M1-10 e2e 测试夹具。

职责:
- 隔离运行时数据(skills/user、memory/teachings)到 tmp_path
- 提供 FakeLLMProvider,按 prompt 顺序返回脚本化响应
- 每个用例前后重置所有全局单例,避免跨用例污染
- 让 e2e 完全离线、不依赖 OPENAI_API_KEY 或真实网络
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

# 把项目根加入 sys.path,与顶层 tests/conftest.py 一致
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 测试用 .env(避免污染真实配置)
ENV_PATH = ROOT / ".env"
if not ENV_PATH.exists():
    ENV_PATH.write_text(
        "OPENAI_API_KEY=sk-test-dummy\n"
        "OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "OPENAI_MODEL=gpt-4o-mini\n"
        "LOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )


class ScriptedLLMProvider:
    """可脚本化的 Fake LLM Provider。

    用法:
        provider = ScriptedLLMProvider()
        provider.queue({"intent": "skill", "selected_skill": "DailyReport", "tool_tasks": []})
        provider.queue({"name": "DailyReport", "method": "...", ...})

        每次 chat() 调用会消费队列头,直接返回该 JSON 字符串。
        队列耗尽时,返回包含完整 JSON 的固定回复(避免测试报错)。
    """

    name = "fake"
    supports_streaming = True

    def __init__(self) -> None:
        self.calls: List[List[Dict[str, str]]] = []
        self._queue: List[str] = []
        self._default_json: Dict[str, Any] = {
            "intent": "skill",
            "selected_skill": "DailyReport",
            "is_retry": False,
            "tool_tasks": [],
        }

    def queue(self, payload: Any) -> None:
        """入队一个响应。payload 可以是 dict(自动序列化为 JSON 字符串)或 str。"""
        import json
        if isinstance(payload, str):
            self._queue.append(payload)
        else:
            self._queue.append(json.dumps(payload, ensure_ascii=False))

    def queue_many(self, payloads: List[Any]) -> None:
        for p in payloads:
            self.queue(p)

    def _next(self, messages: List[Dict[str, str]]) -> str:
        # 记录每次调用
        self.calls.append(messages)
        if self._queue:
            return self._queue.pop(0)
        # 兜底:返回默认 JSON,避免 _try_parse_json 报错
        import json
        return json.dumps(self._default_json, ensure_ascii=False)

    async def chat(self, messages, **kw):
        return self._next(messages)

    def chat_stream(self, messages, **kw):
        """直接返回 AsyncIterator(与 OpenAIProvider 等真实 provider 一致)。

        注意:BaseProvider 协议上 chat_stream 是 async def,但实际语义是返回一个异步
        生成器。infra/llm.py 的 _stream() 用 ``async for delta in self.provider.chat_stream(...)``
        迭代,因此 provider 的 chat_stream 必须是普通方法返回 async iterator,而非
        async def(否则拿到的就是 coroutine 而非 iterator)。
        """
        text = self._next(messages)
        from infra.providers.base import ChatDelta

        async def _gen():
            yield ChatDelta(content=text, finish_reason="stop")

        return _gen()


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    """隔离运行时目录到 tmp_path。

    返回值是一个 dict,提供:
      - skills_path:    SkillStore 的 base_path(里面有 user/ 子目录)
      - teachings_path: TeachingSessionStore 的 base_path
      - memory_path:    memory 的 base_path(放 failures/successes 等)
      - tmp:            tmp_path 自身
    """
    skills_path = tmp_path / "skills"
    skills_path.mkdir(parents=True, exist_ok=True)
    (skills_path / "user").mkdir(parents=True, exist_ok=True)
    (skills_path / "builtin").mkdir(parents=True, exist_ok=True)

    teachings_path = tmp_path / "teachings"
    teachings_path.mkdir(parents=True, exist_ok=True)

    memory_path = tmp_path / "memory"
    memory_path.mkdir(parents=True, exist_ok=True)

    # 1) 重置所有可能受影响的全局单例
    from infra import llm as llm_mod
    from infra import logger as logger_mod
    from skills import manager as skill_mod
    from agents import teaching_session as ts_mod
    from tools.hub import reset_tool_hub
    from tools.base import reset_tool_registry

    llm_mod.reset_llm_client()
    logger_mod.get_logger().__init__()
    skill_mod.reset_skill_store()
    ts_mod.reset_teaching_store()
    reset_tool_registry()
    reset_tool_hub()

    yield {
        "skills_path": skills_path,
        "teachings_path": teachings_path,
        "memory_path": memory_path,
        "tmp": tmp_path,
    }

    # 清理
    skill_mod.reset_skill_store()
    ts_mod.reset_teaching_store()
    llm_mod.reset_llm_client()
    reset_tool_registry()
    reset_tool_hub()


@pytest.fixture
def fake_llm(monkeypatch):
    """注入 ScriptedLLMProvider 并把它接到 ProviderManager。

    返回 ScriptedLLMProvider 实例,允许测试在执行前 queue 脚本响应。
    """
    from infra.providers.manager import get_provider_manager, reset_provider_manager
    from infra import llm as llm_mod
    from infra.config import config as cfg_singleton

    # 确保 ProviderManager 重置干净
    reset_provider_manager()
    llm_mod.reset_llm_client()

    provider = ScriptedLLMProvider()
    pm = get_provider_manager()
    pm.register(
        "fake",
        ScriptedLLMProvider,  # type: ignore[arg-type]
        config={},
        set_current=True,
    )
    pm._instances["fake"] = provider  # 直接复用我们的脚本化实例

    # 让 Config singleton 知道 default_provider=fake,
    # 否则 LLMClient 会去查 config.default_provider(默认 "openai")而抛错。
    # pydantic 不允许任意 attribute 写入,用 object.__setattr__ 绕过 __setattr__ 校验。
    monkeypatch.setattr(cfg_singleton, "default_provider", "fake")

    yield provider

    reset_provider_manager()
    llm_mod.reset_llm_client()
