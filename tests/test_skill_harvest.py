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
from typing import Any, List

import pytest

from core.skill_harvest import (
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
    "worth_keeping": True,
    "capability_key": "competitor_research",
    "name": "竞品调研",
    "capability": "快速摸清一个陌生产品的关键信息",
    "method": (
        "先判断这是不是「同一类」需求：如果目标是把陌生产品摸清，就先找到官方入口，"
        "再看定价页判断定位与档位；如果只有第三方来源，必须交叉两个以上再下结论。"
        "常见的坑是把营销话术当事实 —— 官网自称「领先」不能进结论，要能对上可验证的数字。"
        "完成标准是一句话能说清它的定位、价格档位和主要短板，缺任何一项都不算做完。"
    ),
    "patterns": ["调研一下这个产品", "看看竞品", "帮我做个对比"],
}


def _prime(ledger, key="competitor_research", times=2, corrected=0, **extra):
    """预置 n 次同类观察 —— 让"第 n+1 次"够格晋升。"""
    for i in range(times):
        ledger.record(
            key,
            title=extra.get("title", "竞品调研"),
            capability=extra.get("capability", "快速摸清一个陌生产品"),
            method=extra.get("method", GOOD_PAYLOAD["method"]),
            patterns=extra.get("patterns", GOOD_PAYLOAD["patterns"]),
            tools=["web_search", "web_fetch"],
            user_input=f"第 {i + 1} 次同类任务",
            corrected=i < corrected,
        )
    if extra.get("bind"):
        ledger.bind_skill(
            key,
            skill_id=extra.get("skill_id", "skill_old"),
            skill_name=extra.get("skill_name", "竞品调研"),
            version=extra.get("version", "1.0.0"),
        )
    return ledger


def _results(n=2):
    return {f"t{i}": FakeResult(data={"ok": i}) for i in range(1, n + 1)}


def _specs(*tools):
    return {f"t{i + 1}": {"type": t, "params": {"q": "x"}} for i, t in enumerate(tools)}


@pytest.fixture
def drafts(tmp_path):
    return SkillDraftStore(base_path=tmp_path / "drafts")


@pytest.fixture
def ledger(tmp_path):
    from core.skill_ledger import CompetenceLedger

    return CompetenceLedger(path=tmp_path / "ledger.json")


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
async def test_harvests_multi_tool_task(drafts, ledger):
    """≥2 个不同工具 + 同类攒够次数 → 够格产出草稿。"""
    _prime(ledger, times=2)
    llm = FakeLLM(json.dumps(GOOD_PAYLOAD))
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        store=FakeSkillStore(),
        llm=llm,
        ledger=ledger,
    )
    assert out.skill is not None, out.reason
    assert out.status == "drafted"
    assert out.competence_key == "competitor_research"
    assert out.skill.name == "竞品调研"
    assert llm.calls, "没有调用 LLM 提炼"


@pytest.mark.asyncio
async def test_harvests_multi_round_task(drafts, ledger):
    """轮次够多也算有实质过程（哪怕只用一个工具）。"""
    _prime(ledger, times=2)
    out = await harvest(
        "帮我反复查清楚这件事",
        _results(1),
        rounds=3,
        task_specs=_specs("web_search"),
        drafts=drafts,
        store=FakeSkillStore(),
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
        ledger=ledger,
    )
    assert out.skill is not None, out.reason


# ============================================================
# 1b. 晋升节奏：一次不算一类
# ============================================================

@pytest.mark.asyncio
async def test_first_observation_only_records(drafts, ledger):
    """第 1 次做完，只记账 —— 不产技能。

    这是这次重设计的核心：技能是「一类任务的做法」，
    一次任务只是它的一个样本。
    """
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
        ledger=ledger,
    )
    assert out.skill is None
    assert out.status == "accumulating"
    assert drafts.list() == [], "第一次就产草稿 = 又回到「一次一条」"
    comp = ledger.get("competitor_research")
    assert comp and len(comp["evidence"]) == 1


@pytest.mark.asyncio
async def test_second_observation_still_accumulating(drafts, ledger):
    """第 2 次还不够：两次可能只是同一批工作的两半。"""
    _prime(ledger, times=1)
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
        ledger=ledger,
    )
    assert out.status == "accumulating"
    assert drafts.list() == []
    assert len(ledger.get("competitor_research")["evidence"]) == 2


@pytest.mark.asyncio
async def test_correction_raises_the_bar(drafts, ledger):
    """被纠正过说明原来做法错了 —— 需要更多次干净成功才够格。"""
    _prime(ledger, times=2, corrected=1)  # 3 次里有 1 次被纠正
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
        ledger=ledger,
    )
    assert out.status == "accumulating", "4 次里有 1 次纠正，还不够"
    assert "纠正" in out.reason


