"""F3 验收测试 —— 受控通信。

对应 `docs/18` §8 的 F3 与 §4.6：

- 定向：不能广播
- 有边：没有依赖边不许子↔子直连
- 有限额：消息条数与体积
- 有保序：同一对 agent FIFO
- 有失败语义：对方已完成 / 在跑 / 已死
- 有审计：每条消息落盘
"""
import json

import pytest

from core.agentkit.messaging import (
    MAIN_AGENT_IDS,
    MessageBus,
    MessageKind,
    MessageRejected,
)


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """总线 + 审计写到临时目录（别污染真实 memory/audit）。"""
    import core.audit as audit_mod

    lines = []
    monkeypatch.setattr(
        audit_mod,
        "append_jsonl",
        lambda filename, payload: lines.append((filename, payload)) or True,
    )
    b = MessageBus()
    b._audit_lines = lines   # 便于断言
    return b


# ============================================================
# 路由规则
# ============================================================

def test_broadcast_is_rejected(bus):
    with pytest.raises(MessageRejected):
        bus.send(
            kind=MessageKind.QUERY,
            from_agent="sub_a",
            to_agent="broadcast",
            payload={"question": "在吗"},
        )


def test_peer_to_peer_without_edge_is_rejected(bus):
    """没有依赖边 → 不许子智能体直接通信（必须经主智能体转发）。"""
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    with pytest.raises(MessageRejected):
        bus.send(
            kind=MessageKind.QUERY,
            from_agent="sub_a",
            to_agent="sub_b",
            payload={"question": "帮我查的条款是哪些"},
        )


def test_peer_to_peer_with_edge_is_allowed(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    msg = bus.send(
        kind=MessageKind.QUERY,
        from_agent="sub_a",
        to_agent="sub_b",
        payload={"question": "条款有哪些"},
    )
    assert msg.to_agent == "sub_b"
    assert len(bus.inbox("sub_b")) == 1


def test_main_to_sub_is_always_allowed(bus):
    bus.register_agent("sub_a")
    msg = bus.send(
        kind=MessageKind.TASK_ASSIGN,
        from_agent="main",
        to_agent="sub_a",
        payload={"goal": "查监管要求"},
    )
    assert msg.kind == MessageKind.TASK_ASSIGN


def test_unknown_message_kind_is_rejected(bus):
    with pytest.raises(MessageRejected):
        bus.send(kind="gossip", from_agent="main", to_agent="sub_a")


# ============================================================
# 预算
# ============================================================

def test_message_count_budget_is_enforced(bus):
    bus.register_agent("sub_a", max_messages=2)
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    bus.send(kind=MessageKind.QUERY, from_agent="sub_a", to_agent="sub_b",
             payload={"question": "1"})
    bus.send(kind=MessageKind.QUERY, from_agent="sub_a", to_agent="sub_b",
             payload={"question": "2"})
    with pytest.raises(MessageRejected):
        bus.send(kind=MessageKind.QUERY, from_agent="sub_a", to_agent="sub_b",
                 payload={"question": "3"})


def test_oversized_payload_is_rejected(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    with pytest.raises(MessageRejected):
        bus.send(
            kind=MessageKind.HANDOFF,
            from_agent="sub_a",
            to_agent="sub_b",
            payload={"data": "x" * 5000},
        )


# ============================================================
# 保序与收件
# ============================================================

def test_inbox_is_fifo(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    for i in range(3):
        bus.send(kind=MessageKind.QUERY, from_agent="sub_a", to_agent="sub_b",
                 payload={"question": f"q{i}"})
    inbox = bus.drain("sub_b")
    assert [m.payload["question"] for m in inbox] == ["q0", "q1", "q2"]
    assert bus.drain("sub_b") == []


def test_refs_are_kept_for_large_payloads(bus):
    """大内容应该走引用，不是塞全文。"""
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    msg = bus.send(
        kind=MessageKind.HANDOFF,
        from_agent="sub_a",
        to_agent="sub_b",
        payload={"summary": "条款见附件"},
        refs=["workspace/researcher/out.md"],
    )
    assert msg.refs == ["workspace/researcher/out.md"]


# ============================================================
# 失败语义
# ============================================================

def test_ask_a_finished_agent_answers_from_its_report(bus):
    """对方已完成 → 直接从报告包回答，**不让它重做**。"""
    bus.register_agent("sub_b")
    bus.mark_done("sub_b", {
        "status": "ok",
        "summary": "共 3 条关键条款",
        "findings": ["第 3 条：不得含有未备案内容", "第 7 条：需标注备案号"],
    })
    res = bus.ask(from_agent="sub_a", to_agent="sub_b", question="条款有哪些")
    assert res["answered"] is True
    assert res["state"] == "answered"
    assert "未备案" in res["answer"]
    # 关键：没有因此产生新的消息（没让它重做）
    assert bus.stats()["messages"] == 0


def test_ask_a_running_agent_queues_the_question(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")     # 活着但没完成
    bus.allow_pair("sub_a", "sub_b")
    res = bus.ask(from_agent="sub_a", to_agent="sub_b", question="进度?")
    assert res["answered"] is False
    assert res["state"] == "queued"
    assert len(bus.inbox("sub_b")) == 1


def test_ask_a_dead_agent_reports_unavailable(bus):
    """对方已失败 → 立刻说不可用，不让提问方干等。"""
    bus.register_agent("sub_b")
    bus.mark_failed("sub_b", "工具超时")
    res = bus.ask(from_agent="sub_a", to_agent="sub_b", question="条款有哪些")
    assert res["answered"] is False
    assert res["state"] == "unavailable"
    assert "不可用" in res["answer"]


def test_answer_can_be_sent_back(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    q = bus.send(kind=MessageKind.QUERY, from_agent="sub_a", to_agent="sub_b",
                 payload={"question": "参数是什么"})
    a = bus.answer(from_agent="sub_b", to_agent="sub_a",
                   question_msg_id=q.msg_id, answer="参数是 policy_id")
    assert a.kind == MessageKind.ANSWER
    assert a.reply_to == q.msg_id


# ============================================================
# 审计
# ============================================================

def test_every_message_is_audited(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    bus.send(kind=MessageKind.HANDOFF, from_agent="sub_a", to_agent="sub_b",
             payload={"summary": "给你结论"})
    assert bus._audit_lines, "每条消息都要落审计"
    filename, payload = bus._audit_lines[-1]
    assert filename == "messages.jsonl"
    assert payload["from_agent"] == "sub_a"
    assert payload["to_agent"] == "sub_b"
    assert payload["kind"] == MessageKind.HANDOFF


def test_stats_reflect_usage(bus):
    bus.register_agent("sub_a")
    bus.register_agent("sub_b")
    bus.allow_pair("sub_a", "sub_b")
    bus.send(kind=MessageKind.QUERY, from_agent="sub_a", to_agent="sub_b",
             payload={"question": "q"})
    stats = bus.stats()
    assert stats["messages"] == 1
    assert stats["agents"]["sub_a"]["sent"] == 1
    assert stats["agents"]["sub_b"]["received"] == 1
