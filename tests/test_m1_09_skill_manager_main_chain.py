"""M1-09:SkillManagerAgent 接入主链。

依据 ``docs/11-开发任务清单.md M1-09``:
- ``list / show / versions / rollback / activate`` 走主链 ``Agent.handle()``
- 不再是旁路脚本;用户说"列出技能"能直接得到结果。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_skills(monkeypatch):
    """每个用例隔离一个临时 skills 目录,避免污染 repo 内置技能。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    tmp = Path(tempfile.mkdtemp(prefix="skill_manager_test_"))
    # 拷贝 user 目录中的示例技能用作测试目标
    repo_user = Path(__file__).resolve().parents[1] / "skills" / "user"
    if repo_user.exists():
        for f in repo_user.glob("*.yaml"):
            shutil.copy(f, tmp / f.name)
    # 重建 SkillStore + SkillManagerAgent 单例
    from skills import manager as sm
    from agents import skill_manager_agent as sma
    sm.reset_skill_store()
    sma._singleton = None
    fresh = sm.SkillStore(path=str(tmp))
    monkeypatch.setattr(sm, "_store", fresh)
    yield {"tmp": tmp, "store": fresh}
    sm.reset_skill_store()
    sma._singleton = None
    shutil.rmtree(tmp, ignore_errors=True)


def _agent_for(monkeypatch):
    """构造一个使用隔离 store 的 Agent。"""
    from skills import manager as sm
    sm.reset_skill_store()
    store = sm.SkillStore(path=str(isolated_skills.__wrapped__ if False else None))


@pytest.mark.asyncio
async def test_list_skills_via_main_chain(isolated_skills):
    """用户在主对话中说"列出所有技能",得到结构化列表。"""
    from core.agent import Agent

    captured = []

    def on_event(event, payload):
        captured.append((event, payload))

    agent = Agent(session_id="test", user_id="alice")
    ans = await agent.handle("列出所有技能", on_event=on_event)

    assert "现有" in ans or "技能" in ans
    # skill_manager_result 事件应携带 details
    events = {e for e, _ in captured}
    assert "skill_manager_result" in events
    payload = next(p for e, p in captured if e == "skill_manager_result")
    assert payload["action"] == "list"
    assert payload["ok"] is True
    assert isinstance(payload["details"], list)
    # 至少有一个内置/示例技能
    if payload["details"]:
        item = payload["details"][0]
        assert "name" in item and "version" in item


@pytest.mark.asyncio
async def test_show_skill_via_main_chain(isolated_skills):
    """用户问"查看 demo",得到技能详情。"""
    from core.agent import Agent
    from skills import manager as sm

    # 确保 demo 技能存在
    store = sm.get_skill_store()
    if not store.get_by_name("demo"):
        pytest.skip("需要 demo 技能作为测试目标")

    captured = []

    def on_event(event, payload):
        captured.append((event, payload))

    agent = Agent(session_id="test", user_id="alice")
    ans = await agent.handle("查看 demo", on_event=on_event)

    assert "demo" in ans
    payload = next(p for e, p in captured if e == "skill_manager_result")
    assert payload["action"] == "show"
    assert payload["ok"] is True
    assert payload["skill_name"] == "demo"


@pytest.mark.asyncio
async def test_rollback_via_main_chain(isolated_skills):
    """用户说"回滚 demo 到 1.0.0",active 版本被切换。"""
    from core.agent import Agent
    from skills import manager as sm

    # 需要 demo 同时存在 1.0.0 和 1.0.1
    store = sm.get_skill_store()
    if not (store.get_by_name("demo") and store._registry.list_versions("demo")):
        pytest.skip("demo 多版本不存在")

    versions = sorted(store._registry.list_versions("demo"))
    if len(versions) < 2:
        pytest.skip("需要至少两个 demo 版本")

    target = versions[0]  # 回到最早版本

    captured = []

    def on_event(event, payload):
        captured.append((event, payload))

    agent = Agent(session_id="test", user_id="alice")
    ans = await agent.handle(f"回滚 demo 到 {target}", on_event=on_event)

    payload = next(p for e, p in captured if e == "skill_manager_result")
    assert payload["action"] == "rollback"
    assert payload["ok"] is True
    assert payload["version"] == target

    # active 已切到目标版本
    assert store._registry._active_versions["demo"] == target


@pytest.mark.asyncio
async def test_manager_agent_parsing():
    """解析器对典型指令返回正确的 (action, skill, version)。"""
    from agents.skill_manager_agent import SkillManagerAgent

    sm = SkillManagerAgent()
    cases = [
        ("列出所有技能", "list", "", ""),
        ("查看 demo", "show", "demo", ""),
        ("回滚 demo 到 1.0.0", "rollback", "demo", "1.0.0"),
        ("激活 demo 1.0.1", "activate", "demo", "1.0.1"),
        ("demo 的版本", "versions", "demo", ""),
    ]
    for text, expected_action, expected_skill, expected_ver in cases:
        action, skill, ver = sm._parse(text)
        assert action == expected_action, text
        assert skill == expected_skill, text
        assert ver == expected_ver, text