@pytest.mark.asyncio
async def test_same_observation_still_counts(drafts, ledger):
    """「和上次一样」必须照常记账，而且照样能晋升。

    这是实测暴露的坑：第 3 次同类任务，模型判「完全同类、没有新判断」，
    如果把它当成「不值得沉淀」直接丢弃，这个档案就永远停在 2 次 ——
    做法越稳定，反而越沉淀不下来。
    """
    _prime(ledger, times=2)
    same = {
        "novelty": "same",
        "capability_key": "competitor_research",
        "name": "竞品调研",
        "capability": "快速摸清一个陌生产品",
    }
    out = await harvest(
        "再帮我调研一个竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(same)),
        ledger=ledger,
    )
    assert out.status == "drafted", out.reason
    assert out.skill.method, "判 same 时正文要从档案里沿用它，不能是空的"
    assert "同一类" in out.skill.method or "判断" in out.skill.method


@pytest.mark.asyncio
async def test_not_worth_is_not_recorded(drafts, ledger):
    """「这类事本身不值得沉淀」连账都不记 —— 否则档案表会被一次性任务塞满。"""
    payload = {
        "novelty": "not_worth",
        "capability_key": "one_off_convert",
        "reason": "一次性的临时转换",
    }
    out = await harvest(
        "把这份文件转成 pdf",
        _results(2),
        rounds=2,
        task_specs=_specs("read_multiple_files", "python_exec"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(payload)),
        ledger=ledger,
    )
    assert out.status == "skipped"
    assert "不值得沉淀" in out.reason
    assert ledger.all() == []
    assert drafts.list() == []


@pytest.mark.asyncio
async def test_distinct_capabilities_do_not_pollute(drafts, ledger):
    """不同能力互不计数 —— 否则「做过三件事」就冒充「一类事做了三次」。"""
    _prime(ledger, key="competitor_research", times=2)
    other = dict(GOOD_PAYLOAD, capability_key="ecom_listing_batch", name="批量上架")
    out = await harvest(
        "帮我批量上架这几个品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(other)),
        ledger=ledger,
    )
    assert out.status == "accumulating"
    assert len(ledger.get("ecom_listing_batch")["evidence"]) == 1
    assert len(ledger.get("competitor_research")["evidence"]) == 2


# ============================================================
# 1c. 晋升之后：产出的是修订，不是新技能
# ============================================================

@pytest.mark.asyncio
async def test_revision_instead_of_new_skill(drafts, ledger):
    """同一个档案已有技能 → 产出修订草稿，顶在原技能上，不新增一条。"""
    _prime(ledger, times=3, bind=True, skill_name="竞品调研", version="1.0.0")
    payload = dict(GOOD_PAYLOAD, revision_note="上一版说交叉来源即可；这次发现官网自述不能当事实")
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(payload)),
        ledger=ledger,
    )
    assert out.status == "revision", out.reason
    items = drafts.list()
    assert len(items) == 1
    assert items[0]["revision_of"] == "skill_old"
    assert items[0]["base_version"] == "1.0.0"
    assert items[0]["name"] == "竞品调研", "修订必须沿用原技能名，否则升版本找不到文件"


@pytest.mark.asyncio
async def test_repeat_without_new_judgement_is_not_a_revision(drafts, ledger):
    """「又做了一次，流程一样」不构成修订 —— 否则版本号会无限涨。"""
    _prime(ledger, times=3, bind=True)
    payload = dict(GOOD_PAYLOAD, revision_note="")
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(payload)),
        ledger=ledger,
    )
    assert out.status == "skipped"
    assert "不构成修订" in out.reason
    assert drafts.list() == []


@pytest.mark.asyncio
async def test_same_capability_keeps_one_pending_draft(drafts, ledger):
    """同一条档案重复够格，也只留一条待批准草稿，不越堆越多。"""
    _prime(ledger, times=2)
    for _ in range(2):
        await harvest(
            "帮我调研一下竞品",
            _results(2),
            rounds=1,
            task_specs=_specs("web_search", "web_fetch"),
            drafts=drafts,
            llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
            ledger=ledger,
        )
    assert len(drafts.list()) == 1


@pytest.mark.asyncio
async def test_rejects_bad_capability_key(drafts, ledger):
    """key 不是 snake_case → 拒绝，不入账（否则档案会碎成一堆同义 key）。"""
    payload = dict(GOOD_PAYLOAD, capability_key="竞品调研")
    out = await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(payload)),
        ledger=ledger,
    )
    assert out.status == "failed"
    assert "key" in out.reason
    assert ledger.all() == []


