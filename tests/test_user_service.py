"""假用户服务(UserService)与用户令牌测试。

覆盖:
  - 用户签发 / 档案持久化 / 令牌解析
  - 同一令牌解析出同一用户(长期记忆跨连接连续的前提)
  - 无效令牌 → 新建用户
  - C-01:init 携带 user_token → user_id 稳定;不带 → 新用户
"""
import pytest

from core.user_service import UserService
from infra.auth import issue_user_token, verify_user_token

@pytest.fixture
def service(tmp_path):
    return UserService(base_path=tmp_path / "users")


# ===== 令牌 =====

def test_user_token_roundtrip(service):
    user = service.create(display_name="小明")
    token = service.issue_token(user.id, user.tenant_id)
    payload = verify_user_token(token)
    assert payload is not None
    assert payload["user_id"] == user.id
    assert payload["tenant_id"] == "default"


def test_user_token_rejects_garbage():
    assert verify_user_token("") is None
    assert verify_user_token("not-a-token") is None
    assert verify_user_token("a.b") is None


def test_user_token_tampered_body_rejected():
    """改 payload 里的 user_id → 签名不匹配 → 拒绝。"""
    token = issue_user_token("u-original")
    body, _, sig = token.partition(".")
    tampered = f"{body[:-2]}XY.{sig}"
    assert verify_user_token(tampered) is None


# ===== 档案 =====

def test_create_and_get(service):
    user = service.create(display_name="小红")
    loaded = service.get(user.id)
    assert loaded is not None
    assert loaded.display_name == "小红"
    assert loaded.tenant_id == "default"


def test_get_missing_returns_none(service):
    assert service.get("u-does-not-exist") is None


def test_get_or_create_with_valid_token_returns_same_user(service):
    user = service.create()
    token = service.issue_token(user.id, user.tenant_id)
    again = service.get_or_create(token)
    assert again.id == user.id


def test_get_or_create_without_token_creates_new_user(service):
    user = service.get_or_create("")
    assert user.id
    assert service.get(user.id) is not None


def test_get_or_create_with_invalid_token_creates_new_user(service):
    """令牌伪造 → 不复用身份,新建一个用户。"""
    user = service.get_or_create("forged.token")
    assert user.id
    assert verify_user_token("forged.token") is None


def test_persist_across_service_instances(tmp_path):
    """档案落盘:新进程(新实例)仍能读到同一用户。"""
    svc_a = UserService(base_path=tmp_path / "users")
    user = svc_a.create(display_name="持久用户")
    svc_b = UserService(base_path=tmp_path / "users")
    assert svc_b.get(user.id) is not None


def test_list_users(service):
    service.create(display_name="a")
    service.create(display_name="b")
    assert len(service.list_users()) == 2


def test_delete_user(service):
    user = service.create()
    assert service.delete(user.id) is True
    assert service.get(user.id) is None
    assert service.delete(user.id) is False


def test_update_settings(service):
    user = service.create()
    assert service.update_settings(user.id, {"lang": "zh"}) is True
    assert service.get(user.id).settings["lang"] == "zh"


def test_singleton(monkeypatch, tmp_path):
    import core.user_service as us_mod

    monkeypatch.setattr(
        us_mod, "UserService",
        lambda *a, **kw: UserService(base_path=tmp_path / "users"),
    )
    us_mod.reset_user_service()
    a = us_mod.get_user_service()
    b = us_mod.get_user_service()
    assert a is b
    us_mod.reset_user_service()


# ===== C-01 集成:身份稳定性 =====

@pytest.mark.asyncio
async def test_identity_stable_across_reconnects(monkeypatch, tmp_path):
    """带同一 user_token 反复 init → user_id 不变;不带 → 每次新用户。

    这是长期记忆能跨连接/跨重启连续的前提。
    """
    import core.user_service as us_mod

    monkeypatch.setattr(
        us_mod, "get_user_service",
        lambda: UserService(base_path=tmp_path / "users"),
    )

    from backend.session import SessionManager

    mgr = SessionManager()
    id1 = await mgr.issue_identity("conn-1")
    id2 = await mgr.issue_identity("conn-2", id1.user_token)
    id3 = await mgr.issue_identity("conn-3", id1.user_token)
    id4 = await mgr.issue_identity("conn-4")

    # 同一令牌 → 同一用户
    assert id2.user_id == id1.user_id
    assert id3.user_id == id1.user_id
    # 无令牌 → 新用户
    assert id4.user_id != id1.user_id
    # 但每次连接仍是新的会话身份
    assert id2.client_id != id1.client_id
    assert id2.session_id != id1.session_id
    # 用户令牌可用于换回同一用户
    assert id2.user_token == id1.user_token


@pytest.mark.asyncio
async def test_identity_payload_includes_user_token(monkeypatch, tmp_path):
    import core.user_service as us_mod

    monkeypatch.setattr(
        us_mod, "get_user_service",
        lambda: UserService(base_path=tmp_path / "users"),
    )
    from backend.session import SessionManager

    mgr = SessionManager()
    identity = await mgr.issue_identity("conn-x")
    payload = identity.to_payload()
    assert payload["user_token"]
    assert payload["tenant_id"] == "default"
    assert payload["user_id"] == identity.user_id
