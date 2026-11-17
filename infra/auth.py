"""身份与认证层(C-01)

职责:
- 从请求头解析或生成 user_id / session_id
- 管理端 API 加 owner token 验证
- WebSocket client_id 由服务端签发,不再信任客户端提交

使用方式:
- 装饰器 @require_auth 在管理 API 上做 owner token 校验
- get_user_from_request(req) / get_user_from_ws(ws) 从请求中提取身份
- gen_session_id() / gen_user_id() 生成唯一 ID
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import secrets
import time
from functools import wraps
from typing import Optional

from fastapi import HTTPException, Request

from infra.config import config
from infra.logger import get_logger

logger = get_logger()

# ===== 生成 =====

def gen_user_id() -> str:
    return f"u-{secrets.token_hex(8)}"


def gen_session_id() -> str:
    return f"s-{int(time.time()*1000)}-{secrets.token_hex(4)}"


def gen_client_id() -> str:
    return f"c-{int(time.time()*1000)}-{secrets.token_hex(6)}"


# ===== 会话令牌(C-01:服务端签发的身份) =====

_process_secret: Optional[bytes] = None


def _get_session_secret() -> bytes:
    """会话令牌签名密钥。

    优先用 ``SESSION_SECRET``;未配置时用进程级随机密钥
    (进程重启后旧令牌失效,客户端重新 init 即可)。

    **但这个"重新 init 即可"是骗人的**:客户端 localStorage 里存着
    user_token(长期身份凭证)。密钥一变,那个 token 就验不过 →
    服务端把它当成新用户 → 建新会话 → **用户看到的是"历史对话全没了"**。

    实机踩过:开发时反复重启后端,每重启一次就"丢一次历史"。

    所以:
    - 生产/长期使用**必须**配 `SESSION_SECRET`(固定值)
    - 没配时启动会打一条醒目的警告(见 `warn_if_unstable_secret`)
    """
    global _process_secret
    configured = (getattr(config, "session_secret", "") or "").strip()
    if configured:
        return configured.encode("utf-8")
    if _process_secret is None:
        _process_secret = secrets.token_bytes(32)
    return _process_secret


def warn_if_unstable_secret() -> bool:
    """没配 SESSION_SECRET 时警告一次。返回是否是稳定密钥。

    这不是"可选的优化" —— 它直接决定用户刷新/重启后还在不在同一份历史里。
    """
    configured = (getattr(config, "session_secret", "") or "").strip()
    if configured:
        return True
    try:
        from infra.logger import get_logger

        get_logger().warning(
            "Auth",
            "SESSION_SECRET 未配置:每次重启后端,所有已签发的令牌都会失效,"
            "用户会被当成新用户(表现为「历史对话没了」)。"
            "长期使用请在 .env 里配一个固定的 SESSION_SECRET。",
        )
    except Exception:
        pass
    return False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_session_token(user_id: str, session_id: str, client_id: str) -> str:
    """签发会话令牌。

    令牌内容为 ``base64url(payload).base64url(HMAC-SHA256)``,payload 绑定
    user_id / session_id / client_id。客户端后续请求带上它,服务端据此确认
    "我确实是当初被签发这个身份的连接",而不再只认客户端自带的 id。
    """
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "client_id": client_id,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    body = _b64e(raw)
    sig = hmac.new(_get_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_session_token(token: str) -> Optional[dict]:
    """校验会话令牌,返回 payload;无效返回 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    try:
        expected = hmac.new(
            _get_session_secret(), body.encode("ascii"), hashlib.sha256
        ).digest()
        given = _b64d(sig)
    except Exception:
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


# ===== 用户令牌(假用户服务的长期凭证) =====

