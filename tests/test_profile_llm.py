"""画像抽取（LLM 版）—— 反幻觉是结构性的，不靠提示词求它别编。

## 为什么换成 LLM

规则版只认「我习惯…/我喜欢…/记住…」这类句式。用户正常下任务时
**一条都抽不出来**；偶尔抽出来的还是错的 —— 实机记录：

    constraint: 不要用国际站然后可以开始生成视频

把一句一次性任务指令当成了长期雷区。

## 为什么敢用 LLM

关键不是提示词写得好，而是**代码层面的证据校验**：

    模型说「用户的偏好是 X」
      -> 必须给出 evidence（用户原话的逐字片段）
      -> evidence 必须能在 user_input 里找到
      -> 找不到 → 整条丢弃

这让"编造偏好"变成结构性不可能。下面每条测试都在钉这个。
"""
import pytest

from core.profile_llm import (
    ALLOWED_TYPES,
    MAX_FACTS_PER_TURN,
    MIN_CONFIDENCE,
    PROFILE_PROMPT,
    extract_with_llm,
    validate_facts,
)


def _fact(**kw):
    base = {
        "type": "preference",
        "content": "不喜欢太啰嗦的解释",
        "evidence": "不喜欢太啰嗦的解释",
        "confidence": 0.9,
        "importance": 0.7,
    }
    base.update(kw)
    return base


TEXT = "我这个人不喜欢太啰嗦的解释，直接给结论就行。"


# ============================================================
# 1. 证据校验 —— 反幻觉的核心
# ============================================================

def test_accepts_fact_with_verbatim_evidence():
    got = validate_facts({"facts": [_fact()]}, TEXT)
    assert len(got) == 1
    assert got[0]["content"] == "不喜欢太啰嗦的解释"


def test_rejects_fact_without_evidence():
    """给不出证据 = 编的。"""
    got = validate_facts({"facts": [_fact(evidence="")]}, TEXT)
    assert got == []


def test_rejects_fact_with_paraphrased_evidence():
    """**最关键的一条**：证据必须是原话，不能是改写。

    模型很容易把"用户不喜欢啰嗦"写成 evidence，
    但那不是原文 —— 这种情况必须丢弃。
    """
    got = validate_facts({"facts": [_fact(evidence="用户不喜欢啰嗦")]}, TEXT)
    assert got == []


def test_evidence_tolerates_whitespace_differences():
    text = "我这个人不喜欢\n太啰嗦的解释"
    got = validate_facts({"facts": [_fact(evidence="不喜欢 太啰嗦的解释")]}, text)
    assert len(got) == 1


def test_rejects_hallucinated_preference_altogether():
    """模型凭空造一条"用户喜欢蓝色"，原话里根本没有 → 必须丢弃。"""
    got = validate_facts(
        {"facts": [_fact(type="fact", content="喜欢蓝色", evidence="喜欢蓝色")]},
        TEXT,
    )
    assert got == []


# ============================================================
# 2. 其它闸门
# ============================================================

def test_rejects_unknown_type():
    got = validate_facts({"facts": [_fact(type="emotion")]}, TEXT)
    assert got == []


def test_accepts_all_whitelisted_types():
    for t in ALLOWED_TYPES:
        got = validate_facts({"facts": [_fact(type=t)]}, TEXT)
        assert len(got) == 1, f"{t} 被误拦"


def test_rejects_low_confidence():
    got = validate_facts(
        {"facts": [_fact(confidence=MIN_CONFIDENCE - 0.1)]}, TEXT
    )
    assert got == []


def test_rejects_too_short_or_too_long_content():
    assert validate_facts({"facts": [_fact(content="好")]}, TEXT) == []
    assert validate_facts(
        {"facts": [_fact(content="很" * 200)]}, TEXT
    ) == []


def test_caps_number_of_facts():
    facts = [_fact(content=f"偏好测试条目{i}", evidence="太啰嗦", type="preference")
             for i in range(10)]
    got = validate_facts({"facts": facts}, TEXT)
    assert len(got) == MAX_FACTS_PER_TURN


def test_dedupes_same_fact():
    got = validate_facts({"facts": [_fact(), _fact()]}, TEXT)
    assert len(got) == 1


def test_tolerates_garbage_payload():
    for bad in ({}, {"facts": "nope"}, {"facts": [None, 1, "x"]}, None):
        assert validate_facts(bad, TEXT) == []


def test_clamps_importance():
    got = validate_facts({"facts": [_fact(importance=9.9)]}, TEXT)
    assert got[0]["importance"] == 1.0


# ============================================================
# 3. 提示词要点（防止以后被改坏）
# ============================================================

def test_prompt_demands_long_term_only():
    p = PROFILE_PROMPT
    assert "长期" in p
    assert "一次性" in p
    assert "evidence" in p


def test_prompt_contains_the_real_bad_example():
    """把实机踩过的误抽案例写进提示词，防止它再犯。

    两个真实案例，第二个更危险：
    1. 「不要用国际站然后可以开始生成视频」被抽成 `constraint`
       —— 一次性指令被当成长期雷区
    2. 「minimax 我以后都用国内站」被抽成「用户不要使用国际站」
       —— **限定被放大成普遍结论**，于是聊电商时助手也拿它否掉跨境方案
    """
    p = PROFILE_PROMPT
    assert "不要用国际站" in p
    assert "minimax" in p
    # 限定字段 + 不许放大的规则必须在提示词里
    assert "applies_to" in p
    assert "放大" in p
    # 平台站群 ≠ 跨境业务，这一点必须点明，否则模型会自己联想
    assert "跨境电商" in p


