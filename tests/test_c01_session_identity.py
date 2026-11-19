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