def issue_user_token(user_id: str, tenant_id: str = "default") -> str:
    """签发用户令牌 —— 长期有效的用户身份凭证。

    与会话令牌的区别:
    - 会话令牌:一次 WS 连接的生命周期,绑定 conn_id
    - 用户令牌:长期,绑定 user_id;客户端持久化后每次连接都带上,
      服务端据此认出"这是同一个用户",从而让长期记忆跨重启/跨连接连续

    单机个人环境默认不过期;如需过期可给 payload 加 exp 字段。
    """
    payload = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    body = _b64e(raw)
    sig = hmac.new(_get_session_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_user_token(token: str) -> Optional[dict]:
    """校验用户令牌,返回 payload;无效返回 None。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    try:
        expected = hmac.new(
            _get_session_secret(), body.encode("ascii"), hashlib.sha256
        ).digest()
        given = _b64d(sig)
    except Exception:
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("user_id"):
        return None
    return payload


# ===== Owner Token =====

def get_owner_token() -> Optional[str]:
    """从 config 读取 owner token,未配置返回 None(表示不启用鉴权)。"""
    token = (getattr(config, "owner_token", "") or "").strip()
    return token or None


def _read_bearer(req) -> Optional[str]:
    if req is None:
        return None
    auth = ""
    # FastAPI Starlette Request
    if hasattr(req, "headers"):
        auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def require_auth(func):
    """管理 API 装饰器:需要提供有效的 owner token。

    - ``OWNER_TOKEN`` 未配置时跳过校验(单机个人环境的默认行为)。
    - 配置后,请求必须带 ``Authorization: Bearer <token>``。
    - 装饰器会向 FastAPI 签名注入 ``request`` 参数,端点本身无需声明;
      若端点自己声明了 ``request``,则原样透传。
    """
    accepts_request = "request" in inspect.signature(func).parameters

    @wraps(func)
    async def wrapper(*args, **kwargs):
        request = kwargs.get("request")
        if not accepts_request:
            kwargs.pop("request", None)
        expected = get_owner_token()
        if expected is None:
            # 未配置 token → 跳过校验(单机个人环境)
            logger.warning("Auth", "owner_token 未配置,跳过身份验证")
            return await func(*args, **kwargs)
        token = _read_bearer(request)
        if not token:
            raise HTTPException(status_code=401, detail="需要 Authorization: Bearer <token>")
        if not hmac.compare_digest(token, expected):
            raise HTTPException(status_code=403, detail="token 无效")
        return await func(*args, **kwargs)

    # 让 FastAPI 看到带 Request 的签名(必须在 @app.xxx 装饰器注册之前生效)
    sig = inspect.signature(func)
    if "request" not in sig.parameters:
        params = list(sig.parameters.values())
        params.append(
            inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request)
        )
        wrapper.__signature__ = sig.replace(parameters=params)
    return wrapper


# ===== 请求 → 身份 =====

def get_user_from_request(req) -> tuple[str, str]:
    """从 HTTP 请求中提取 user_id / session_id。

    优先级:
    1. header X-User-ID (显式传入)
    2. header X-Session-ID
    3. 生成新的并返回
    """
    headers = {}
    if hasattr(req, "headers"):
        headers = dict(req.headers)

    uid = headers.get("x-user-id", "").strip()
    sid = headers.get("x-session-id", "").strip()

    if not uid:
        uid = gen_user_id()
        logger.info("Auth", f"生成 user_id: {uid}")
    if not sid:
        sid = gen_session_id()

    return uid, sid


def get_user_from_ws(ws) -> tuple[str, str, str]:
    """从 WebSocket 连接中提取 user_id / session_id / client_id。

    client_id 由服务端 gen_client_id() 签发,不信任客户端提交。
    """
    headers = {}
    # Starlette WebSocket
    if hasattr(ws, "headers"):
        headers = dict(ws.headers)

    uid = headers.get("x-user-id", "").strip() or gen_user_id()
    sid = headers.get("x-session-id", "").strip() or gen_session_id()
    # 不接受客户端提交的 client_id
    cid = gen_client_id()
    return uid, sid, cid
