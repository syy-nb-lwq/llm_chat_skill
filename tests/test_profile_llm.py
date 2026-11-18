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

    用户说过：「不要用国际站然后可以开始生成视频」
    规则版把它抽成了 `constraint`。提示词里必须点名这个坑。
    """
    p = PROFILE_PROMPT
    assert "不要用国际站" in p
    assert "不构成长期雷区" in p


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
