"""会话级归纳的闸门测试。

这里钉的核心只有一件事：**一条"综合结论"必须真的由多轮对话撑着**。
用户原话：「记忆的总结不要是单独记住一句话，而是说综合来看才能沉淀。」

所以最有价值的那几条测试全是"应当被拒绝"的：
单轮证据冒充归纳、把第 2 轮的话标成第 1 轮、拿助手的回答当证据。
它们过了，就说明"综合"退化成了一句口号。
"""
import json

import pytest

from core.digest_window import DigestWindow
from core.profile_digest import (
    build_transcript,
    digest_with_llm,
    validate_digest,
)


def _turns():
    """一段真实形状的对话：三轮都在提同一个诉求。"""
    return [
        {"index": 1, "user": "帮我写个爬虫，抓一下这个榜单", "assistant": "好的，用 requests 还是 selenium？"},
        {"index": 2, "user": "回答太长了，短一点", "assistant": "收到，下次精简。"},
        {"index": 3, "user": "还是太长，再短点，只要结论", "assistant": "结论：用 requests 即可。"},
    ]


def _fact(**kw):
    base = {
        "type": "preference",
        "kind": "inferred",
        "content": "用户偏好极简回答，只要结论不要过程",
        "applies_to": "",
        "evidence": [
            {"turn": 2, "quote": "回答太长了，短一点"},
            {"turn": 3, "quote": "还是太长，再短点"},
        ],
        "confidence": 0.85,
        "importance": 0.8,
    }
    base.update(kw)
    return base


# ===== 核心：归纳必须跨轮 =====


def test_inferred_needs_evidence_from_two_turns():
    got = validate_digest({"facts": [_fact()]}, _turns())
    assert len(got) == 1
    assert got[0]["evidence_turns"] == [2, 3]


def test_inferred_with_single_turn_is_rejected():
    """两段 quote 都取自同一轮 —— 那不是"综合"，是同一句话说了两遍。"""
    fact = _fact(evidence=[
        {"turn": 2, "quote": "回答太长"},
        {"turn": 2, "quote": "短一点"},
    ])
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_inferred_with_one_evidence_item_is_rejected():
    fact = _fact(evidence=[{"turn": 3, "quote": "再短点"}])
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_missing_kind_defaults_to_strict():
    """漏标 kind 时按 inferred 处理 —— 不能让漏标成为绕过跨轮校验的后门。"""
    fact = _fact(evidence=[{"turn": 3, "quote": "再短点"}])
    fact.pop("kind")
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_stated_allows_single_turn():
    """用户自己说的身份，合法性来自那句话本身，不需要多轮佐证。"""
    fact = {
        "type": "fact",
        "kind": "stated",
        "content": "用户在做榜单数据抓取",
        "evidence": [{"turn": 1, "quote": "帮我写个爬虫"}],
        "confidence": 0.9,
    }
    got = validate_digest({"facts": [fact]}, _turns())
    assert len(got) == 1
    assert got[0]["evidence_turns"] == [1]


def test_stated_with_two_turns_passes():
    fact = {
        "type": "constraint",
        "kind": "stated",
        "content": "用户要求回答只给结论",
        "evidence": [
            {"turn": 2, "quote": "回答太长了"},
            {"turn": 3, "quote": "只要结论"},
        ],
        "confidence": 0.9,
    }
    got = validate_digest({"facts": [fact]}, _turns())
    assert len(got) == 1


# ===== 证据必须可对账 =====


def test_quote_must_belong_to_the_declared_turn():
    """把第 3 轮的话标成第 1 轮 —— 跨轮约束会因此形同虚设，必须拦住。"""
    fact = _fact(evidence=[
        {"turn": 1, "quote": "还是太长，再短点"},
        {"turn": 2, "quote": "短一点"},
    ])
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_quote_from_assistant_is_rejected():
    """助手说的话不能当用户的证据。"""
    fact = _fact(evidence=[
        {"turn": 1, "quote": "用 requests 还是 selenium"},
        {"turn": 2, "quote": "短一点"},
    ])
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_nonexistent_turn_is_rejected():
    fact = _fact(evidence=[
        {"turn": 9, "quote": "短一点"},
        {"turn": 3, "quote": "再短点"},
    ])
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_evidence_must_be_a_list_not_a_string():
    """老格式（单个字符串）不接受：没有轮次号就无法对账。"""
    fact = _fact(evidence="用户嫌回答太长")
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_whitespace_differences_are_tolerated():
    turns = [{"index": 1, "user": "回答太长了\n短一点", "assistant": ""},
             {"index": 2, "user": "再短点", "assistant": ""}]
    fact = _fact(evidence=[
        {"turn": 1, "quote": "回答太长了 短一点"},
        {"turn": 2, "quote": "再短点"},
    ])
    assert len(validate_digest({"facts": [fact]}, turns)) == 1


# ===== 其他闸门 =====


def test_two_ephemeral_turns_can_still_form_a_pattern():
    """已知边界：同一句临时参数出现两轮，结构校验拦不住。

    "这次用 5 秒" + "这次还是 5 秒" 在证据形态上完全合法（跨轮、逐字对得上），
    而结论"偏好 5 秒"确实是错的。这一层只能靠提示词里"临时参数不要记"，
    以及 content 里出现「这次/刚才」时直接拒（见下一条）。

    记在这里是为了不假装它是零风险：真要把这层堵死，得让模型区分
    "参数调整"和"偏好表达"，那是另一个量级的问题。
    """
    turns = [
        {"index": 1, "user": "这次用 5 秒", "assistant": ""},
        {"index": 2, "user": "这次还是 5 秒吧", "assistant": ""},
    ]
    fact = _fact(
        content="用户偏好视频时长 5 秒",
        evidence=[
            {"turn": 1, "quote": "这次用 5 秒"},
            {"turn": 2, "quote": "还是 5 秒"},
        ],
    )
    assert len(validate_digest({"facts": [fact]}, turns)) == 1


