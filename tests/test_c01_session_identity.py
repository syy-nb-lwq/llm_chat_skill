"""C-01 服务端签发身份 + 会话令牌测试。

覆盖:
- 会话令牌签发/校验闭环,以及被篡改后的拒绝
- init 时签发的身份与客户端自带的 conn_id 无关
- 后续请求必须携带有效令牌,缺失/伪造一律拒绝
- 服务端签发的 user_id / session_id 会传递到 Agent
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===== 令牌 =====


def test_session_token_roundtrip():
    from infra.auth import issue_session_token, verify_session_token

    token = issue_session_token("u-1", "s-1", "c-1")
    payload = verify_session_token(token)
    assert payload is not None
    assert payload["user_id"] == "u-1"
    assert payload["session_id"] == "s-1"
    assert payload["client_id"] == "c-1"


@pytest.mark.parametrize("bad", ["", "not-a-token", "abc", "a.b", "...."])
def test_session_token_rejects_invalid(bad):
    from infra.auth import verify_session_token

    assert verify_session_token(bad) is None


def test_session_token_rejects_tampered_payload():
    import base64
    import json

    from infra.auth import issue_session_token, verify_session_token

    token = issue_session_token("u-1", "s-1", "c-1")
    body, _, sig = token.partition(".")

    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["client_id"] = "c-forged"
    forged_body = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    # 签名对不上 → 拒绝
    assert verify_session_token(f"{forged_body}.{sig}") is None
    # 原始令牌仍然有效
    assert verify_session_token(token) is not None


# ===== 身份签发 =====


@pytest.mark.asyncio
async def test_issue_identity_is_server_generated():
    from backend.session import sessions

    conn_id = "client-supplied-conn-id"
    identity = await sessions.issue_identity(conn_id)
    try:
        # 身份全部由服务端生成,不等于客户端提交的 conn_id
        assert identity.client_id != conn_id
        assert identity.user_id.startswith("u-")
        assert identity.session_id.startswith("s-")
        assert identity.client_id.startswith("c-")
        assert identity.token
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_resolve_identity_requires_valid_token():
    from backend.session import sessions

    conn_id = "conn-for-token-test"
    identity = await sessions.issue_identity(conn_id)
    try:
        # 缺令牌 → 拒绝
        assert sessions.resolve_identity(conn_id) is None
        assert sessions.resolve_identity(conn_id, "") is None
        # 伪造令牌 → 拒绝
        assert sessions.resolve_identity(conn_id, "forged.token") is None
        # 正确令牌 → 通过
        resolved = sessions.resolve_identity(conn_id, identity.token)
        assert resolved is not None
        assert resolved.client_id == identity.client_id
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_resolve_identity_rejects_unknown_conn():
    from backend.session import sessions

    assert sessions.resolve_identity("never-initialized", "whatever") is None


@pytest.mark.asyncio
async def test_session_carries_issued_identity_to_agent():
    """服务端签发的 user_id / session_id 要传到 Agent(用户隔离的基础)。"""
    from backend.session import sessions

    conn_id = "conn-for-agent-test"
    identity = await sessions.issue_identity(conn_id)
    try:
        session = await sessions.get_or_create(
            identity.client_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
        )
        assert session.agent.user_id == identity.user_id
        assert session.agent.session_id == identity.session_id
    finally:
        await sessions.destroy(identity.client_id)


@pytest.mark.asyncio
async def test_two_conns_get_distinct_identities():
    """两个客户端不能拿到同一个身份。"""
    from backend.session import sessions

    a = await sessions.issue_identity("conn-a")
    b = await sessions.issue_identity("conn-b")
    try:
        assert a.client_id != b.client_id
        assert a.user_id != b.user_id
        assert a.session_id != b.session_id
        # A 的令牌不能用在 B 上
        assert sessions.resolve_identity("conn-b", a.token) is None
    finally:
        await sessions.destroy(a.client_id)
        await sessions.destroy(b.client_id)
