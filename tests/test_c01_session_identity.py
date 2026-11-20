"""服务端签发连接身份测试（本地单用户，无令牌）。

覆盖:
- init 时签发的 client_id / session_id 由服务端生成
- 可按 session_id 恢复会话（本地单用户，无归属校验）
- conn_id → 身份解析不需要令牌
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===== 身份签发 =====


@pytest.mark.asyncio
async def test_issue_identity_is_server_generated():
    from backend.session import sessions

    conn_id = "client-supplied-conn-id"
    identity = await sessions.issue_identity(conn_id)
    try:
        # client_id 由服务端生成，不等于客户端提交的 conn_id
        assert identity.client_id != conn_id
        assert identity.session_id.startswith("s-")
        assert identity.client_id.startswith("c-")
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_issue_identity_can_resume_session():
    from backend.session import sessions

    identity = await sessions.issue_identity("conn-resume", resume_session_id="s-existing")
    try:
        assert identity.session_id == "s-existing"
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_resolve_identity_needs_no_token():
    from backend.session import sessions

    conn_id = "conn-for-resolve-test"
    identity = await sessions.issue_identity(conn_id)
    try:
        resolved = sessions.resolve_identity(conn_id)
        assert resolved is not None
        assert resolved.client_id == identity.client_id
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_resolve_identity_rejects_unknown_conn():
    from backend.session import sessions

    assert sessions.resolve_identity("never-initialized") is None


@pytest.mark.asyncio
async def test_session_carries_issued_identity_to_agent():
    """服务端签发的 session_id 要传到 Agent。"""
    from backend.session import sessions

    conn_id = "conn-for-agent-test"
    identity = await sessions.issue_identity(conn_id)
    try:
        session = await sessions.get_or_create(
            identity.client_id,
            session_id=identity.session_id,
        )
        assert session.agent.session_id == identity.session_id
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_two_conns_get_distinct_identities():
    """两个连接拿到不同的 client_id / session_id。"""
    from backend.session import sessions

    a = await sessions.issue_identity("conn-a")
    b = await sessions.issue_identity("conn-b")
    try:
        assert a.client_id != b.client_id
        assert a.session_id != b.session_id
    finally:
        await sessions.destroy(a.client_id)
        await sessions.destroy(b.client_id)


# ===== 换会话（前端「新对话 / 切换会话」依赖的机制） =====
#
# 前端不重连，只是对同一个 conn 重发一次 init。服务端每次 init 都会签发新的
# client_id，但 `conn_id → client_id` 的映射被覆盖 —— 于是后续 chat/{conn}
# 解析到的是新会话。下面几条钉的就是这个覆盖行为：
# 它一旦坏掉，表现是"点了切换，消息还写回旧会话"，而界面看起来一切正常。


@pytest.mark.asyncio
async def test_reinit_same_conn_switches_session():
    from backend.session import sessions

    conn_id = "conn-switch-test"
    first = await sessions.issue_identity(conn_id, resume_session_id="s-before")
    try:
        second = await sessions.issue_identity(conn_id, resume_session_id="s-after")
        # 每次 init 都是新身份（旧 Session 靠 TTL 回收）
        assert second.client_id != first.client_id
        # 但这条连接现在指向新会话
        assert sessions.resolve_identity(conn_id).session_id == "s-after"
        assert sessions.resolve_identity(conn_id).client_id == second.client_id
    finally:
        await sessions.destroy(first.client_id)
        await sessions.destroy(second.client_id)


@pytest.mark.asyncio
async def test_reinit_without_session_id_mints_a_new_one():
    """「新对话」= init 不带 session_id，服务端必须给一个新的。"""
    from backend.session import sessions

    conn_id = "conn-new-chat-test"
    old = await sessions.issue_identity(conn_id, resume_session_id="s-existing")
    try:
        fresh = await sessions.issue_identity(conn_id, resume_session_id="")
        assert fresh.session_id.startswith("s-")
        assert fresh.session_id != "s-existing"
        assert sessions.resolve_identity(conn_id).session_id == fresh.session_id
    finally:
        await sessions.destroy(old.client_id)
        await sessions.destroy(fresh.client_id)


@pytest.mark.asyncio
async def test_switch_lands_on_a_distinct_agent():
    """换会话要真的换 Agent —— 否则 Context 还是旧的，模型照样记得上文。"""
    from backend.session import sessions

    conn_id = "conn-switch-agent-test"
    identity_a = await sessions.issue_identity(conn_id, resume_session_id="s-agent-a")
    session_a = await sessions.get_or_create(
        identity_a.client_id, session_id=identity_a.session_id
    )
    identity_b = await sessions.issue_identity(conn_id, resume_session_id="s-agent-b")
    session_b = await sessions.get_or_create(
        identity_b.client_id, session_id=identity_b.session_id
    )
    try:
        assert session_a is not session_b
        assert session_b.agent.session_id == "s-agent-b"
        assert session_b.agent is not session_a.agent
    finally:
        await sessions.destroy(identity_a.client_id)
        await sessions.destroy(identity_b.client_id)