def test_content_with_ephemeral_word_is_rejected():
    fact = _fact(content="用户这次偏好极简回答")
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_invented_applies_to_is_rejected():
    fact = _fact(applies_to="淘宝")
    assert validate_digest({"facts": [fact]}, _turns()) == []


def test_applies_to_present_in_conversation_passes():
    turns = _turns() + [
        {"index": 4, "user": "minimax 那个模型怎么调", "assistant": "用国内站"},
        {"index": 5, "user": "以后 minimax 都用国内站", "assistant": "好的"},
    ]
    fact = _fact(
        content="在 minimax 上优先使用国内站",
        applies_to="minimax",
        evidence=[
            {"turn": 4, "quote": "minimax 那个模型怎么调"},
            {"turn": 5, "quote": "以后 minimax 都用国内站"},
        ],
    )
    got = validate_digest({"facts": [fact]}, turns)
    assert len(got) == 1
    assert got[0]["applies_to"] == "minimax"


def test_low_confidence_is_rejected():
    assert validate_digest({"facts": [_fact(confidence=0.3)]}, _turns()) == []


def test_bad_type_is_rejected():
    assert validate_digest({"facts": [_fact(type="emotion")]}, _turns()) == []


def test_duplicate_contents_collapse():
    got = validate_digest({"facts": [_fact(), _fact()]}, _turns())
    assert len(got) == 1


def test_empty_facts_is_fine():
    assert validate_digest({"facts": []}, _turns()) == []
    assert validate_digest({}, _turns()) == []


# ===== 渲染 =====


def test_transcript_carries_turn_numbers():
    """没有轮次号，模型没法标证据，校验也没法对账。"""
    text = build_transcript(_turns())
    assert "[第 1 轮]" in text
    assert "[第 3 轮]" in text
    assert "用户：" in text and "助手：" in text


def test_transcript_truncates_long_assistant_replies():
    turns = [{"index": 1, "user": "问", "assistant": "答" * 500}]
    assert len(build_transcript(turns)) < 300


# ===== 端到端（假 LLM） =====


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    async def chat_with_retry(self, messages, **kwargs):
        return self.reply


@pytest.mark.asyncio
async def test_digest_keeps_evidence_turns_and_keys_by_scope():
    reply = json.dumps({"facts": [_fact(applies_to="")]})
    got = await digest_with_llm(_turns(), llm=_FakeLLM(reply))
    assert len(got) == 1
    assert got[0].evidence_turns == [2, 3]
    assert got[0].rule == "digest"


@pytest.mark.asyncio
async def test_digest_returns_empty_on_garbage():
    got = await digest_with_llm(_turns(), llm=_FakeLLM("我觉得用户喜欢简短"))
    assert got == []


@pytest.mark.asyncio
async def test_digest_skips_trivial_conversation():
    """全是「继续」的一段，不值得跑一次 LLM。"""
    turns = [{"index": 1, "user": "继续", "assistant": ""},
             {"index": 2, "user": "好", "assistant": ""}]
    got = await digest_with_llm(turns, llm=_FakeLLM(json.dumps({"facts": [_fact()]})))
    assert got == []


# ===== 累积窗口 =====


def test_window_holds_until_full():
    win = DigestWindow(size=3, keep=0)
    assert win.add("第一轮说的话") is None
    assert win.add("第二轮说的话") is None
    assert win.pending() == 2
    batch = win.add("第三轮说的话")
    assert batch is not None
    assert [t["index"] for t in batch] == [1, 2, 3]
    assert win.pending() == 0


def test_window_keeps_tail_for_continuity():
    """跨批的"反复出现"不能在边界上断掉。"""
    win = DigestWindow(size=3, keep=1)
    win.add("第一轮说的话")
    win.add("第二轮说的话")
    win.add("第三轮说的话")
    win.add("第四轮说的话")
    batch = win.add("第五轮说的话")
    assert [t["user"] for t in batch] == ["第三轮说的话", "第四轮说的话", "第五轮说的话"]


def test_window_does_not_advance_index_for_filler():
    """「继续」「好」不占位 —— 否则一串继续会把窗口撑满，白跑一次归纳。"""
    win = DigestWindow(size=2, keep=0)
    assert win.add("继续") is None
    assert win.add("好") is None
    assert win.pending() == 0
    assert win.add("第二点：回答短一点") is None
    batch = win.add("第三点：还是短一点")
    assert batch is not None
    assert [t["index"] for t in batch] == [1, 2]


def test_flush_returns_partial_batch():
    """reset 时剩下几轮也要交出去，否则随 reset 一起消失。"""
    win = DigestWindow(size=6, keep=2)
    win.add("第一轮说的话")
    batch = win.flush()
    assert batch is not None
    assert len(batch) == 1
    assert win.flush() is None


def test_window_carries_assistant_text():
    win = DigestWindow(size=1, keep=0)
    batch = win.add("用户说的话", "助手的回答")
    assert batch[0]["assistant"] == "助手的回答"
