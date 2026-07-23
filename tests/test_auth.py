"""C-01:身份与认证层测试。

覆盖:
- gen_user_id / gen_session_id / gen_client_id 格式
- extract_ws_identity:从 data 提取身份 + 服务端签发 server_client_id
- require_owner_token:未配置放行 / 配置后校验 Bearer token
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_gen_ids_format():
    from infra.auth import gen_user_id, gen_session_id, gen_client_id

    uid = gen_user_id()
    sid = gen_session_id()
    cid = gen_client_id()
    assert uid.startswith("u-")
    assert sid.startswith("s-")
    assert cid.startswith("c-")
    # 每次生成唯一
    assert gen_user_id() != uid
    assert gen_client_id() != cid


def test_extract_ws_identity_from_data():
    from infra.auth import extract_ws_identity

    uid, sid, server_cid = extract_ws_identity(
        {"user_id": "alice", "session_id": "sess-1"}
    )
    assert uid == "alice"
    assert sid == "sess-1"
    # server_client_id 始终由服务端签发
    assert server_cid.startswith("c-")


def test_extract_ws_identity_generates_when_missing():
    from infra.auth import extract_ws_identity

    uid, sid, server_cid = extract_ws_identity({})
    assert uid.startswith("u-")  # 缺失则生成
    assert sid.startswith("s-")
    assert server_cid.startswith("c-")
    # 两次调用生成不同 id
    uid2, _, _ = extract_ws_identity({})
    assert uid2 != uid


def test_extract_ws_identity_from_headers():
    from infra.auth import extract_ws_identity

    uid, sid, _ = extract_ws_identity(
        {}, headers={"x-user-id": "bob", "x-session-id": "s2"}
    )
    assert uid == "bob"
    assert sid == "s2"


class _FakeRequest:
    """模拟 Starlette Request,只带 headers。"""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}


def test_require_owner_token_disabled_by_default(monkeypatch):
    """owner_token 未配置 → 放行。"""
    from infra import auth as auth_mod
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    # get_owner_token 读 config.owner_token
    assert auth_mod.get_owner_token() is None

    # require_owner_token 不抛
    asyncio.run(auth_mod.require_owner_token(_FakeRequest()))


def test_require_owner_token_missing_token(monkeypatch):
    """配置了 owner_token 但请求没带 → 401。"""
    from fastapi import HTTPException
    from infra import auth as auth_mod
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-xyz")
    assert auth_mod.get_owner_token() == "secret-xyz"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_mod.require_owner_token(_FakeRequest({})))
    assert exc.value.status_code == 401


def test_require_owner_token_wrong_token(monkeypatch):
    """token 不匹配 → 403。"""
    from fastapi import HTTPException
    infra = pytest.importorskip("infra")
    from infra import auth as auth_mod
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-xyz")

    req = _FakeRequest({"authorization": "Bearer wrong-token"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_mod.require_owner_token(req))
    assert exc.value.status_code == 403


def test_require_owner_token_correct(monkeypatch):
    """正确 token → 放行(不抛)。"""
    from infra import auth as auth_mod
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-xyz")
    req = _FakeRequest({"authorization": "Bearer secret-xyz"})
    # 不抛即通过
    asyncio.run(auth_mod.require_owner_token(req))


def test_management_routes_have_owner_token_dep():
    """管理写操作路由应注册 require_owner_token 依赖(C-01)。

    不启动 app(避免 startup 校验),只检查路由的 dependencies。
    """
    from backend.main import app, require_owner_token

    protected = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        deps = getattr(route, "dependant", None)
        call = getattr(route, "endpoint", None)
        method = ""
        for m in getattr(route, "methods", set()) or set():
            method = m
            break
        if method not in ("DELETE", "POST"):
            continue
        if not path.startswith("/api/"):
            continue
        # 读操作 GET 不保护
        if path in ("/api/feedback",) or "approve" in path or "reject" in path \
           or "reload" in path or "rollback" in path or "cancel" in path \
           or "choose" in path or "confirm" in path or "self-evolution" in path \
           or path == "/api/memory" or path.startswith("/api/memory/") \
           or path.startswith("/api/skills/") and path != "/api/skills":
            # 检查 dependant 的 dependencies 是否含 require_owner_token
            dep_list = getattr(deps, "dependencies", []) if deps else []
            funcs = [getattr(d, "call", None) for d in dep_list]
            if require_owner_token in funcs:
                protected.add(path)

    # 至少这些管理路由被保护
    expected = {
        "/api/skills/{name}",
        "/api/skills/{name}/{version}",
        "/api/skills/reload",
        "/api/teachings/cancel",
        "/api/teachings/choose",
        "/api/teachings/confirm",
        "/api/patches/{patch_id}/approve",
        "/api/patches/{patch_id}/reject",
        "/api/feedback",
        "/api/memory/{item_id}",
        "/api/memory",
        "/api/skills/{name}/rollback/{version}",
        "/api/features/self-evolution",
    }
    missing = expected - protected
    assert not missing, f"未加 owner_token 依赖的路由: {missing}"
