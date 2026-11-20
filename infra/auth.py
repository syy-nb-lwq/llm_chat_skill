"""标识生成与（已移除的）鉴权层。

## 本地单用户

项目跑在使用者本地，只有他一个人 —— **没有多用户隔离，没有令牌，没有登录**。

因此这里只剩两件事：

1. 生成会话/连接标识（`gen_session_id` / `gen_client_id`）
2. `require_auth` —— 保留为 no-op 装饰器，兼容旧端点上的装饰器写法

历史实现里的 HMAC 会话令牌、用户令牌、假用户服务（`core/user_service.py`）
已随多用户体系一并移除。
"""
from __future__ import annotations

import secrets
import time

from infra.logger import get_logger

logger = get_logger()


# ===== 生成 =====


def gen_session_id() -> str:
    """生成一个会话 id（客户端可带回以恢复历史）。"""
    return f"s-{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def gen_client_id() -> str:
    """生成一个连接 id（服务端签发，只用于 topic 路由）。"""
    return f"c-{int(time.time() * 1000)}-{secrets.token_hex(6)}"


# ===== 兼容 =====


def require_auth(func):
    """no-op 装饰器（本地单用户无鉴权）。

    直接返回原函数 —— 不注入 ``request`` 参数（否则 FastAPI 会把它当成
    必填 query 参数，接口全部 422）。
    """
    return func
