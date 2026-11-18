"""工具自修复 —— agent 自己造的工具反复失败时，让它自己改一版。

## 硬边界

内置工具是**我们的代码**，绝不允许 agent 自动改写（那等于让它改自己的权限边界）。
所以只修"能找到 create_tool 提案"的工具。

## 闸门

修完必须过两道：`validate_proposal`（语法/安全检查）+ 跑 `test_cases`。
**测试不过就不替换** —— 宁可保留坏的旧版，也不要换上一个更坏的新版。
"""
import json

import pytest

import core.tool_health as th
import core.tool_repair as tr
from tools.proposal import STATUS_PUBLISHED, ToolProposal, ToolProposalStore


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def chat_with_retry(self, messages, **kwargs):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeHub:
    def __init__(self, names=None):
        self._tools = {n: object() for n in (names or [])}

    def names(self):
        return list(self._tools)

    def register_python_tool(self, tool):
        self._tools[tool.name] = tool

    def unregister_tool(self, name):
        return self._tools.pop(name, None) is not None


@pytest.fixture
def env(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr("tools.script_writer.workspace_dir", lambda: ws)

    health = th.ToolHealthStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(th, "_store", health)

    store = ToolProposalStore(base_path=tmp_path / "proposals")
    return health, store, ws


def _publish(store, monkeypatch, name="my_tool", impl="return {'ok': True}",
             test_cases=None):
    p = ToolProposal(
        name=name, description="测试工具", kind="code",
        implementation=impl, test_cases=test_cases or [],
        status=STATUS_PUBLISHED,
    )
    store.save(p)
    import tools.proposal as prop
    monkeypatch.setattr(prop, "_store", store)
    return p


def _break(health, name="my_tool", error="ValueError: boom"):
    for _ in range(th.CONSECUTIVE_FAIL_LIMIT):
        health.record(name, False, error=error)


@pytest.mark.asyncio
async def test_refuses_builtin_tool(env, monkeypatch):
    """没有提案 = 不是 agent 造的 = 不动源码。"""
    health, store, _ws = env
    import tools.proposal as prop
    monkeypatch.setattr(prop, "_store", store)
    _break(health, "video_concat")

    r = await tr.repair_tool("video_concat", hub=FakeHub(["video_concat"]))
    assert r.attempted is False
    assert "不是 agent 造" in r.reason


@pytest.mark.asyncio
async def test_skips_healthy_tool(env, monkeypatch):
    health, store, _ws = env
    _publish(store, monkeypatch)
    for _ in range(5):
        health.record("my_tool", True)

    r = await tr.repair_tool("my_tool", hub=FakeHub(["my_tool"]))
    assert r.attempted is False
    assert "健康" in r.reason


@pytest.mark.asyncio
async def test_skips_when_samples_insufficient(env, monkeypatch):
    health, store, _ws = env
    _publish(store, monkeypatch)
    health.record("my_tool", False, error="once")

    r = await tr.repair_tool("my_tool", hub=FakeHub(["my_tool"]))
    assert r.attempted is False
    assert "样本不足" in r.reason


@pytest.mark.asyncio
async def test_publishes_fixed_version(env, monkeypatch):
    health, store, _ws = env
    _publish(store, monkeypatch, impl="raise ValueError('boom')")
    _break(health)

    llm = FakeLLM(json.dumps({"implementation": "return {'ok': False}"}))
    hub = FakeHub(["my_tool"])
    r = await tr.repair_tool("my_tool", hub=hub, llm=llm)

    assert r.published is True, r.reason
    assert r.revision == 1
    assert "my_tool" in hub.names()
    # 提案被更新并标记了来源
    saved = store.get(_publish_id(store, "my_tool"))
    assert saved.validation.get("revision") == 1
    assert saved.reviewed_by == "auto-repair"


@pytest.mark.asyncio
async def test_rejects_fix_that_fails_validation(env, monkeypatch):
    """危险代码不能因为"能修"就放行。"""
    health, store, _ws = env
    _publish(store, monkeypatch)
    _break(health)

    llm = FakeLLM(json.dumps({"implementation": "import subprocess\nsubprocess.run(['ls'])"}))
    r = await tr.repair_tool("my_tool", hub=FakeHub(["my_tool"]), llm=llm)

    assert r.published is False
    assert "校验" in r.reason


@pytest.mark.asyncio
async def test_rejects_fix_that_fails_tests(env, monkeypatch):
    health, store, _ws = env
    _publish(store, monkeypatch,
             test_cases=[{"params": {}, "expect_success": True}])
    _break(health)

    # 这段代码会抛异常 → 测试用例失败
    llm = FakeLLM(json.dumps({"implementation": "raise RuntimeError('still broken')"}))
    r = await tr.repair_tool("my_tool", hub=FakeHub(["my_tool"]), llm=llm)

    assert r.published is False
    assert "测试未通过" in r.reason


@pytest.mark.asyncio
async def test_environment_problem_is_reported_not_patched(env, monkeypatch):
    """如果是"缺外部程序/缺密钥"，改代码没用 —— 要记成能力缺口。"""
    health, store, _ws = env
    _publish(store, monkeypatch)
    _break(health)

    llm = FakeLLM(json.dumps({"unfixable": "缺少 ffmpeg 可执行文件"}))
    r = await tr.repair_tool("my_tool", hub=FakeHub(["my_tool"]), llm=llm)

    assert r.published is False
    assert "环境问题" in r.reason
    gaps = health.list_gaps()
    assert gaps and "ffmpeg" in gaps[0]["need"]


@pytest.mark.asyncio
async def test_bad_llm_output_is_handled(env, monkeypatch):
    health, store, _ws = env
    _publish(store, monkeypatch)
    _break(health)

    r = await tr.repair_tool("my_tool", hub=FakeHub(["my_tool"]),
                             llm=FakeLLM("这是一段废话，不是 JSON"))
    assert r.published is False
    assert "JSON" in r.reason


@pytest.mark.asyncio
async def test_auto_repair_caps_attempts(env, monkeypatch):
    health, store, _ws = env
    for name in ("a_tool", "b_tool", "c_tool"):
        _publish(store, monkeypatch, name=name)
        _break(health, name)

    called = []

    async def fake_repair(name, **kw):
        called.append(name)
        return tr.RepairResult(tool=name, attempted=True, reason="stub")

    monkeypatch.setattr(tr, "repair_tool", fake_repair)
    results = await tr.auto_repair(max_repairs=1)
    assert len(results) == 1
    assert len(called) == 1


def _publish_id(store, name):
    for p in store.list():
        if p.name == name:
            return p.id
    return ""