@pytest.mark.asyncio
async def test_prompt_lists_existing_capabilities(drafts, ledger):
    """已有档案要喂给模型 —— 不喂它就会另起一个 key，这正是过去失控的原因。"""
    _prime(ledger, key="short_drama_video_publish", times=1, title="短剧视频发布")
    llm = FakeLLM(json.dumps(GOOD_PAYLOAD))
    await harvest(
        "帮我调研一下竞品",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        llm=llm,
        ledger=ledger,
    )
    prompt = llm.calls[0][0]["content"]
    assert "short_drama_video_publish" in prompt
    assert "短剧视频发布" in prompt


# ============================================================
# 2. 提炼质量
# ============================================================

def test_harvest_produces_no_replayable_steps():
    """自动沉淀只给判断，不给可重放步骤。

    steps 曾由工具序列自动生成，但参数是空的（值在脱敏时删掉了）。
    planner 选中这种技能，DAG 会把 `web_search(空)` 一路跑到底全失败 ——
    技能库反而成了故障源。参数完整的 DAG 只能人教。
    """
    skill = build_skill_from_plan(
        GOOD_PAYLOAD,
        user_input="调研一下",
        tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    assert skill.steps == [], "自动沉淀不该产可重放步骤"
    assert skill.regression, "但这次真实成功要留作样例"
    assert [r["expect"]["tool"] for r in skill.regression] == ["web_search"]


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
async def test_skips_when_same_name_exists(drafts, ledger):
    """不自动覆盖已有技能 —— 升级是人的决定。"""
    _prime(ledger, times=2)
    out = await harvest(
        "调研一下",
        _results(2),
        rounds=1,
        task_specs=_specs("web_search", "web_fetch"),
        drafts=drafts,
        store=FakeSkillStore(existing={"竞品调研": object()}),
        llm=FakeLLM(json.dumps(GOOD_PAYLOAD)),
        ledger=ledger,
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

# ============================================================
# 6. 质量闸门：拦「执行日志的散文版」
# ============================================================
#
# 真实教训：15 条草稿里 9 条写成「第一步用 python_exec…；第二步用 publish_artifact…」，
# 换个场景一个字都用不上。工具顺序已经存在 steps 里，method 再念一遍毫无价值 ——
# 而且这类技能进库后会污染匹配、占注入预算。


def test_rejects_method_that_repeats_tool_names(drafts):
    """正文点名多个工具 → 这是调用日志，不是方法论。"""
    from core.skill_harvest import _build_skill_from_plan

    payload = dict(GOOD_PAYLOAD)
    payload["method"] = (
        "第一步，用 python_exec 生成产物；第二步，调用 publish_artifact 发布；"
        "第三步，再用 python_exec 复核结果，失败就重试直到 publish_artifact 成功。"
    )
    skill, why = _build_skill_from_plan(
        payload, user_input="x", tool_results=_results(2),
        task_specs=_specs("python_exec", "publish_artifact"),
    )
    assert skill is None
    assert "执行日志" in why, why


def test_warns_when_only_steps_without_judgement():
    """三步以上却一个判断都没有 → 提醒（不拒收，人来定）。"""
    from core.skill_harvest import assess_method_quality

    q = assess_method_quality(
        "第一步，打开官网；第二步，看定价页；第三步，搜一下评测；第四步，整理成表格。"
    )
    assert q["reject"] is False
    assert any("执行日志" in w for w in q["warnings"]), q["warnings"]


def test_flags_patterns_copied_from_origin():
    """触发词直接抄用户原话 → 以后只有原话能命中，等于没写。"""
    from core.skill_harvest import assess_method_quality

    q = assess_method_quality(
        "如果目标是选品，就先算清成本与佣金，再判断竞争度是否可接受。",
        origin="主要是选品工具 你觉得 你可以帮我干啥",
        patterns=["主要是选品工具"],
    )
    assert q["reject"] is False
    assert any("原话" in w for w in q["warnings"]), q["warnings"]


@pytest.mark.asyncio
async def test_legacy_worth_keeping_false_still_skips(drafts, ledger):
    """旧格式 worth_keeping=false 仍按「不值得沉淀」处理（兼容老模型输出）。"""
    payload = {"worth_keeping": False, "reason": "一次性的临时任务"}
    out = await harvest(
        "把这份文件转成 pdf",
        _results(2),
        rounds=2,
        task_specs=_specs("read_multiple_files", "python_exec"),
        drafts=drafts,
        llm=FakeLLM(json.dumps(payload)),
        ledger=ledger,
    )
    assert out.skipped is True
    assert out.skill is None
    assert "不值得沉淀" in out.reason
    assert drafts.list() == []


def test_trajectory_carries_failure_reason_without_secrets():
    """失败原因要带给提炼模型，但链接/密钥/路径必须脱敏。"""
    text = _build_trajectory(
        {
            "t1": FakeResult(success=False, error="连接 https://api.example.com/v1 失败 key=sk-abcdefgh12345678"),
            "t2": FakeResult(success=True, data={"ok": 1}),
        },
        {
            "t1": {"type": "publish_artifact", "params": {"path": "D:/x/y.mp4"}},
            "t2": {"type": "python_exec", "params": {"code": "print(1)"}},
        },
    )
    assert "失败" in text
    assert "publish_artifact" in text
    assert "example.com" not in text, "URL 泄漏到提炼提示词里了"
    assert "sk-abcdefgh12345678" not in text, "密钥泄漏到提炼提示词里了"
    assert "D:/x/y.mp4" not in text, "参数值泄漏到提炼提示词里了"


def test_approve_draft_persists_through_restart(tmp_path, drafts, monkeypatch):
    """批准后必须**落盘** —— 否则重启一次技能库又空了。"""
    import core.skill_harvest as mod
    from skills.manager import SkillStore

    monkeypatch.setattr(mod, "_draft_store", drafts)
    store = SkillStore(str(tmp_path / "skills"))
    monkeypatch.setattr(mod, "get_draft_store", lambda: drafts)

    skill = build_skill_from_plan(
        GOOD_PAYLOAD, user_input="x", tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    draft_id = drafts.save(skill)

    ok, msg = approve_draft(draft_id, store=store, drafts=drafts)
    assert ok, msg

    # 模拟重启：新开一个 store 从磁盘读
    fresh = SkillStore(str(tmp_path / "skills"))
    assert fresh.get_by_name(skill.name) is not None, "批准后重启就丢了"
    assert list((tmp_path / "skills" / "user").glob("*.yaml")), "没有写 yaml 文件"

# ============================================================
# 7. 修订批准：升版本，而不是又加一条技能
# ============================================================


class FakeVersionedStore(FakeSkillStore):
    """有 get(id) + update_skill 的最小 store —— 修订分支要的就是这两个。"""

    def __init__(self, target=None):
        super().__init__({target.name: target} if target else {})
        self.by_id = {target.id: target} if target else {}
        self.updates = []

    def get(self, skill_id):
        return self.by_id.get(skill_id)

    def update_skill(self, skill_name, updates, version=None):
        self.updates.append((skill_name, updates))
        return [f"{skill_name}@1.1.0.yaml"]


def test_approve_revision_bumps_version(drafts, ledger, monkeypatch):
    """批准修订 → 走 update_skill 升版本；技能库里不该多出一条。

    这是"技能不断完善"的落点：同一类事做得越多，同一条技能越准，
    而不是技能数量往上涨、每条都只说了一部分。
    """
    import core.skill_harvest as mod
    from skills.models import Skill

    monkeypatch.setattr(mod, "get_ledger", lambda: ledger)
    _prime(ledger, times=1)  # 档案得先存在，修订才有地方计数
    target = Skill(name="竞品调研", version="1.0.0", id="skill_old")
    store = FakeVersionedStore(target)

    skill = build_skill_from_plan(
        GOOD_PAYLOAD, user_input="x", tool_results=_results(2),
        task_specs=_specs("web_search", "web_fetch"),
    )
    skill.name = "竞品调研"
    draft_id = drafts.save(
        skill,
        revision_of="skill_old",
        base_version="1.0.0",
        competence_key="competitor_research",
    )

    ok, msg = approve_draft(draft_id, store=store, drafts=drafts)
    assert ok, msg
    assert store.updates, "修订没有走升版本"
    assert store.updates[0][0] == "竞品调研"
    assert "1.1.0" in msg
    assert store.added == [], "修订不该往库里新增技能"
    assert drafts.get(draft_id) is None
    assert ledger.get("competitor_research")["revisions"] == 1


def test_approve_revision_refuses_when_target_gone(drafts, ledger, monkeypatch):
    """目标技能被删了就明确失败，别把修订当成新技能塞进去。"""
    import core.skill_harvest as mod

    monkeypatch.setattr(mod, "get_ledger", lambda: ledger)
    store = FakeVersionedStore(None)

    skill = build_skill_from_plan(
        GOOD_PAYLOAD, user_input="x", tool_results=_results(2),
    )
    draft_id = drafts.save(skill, revision_of="skill_gone", base_version="1.0.0")

    ok, msg = approve_draft(draft_id, store=store, drafts=drafts)
    assert ok is False
    assert "不存在" in msg
    assert store.added == []
