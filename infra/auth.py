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

import hashlib
import hmac
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


# ===== Owner Token =====

def get_owner_token() -> Optional[str]:
    """从 config 读取 owner token,不存在返回 None。"""
    return getattr(config, "owner_token", None) or None


def _read_bearer(req) -> Optional[str]:
    auth = ""
    # FastAPI Starlette Request
    if hasattr(req, "headers"):
        auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def require_auth(func):
    """管理 API 装饰器:需要提供有效的 owner token。"""
    @wraps(func)
    async def wrapper(req, *args, **kwargs):
        token = _read_bearer(req)
        expected = get_owner_token()
        if expected is None:
            # 未配置 token → 跳过校验(单机个人环境)
            logger.warning("Auth", "owner_token 未配置,跳过身份验证")
            return await func(req, *args, **kwargs)
        if not token:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="需要 Authorization: Bearer <token>")
        if not hmac.compare_digest(token, expected):
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="token 无效")
        return await func(req, *args, **kwargs)
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


def extract_ws_identity(data: dict, headers: dict | None = None) -> tuple[str, str, str]:
    """从 PubSub 消息体/headers 提取身份(C-01)。

    PubSub dispatcher 拿不到原始 ws 对象,只能从 data 和 topic 解析。
    - user_id / session_id 优先从 data / headers 取,缺失则服务端签发
    - server_client_id 始终由服务端签发,回传给前端选用
    """
    data = data or {}
    headers = headers or {}
    uid = str(data.get("user_id") or headers.get("x-user-id") or "").strip() or gen_user_id()
    sid = str(data.get("session_id") or headers.get("x-session-id") or "").strip() or gen_session_id()
    # 始终签发服务端 client_id,前端收到后可更新本地
    server_cid = gen_client_id()
    return uid, sid, server_cid


async def require_owner_token(request: Request) -> None:
    """FastAPI 依赖:管理 API 的 owner token 校验(C-01)。

    用法: ``@app.post(..., dependencies=[Depends(require_owner_token)])``

    - 未配置 ``owner_token`` → 放行(单机个人环境,不破坏现有)
    - 配置了 → 请求头必须带 ``Authorization: Bearer <token>``,否则 401/403
    """
    expected = get_owner_token()
    if expected is None:
        return
    token = _read_bearer(request)
    if not token:
        raise HTTPException(status_code=401, detail="需要 Authorization: Bearer <token>")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="token 无效")
