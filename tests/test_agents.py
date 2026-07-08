"""Agent 单元测试 - mock LLM,不依赖真实 API"""
import asyncio
import json
from typing import Any, Dict

import pytest

from infra.llm import LLMClient


# ===== Mock LLM =====

class FakeLLM(LLMClient):
    """替换真正的 LLM,返回预设响应"""
    def __init__(self, scripted: list):
        # 不调父类 __init__(避免 OpenAI client 实例化)
        self.model = "fake"
        self.default_temperature = 0.0
        self._scripted = list(scripted)

    async def chat_with_retry(self, messages, **kw):
        if not self._scripted:
            return "{}"
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def _complete(self, messages, temperature=None):
        return await self.chat_with_retry(messages)

    async def _stream(self, messages, temperature=None):
        for ch in "streamed":
            yield ch

    async def stream(self, messages, **kw):
        async for c in self._stream(messages):
            yield c


def _patch_llm(monkeypatch, scripted):
    fake = FakeLLM(scripted)
    # BaseAgent.__init__ 里 `from infra.llm import get_llm_client`,
    # 该名字绑定在 core.agent_base 模块里。要同时 patch 两边。
    monkeypatch.setattr("infra.llm.get_llm_client", lambda: fake)
    monkeypatch.setattr("core.agent_base.get_llm_client", lambda: fake)
    # 清掉可能已经缓存的实例
    from infra import llm as _llm_mod
    _llm_mod._llm_client = None
    return fake


# ===== Manager =====

@pytest.mark.asyncio
async def test_manager_plan_parses_valid_json(monkeypatch):
    valid = json.dumps({
        "intent": "查天气",
        "selected_skill": "",
        "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
    }, ensure_ascii=False)
    _patch_llm(monkeypatch, [valid])

    from agents.manager import ManagerAgent
    m = ManagerAgent()
    plan = await m.plan("厦门天气")
    assert plan.intent == "查天气"
    assert len(plan.tool_tasks) == 1
    assert plan.tool_tasks[0]["type"] == "weather_query"


@pytest.mark.asyncio
async def test_manager_plan_invalid_then_valid(monkeypatch):
    """第一次坏 JSON, 第二次好 JSON: 应重试成功"""
    bad = "不是 JSON 啊 {"
    good = json.dumps({"intent": "x", "tool_tasks": []}, ensure_ascii=False)
    _patch_llm(monkeypatch, [bad, good])

    from agents.manager import ManagerAgent
    m = ManagerAgent()
    plan = await m.plan("hi")
    assert plan.intent == "x"


@pytest.mark.asyncio
async def test_manager_plan_all_fail_degrades(monkeypatch):
    """全部失败时降级为空规划"""
    _patch_llm(monkeypatch, ["bad", "bad", "bad", "bad"])
    from agents.manager import ManagerAgent
    m = ManagerAgent()
    plan = await m.plan("hi")
    assert plan.tool_tasks == []


def test_manager_should_answer_directly():
    from agents.manager import ManagerAgent
    m = ManagerAgent()
    assert m.should_answer_directly("你好")
    assert m.should_answer_directly("hello")
    assert not m.should_answer_directly("查厦门天气")


def test_manager_should_learn_skill():
    from agents.manager import ManagerAgent
    m = ManagerAgent()
    assert m.should_learn_skill("以后应该先查天气")
    assert not m.should_learn_skill("查厦门天气")


# ===== Orchestrator =====

@pytest.mark.asyncio
async def test_orchestrator_stream(monkeypatch):
    _patch_llm(monkeypatch, ["ok"])
    from agents.orchestrator import OrchestratorAgent
    from tools.base import ToolResult
    o = OrchestratorAgent()
    chunks = []
    async for c in o.stream("hello", {}, None):
        chunks.append(c)
    assert chunks == ["s", "t", "r", "e", "a", "m", "e", "d"]


# ===== SkillTrainer =====

@pytest.mark.asyncio
async def test_trainer_heuristic_no_trigger():
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok, conf, reason = await t.detect("查厦门天气")
    assert ok is False


@pytest.mark.asyncio
async def test_trainer_extract_returns_skill(monkeypatch, tmp_path):
    from skills.manager import reset_skill_store
    reset_skill_store()

    extract = json.dumps({
        "name": "demo",
        "method": "do it",
        "capability": "can do",
        "patterns": ["demo"],
        "tags": ["t"],
        "steps": [{"id": "s1", "name": "do", "description": "do"}],
    }, ensure_ascii=False)
    _patch_llm(monkeypatch, [
        json.dumps({"is_teaching": True, "confidence": 0.9, "reason": "ok"}),
        extract,
    ])

    # 把 user 目录指到 tmp_path
    import skills.manager as mgr
    mgr._store = mgr.SkillStore(str(tmp_path / "skills"))

    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    t.skill_store = mgr.get_skill_store(str(tmp_path / "skills"))

    ok, msg, skill = await t.teach("以后做 demo,应该这样")
    assert ok is True
    assert skill.name == "demo"
    assert skill.source == "taught"
    # 写入文件
    files = list((tmp_path / "skills" / "user").glob("demo@*.yaml"))
    assert len(files) == 1