def test_prompt_allows_empty_result():
    """最常见、最正确的结果就是"没什么可记的"。"""
    assert '{"facts": []}' in PROFILE_PROMPT


# ============================================================
# 4. 端到端（假 LLM）
# ============================================================

class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def chat_with_retry(self, messages, **kwargs):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.asyncio
async def test_extract_with_llm_happy_path():
    import json

    llm = FakeLLM(json.dumps({"facts": [_fact()]}))
    got = await extract_with_llm(TEXT, llm=llm)
    assert len(got) == 1
    assert got[0].type == "preference"
    assert got[0].rule == "llm"


@pytest.mark.asyncio
async def test_extract_with_llm_returns_empty_on_bad_json():
    got = await extract_with_llm(TEXT, llm=FakeLLM("我觉得这个用户挺好的"))
    assert got == []


@pytest.mark.asyncio
async def test_extract_with_llm_survives_llm_error():
    got = await extract_with_llm(TEXT, llm=FakeLLM(RuntimeError("API 挂了")))
    assert got == []


@pytest.mark.asyncio
async def test_short_input_skips_llm():
    llm = FakeLLM("{}")
    got = await extract_with_llm("好", llm=llm)
    assert got == []
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_empty_facts_is_normal():
    """模型说"没什么可记的"——这是正常结果，不是失败。"""
    import json

    got = await extract_with_llm(TEXT, llm=FakeLLM(json.dumps({"facts": []})))
    assert got == []


# ============================================================
# 5. 限定作用域 —— 不许把"某个平台的选择"放大成普遍结论
# ============================================================
#
# 实机翻车：用户在前文讨论 minimax（有国内站/海外站）时说
# 「我以后都不要用国际站，只用国内站」，被抽成
#     constraint: 用户以后不要使用国际站，只使用国内站
# 于是几天后用户说「我要做电商」，助手拿这条去否掉跨境电商方案。
# 用户从没说过"不做跨境"，他说的是某个 AI 服务商用哪个站。
#
# 修法：抽取时带上前几轮上下文（听懂指代）+ 新增 applies_to（限定名）+
# 注入时按话题过滤（semantic_memory.build_profile_block）。

SCOPED_TEXT = "我以后都不要用国际站，只用国内站。"
SCOPED_CONTEXT = "用户：minimax 那个视频生成怎么调\n助手：可以用国内站 platform.minimaxi.com"


def _scoped_fact(**kw):
    base = {
        "type": "preference",
        "content": "在 minimax 上优先使用国内站",
        "applies_to": "minimax",
        "evidence": "我以后都不要用国际站",
        "confidence": 0.9,
        "importance": 0.8,
    }
    base.update(kw)
    return base


def test_keeps_applies_to_that_appears_in_context():
    """平台名只在**前文**里出现过 —— 这是最常见的真实情况，必须认。"""
    got = validate_facts({"facts": [_scoped_fact()]}, SCOPED_TEXT, context=SCOPED_CONTEXT)
    assert len(got) == 1
    assert got[0]["applies_to"] == "minimax"


def test_keeps_applies_to_that_appears_in_current_sentence():
    got = validate_facts(
        {"facts": [_scoped_fact(evidence="minimax 用国内站")]},
        "minimax 用国内站就行",
        context="",
    )
    assert len(got) == 1


def test_rejects_invented_applies_to():
    """限定名在前文和当前句里都没有 = 前提是编的。

    这比不填更危险：将来按名字做相关性过滤，会挂到一个不存在的场景上。
    """
    got = validate_facts(
        {"facts": [_scoped_fact(applies_to="淘宝")]}, SCOPED_TEXT, context=SCOPED_CONTEXT
    )
    assert got == []


def test_evidence_still_cannot_come_from_context():
    """上下文只用来听懂指代，**不能**当证据 —— 否则模型可以拿前文凑数。"""
    got = validate_facts(
        {"facts": [_scoped_fact(evidence="minimax 那个视频生成怎么调")]},
        SCOPED_TEXT,
        context=SCOPED_CONTEXT,
    )
    assert got == []


def test_global_fact_has_empty_applies_to():
    """对所有场景都成立的话，applies_to 留空。"""
    got = validate_facts({"facts": [_fact()]}, TEXT)
    assert len(got) == 1
    assert got[0]["applies_to"] == ""


def test_too_long_applies_to_is_rejected():
    """applies_to 是个名字，不是句子。"""
    long_name = "某" * 60
    got = validate_facts(
        {"facts": [_scoped_fact(applies_to=long_name, evidence=long_name)]},
        long_name + SCOPED_TEXT,
    )
    assert got == []


@pytest.mark.asyncio
async def test_context_is_rendered_into_prompt():
    """前几轮必须真的进了提示词，否则模型还是看不见 minimax。"""
    import json

    captured = {}

    class RecordingLLM:
        async def chat_with_retry(self, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return json.dumps({"facts": []})

    await extract_with_llm(SCOPED_TEXT, context=SCOPED_CONTEXT, llm=RecordingLLM())
    assert "minimax" in captured["prompt"]
    assert "前几轮" in captured["prompt"]
    # 上下文是"只读指代"的，必须写明不许当证据用
    assert "取 evidence" in captured["prompt"]


@pytest.mark.asyncio
async def test_extract_keeps_applies_to_and_keys_by_it():
    """限定名要进 normalized_key —— 否则"两个平台用同一策略"会互相覆盖。"""
    import json

    reply = json.dumps({"facts": [_scoped_fact()]})
    got = await extract_with_llm(SCOPED_TEXT, context=SCOPED_CONTEXT, llm=FakeLLM(reply))
    assert len(got) == 1
    assert got[0].applies_to == "minimax"
    assert "minimax" in got[0].normalized_key
