"""方法论自增长 —— 从「做成了」里沉淀技能。

## 为什么这条测试重要

用户对系统的定位是四根柱子：习惯（长期记忆）+ 短期记忆 + **方法论** + 工具。
「工具自增长」做完之后，方法论还是只能靠人教（"教我…"），
于是出现不对称：会给自己造工具，却不会给自己攒方法论。

## 这里钉住的三条防线

1. **只从成功里学**
2. **只从"有实质过程"里学**（≥2 个工具 或 ≥2 轮）——
   一问一答不配叫方法论
3. **默认不生效**：提炼出来进草稿队列，人批准才进技能库
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from core.skill_harvest import (
    MIN_DISTINCT_TOOLS,
    HarvestResult,
    SkillDraftStore,
    _build_trajectory,
    _tool_sequence,
    approve_draft,
    build_skill_from_plan,
    harvest,
)


# ============================================================
# 测试替身
# ============================================================

@dataclass
class FakeResult:
    success: bool = True
    data: Any = None
    error: str = ""


@dataclass
class FakeLLM:
    reply: str = ""
    calls: List[Any] = field(default_factory=list)

    async def chat_with_retry(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeSkillStore:
    def __init__(self, existing=None):
        self.existing = dict(existing or {})
        self.added = []

    def get_by_name(self, name):
        return self.existing.get(name)

    def add(self, skill, set_active=False):
        self.added.append((skill, set_active))
        self.existing[skill.name] = skill
        return skill.id


GOOD_PAYLOAD = {
    "name": "竞品调研",
    "capability": "快速摸清一个陌生产品的关键信息",
    "method": (
        "第一步,先用搜索确认官方站点和文档入口;"
        "第二步,抓取官网首页和定价页,提取定位与价格档位;"
        "第三步,搜索第三方评测,补充独立视角的优缺点;"
        "第四步,把四处信息对齐成一张对比表再下结论。"
    ),
    "patterns": ["调研一下这个产品", "看看竞品", "帮我做个对比"],
}


def _results(n=2):
    return {f"t{i}": FakeResult(data={"ok": i}) for i in range(1, n + 1)}


def _specs(*tools):
    return {f"t{i + 1}": {"type": t, "params": {"q": "x"}} for i, t in enumerate(tools)}


@pytest.fixture
def drafts(tmp_path):
    return SkillDraftStore(base_path=tmp_path / "drafts")


# ============================================================
# 1. 闸门：够不够格当方法论
# ============================================================

@pytest.mark.asyncio
async def test_skips_trivial_task(drafts):
    """一问一答不配叫方法论。"""
    out = await harvest(
        "今天天气怎么样",
        {"t1": FakeResult()},
        rounds=1,
        task_specs=_specs("weather_query"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
    )
    assert out.skipped is True
    assert out.skill is None
    assert "简单" in out.reason


@pytest.mark.asyncio
async def test_skips_when_nothing_succeeded(drafts):
    """失败的轨迹没有复用价值。"""
    results = {"t1": FakeResult(success=False), "t2": FakeResult(success=False)}
    out = await harvest(
        "做个调研",
        results,
        rounds=2,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
    )
    assert out.skipped is True
    assert "成功" in out.reason


@pytest.mark.asyncio
async def test_harvests_multi_tool_task(drafts):
    """≥2 个不同工具 → 值得沉淀。"""
    llm = FakeLLM(json.dumps(GOOD_PAYLOAD))
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        store=FakeSkillStore(),
        llm=llm,
    )
    assert out.skill is not None, out.reason
    assert out.skill.name == "竞品调研"
    assert llm.calls, "没有调用 LLM 提炼"


@pytest.mark.asyncio
async def test_harvests_multi_round_task(drafts):
    """轮次够多也算有实质过程（哪怕只用一个工具）。"""
    out = await harvest(
        "帮我反复查清楚这件事",
        _results(1),
        rounds=3,
        task_specs=_specs("web_search"),
        drafts=drafts,
        store=FakeSkillStore(),
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
    )
    assert out.skill is not None, out.reason


# ============================================================
# 2. 提炼质量
# ============================================================

def test_steps_come_from_real_tools():
    """steps 必须是**真实执行过的工具**，不是模型编的。"""
    skill = build_skill_from_plan(
        GOOD_PAYLOAD,
        user_input="调研一下",
        tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    assert [s.tool for s in skill.steps] == ["web_search", "web_fetch"]


def test_regression_generated_from_real_run():
    """回归样例来自这次真实成功 —— 技能一出生就带验证。"""
    skill = build_skill_from_plan(
        GOOD_PAYLOAD,
        user_input="调研一下竞品 X",
        tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    assert skill.regression
    case = skill.regression[0]
    assert case["input"]["user_input"] == "调研一下竞品 X"
    assert case["expect"]["success"] is True


def test_rejects_short_method():
    bad = dict(GOOD_PAYLOAD, method="先搜再写")
    assert build_skill_from_plan(bad, user_input="x", tool_results=_results(2)) is None


def test_rejects_missing_patterns():
    bad = dict(GOOD_PAYLOAD, patterns=[])
    assert build_skill_from_plan(bad, user_input="x", tool_results=_results(2)) is None


def test_rejects_missing_name():
    bad = dict(GOOD_PAYLOAD, name="")
    assert build_skill_from_plan(bad, user_input="x", tool_results=_results(2)) is None


@pytest.mark.asyncio
async def test_skips_when_same_name_exists(drafts):
    """不自动覆盖已有技能 —— 升级是人的决定。"""
    out = await harvest(
        "调研一下",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        store=FakeSkillStore(existing={"竞品调研": object()}),
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
    )
    assert out.skipped is True
    assert "同名" in out.reason


@pytest.mark.asyncio
async def test_handles_bad_llm_output(drafts):
    out = await harvest(
        "调研一下",
        _results(2),
        rounds=1,
        task_specs=_specs("a", "b"),
        drafts=drafts,
        store=FakeSkillStore(),
        llm=FakeLLM("我觉得这个任务挺好的，没什么可总结的"),
    )
    assert out.skill is None
    assert "JSON" in out.reason


@pytest.mark.asyncio
async def test_handles_llm_exception(drafts):
    out = await harvest(
        "调研一下",
        _results(2),
        rounds=1,
        task_specs=_specs("a", "b"),
        drafts=drafts,
        store=FakeSkillStore(),
        llm=FakeLLM(RuntimeError("API 挂了")),
    )
    assert out.skill is None
    assert "失败" in out.reason


def test_extract_json_tolerates_fence():
    from core.skill_harvest import _extract_json

    fenced = "好的，这是结果：\n```json\n{\"name\": \"x\"}\n```\n希望有用"
    assert _extract_json(fenced) == {"name": "x"}


# ============================================================
# 3. 隐私：轨迹里不能带参数值
# ============================================================

def test_trajectory_excludes_param_values():
    """参数值里可能有用户隐私（地址、账号），只能写参数**名**。"""
    text = _build_trajectory(
        {"t1": FakeResult()},
        {"t1": {"type": "weather_query", "params": {"city": "北京市朝阳区某小区"}}},
    )
    assert "weather_query" in text
    assert "city" in text
    assert "朝阳区" not in text, "参数值泄漏到提炼提示词里了"


def test_tool_sequence_dedupes():
    seq = _tool_sequence(
        {"t1": FakeResult(), "t2": FakeResult()},
        {"t1": {"type": "web_search"}, "t2": {"type": "web_search"}},
    )
    assert seq == ["web_search"]


# ============================================================
# 4. 草稿队列 + 批准
# ============================================================

def test_draft_store_roundtrip(drafts):
    skill = build_skill_from_plan(
        GOOD_PAYLOAD, user_input="x", tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    draft_id = drafts.save(skill, origin="调研一下")
    assert drafts.get(draft_id) is not None

    items = drafts.list()
    assert len(items) == 1
    assert items[0]["name"] == skill.name
    assert items[0]["origin"] == "调研一下"


def test_draft_delete(drafts):
    skill = build_skill_from_plan(
        GOOD_PAYLOAD, user_input="x", tool_results=_results(2),
    )
    draft_id = drafts.save(skill)
    assert drafts.delete(draft_id) is True
    assert drafts.get(draft_id) is None


def test_approve_draft_activates_skill(drafts, monkeypatch):
    """批准之后要**激活**（set_active=True），否则 planner 永远看不到。"""
    import core.skill_harvest as mod

    monkeypatch.setattr(mod, "_draft_store", drafts)
    skill = build_skill_from_plan(
        GOOD_PAYLOAD, user_input="x", tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    draft_id = drafts.save(skill)

    store = FakeSkillStore()
    ok, msg = approve_draft(draft_id, store=store)
    assert ok, msg
    assert store.added and store.added[0][1] is True, "没有 set_active=True"
    assert drafts.get(draft_id) is None, "批准后草稿应被清掉"


def test_approve_missing_draft(drafts, monkeypatch):
    import core.skill_harvest as mod

    monkeypatch.setattr(mod, "_draft_store", drafts)
    ok, msg = approve_draft("nope", store=FakeSkillStore())
    assert ok is False
    assert "不存在" in msg


def test_approve_refuses_duplicate_name(drafts, monkeypatch):
    import core.skill_harvest as mod

    monkeypatch.setattr(mod, "_draft_store", drafts)
    skill = build_skill_from_plan(GOOD_PAYLOAD, user_input="x", tool_results=_results(2))
    draft_id = drafts.save(skill)

    ok, msg = approve_draft(draft_id, store=FakeSkillStore(existing={skill.name: object()}))
    assert ok is False
    assert "同名" in msg


def test_harvest_result_bool_and_dict():
    r = HarvestResult(skipped=True, reason="太简单")
    assert not r
    assert r.to_dict()["harvested"] is False
    assert r.to_dict()["reason"] == "太简单"


# ============================================================
# 5. 接口
# ============================================================

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import core.skill_harvest as mod
    from fastapi.testclient import TestClient

    store = SkillDraftStore(base_path=tmp_path / "drafts")
    monkeypatch.setattr(mod, "_draft_store", store)

    import backend.main as bm

    with TestClient(bm.app) as c:
        yield c, store


def test_list_drafts_api(api_client):
    c, store = api_client
    skill = build_skill_from_plan(GOOD_PAYLOAD, user_input="x", tool_results=_results(2))
    store.save(skill, origin="调研")

    r = c.get("/api/skill-drafts")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["drafts"][0]["name"] == skill.name


def test_approve_draft_api(api_client, monkeypatch):
    c, store = api_client
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    monkeypatch.setattr("core.skill_harvest.approve_draft", lambda draft_id: (True, "ok"))

    skill = build_skill_from_plan(GOOD_PAYLOAD, user_input="x", tool_results=_results(2))
    store.save(skill)
    r = c.post(f"/api/skill-drafts/{skill.id}/approve")
    assert r.status_code == 200


def test_delete_draft_api(api_client, monkeypatch):
    c, store = api_client
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    skill = build_skill_from_plan(GOOD_PAYLOAD, user_input="x", tool_results=_results(2))
    store.save(skill)

    r = c.delete(f"/api/skill-drafts/{skill.id}")
    assert r.status_code == 200
    assert store.get(skill.id) is None
