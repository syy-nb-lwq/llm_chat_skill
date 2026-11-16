"""WebSocket 通信模块 - 基于 fastapi-websocket-pubsub"""
import asyncio
from typing import Any

from fastapi_websocket_pubsub import PubSubEndpoint, ALL_TOPICS

from infra.logger import get_logger


# ===== PubSub 端点 =====
endpoint = PubSubEndpoint()
logger = get_logger()

#: 事件推送的等待上限(秒)
#:
#: fastapi-websocket-rpc 是请求/响应式协议:服务端 publish() 会等客户端
#: 回 ACK 才返回。正常客户端会秒回;但客户端崩溃/断网/协议实现不全时,
#: 这个等待**没有超时**(库默认 None),会把 Agent 那一轮的事件投递永久卡住
#: —— 表现是"转圈不动"。
#:
#: 所以这里必须自己兜一个上限:超时就放弃这条事件,继续往下走。
PUSH_TIMEOUT = 10.0


# ===== 服务端主动推送 =====

async def push_event(client_id: str, event: str, payload: dict):
    """推一个事件给客户端。

    带超时:客户端不回 ACK 时不阻塞整轮流程(见 PUSH_TIMEOUT 说明)。
    """
    try:
        await asyncio.wait_for(
            endpoint.publish([f"events/{client_id}"], {
                "event": event,
                "payload": payload,
            }),
            timeout=PUSH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "ws", f"推送事件超时({PUSH_TIMEOUT}s),已放弃: {event} -> {client_id}"
        )
    except Exception as exc:
        logger.warning("ws", f"推送事件失败: {event} -> {client_id}: {exc}")


async def push_log(client_id: str, entry: dict):
    await endpoint.publish([f"log/{client_id}"], entry)


# ===== 业务处理器 (统一一个分发器) =====

async def dispatcher(subscription, data: Any):
    """所有 topic 的统一入口 (回调签名: (subscription, data))"""
    topic = subscription.topic
    # 忽略服务端自己推送的事件流,避免无限循环
    if topic.startswith("events/") or topic.startswith("log/"):
        return
    try:
        if topic.startswith("init/"):
            conn_id = topic[5:]
            # C-01:身份由服务端签发,不再信任客户端提交的 id
            # 假用户服务:带 user_token 则认出同一个用户,否则新建
            # 会话恢复:带 session_id 则恢复该会话(必须属于该用户)
            from backend.session import sessions

            identity = await sessions.issue_identity(
                conn_id,
                (data or {}).get("user_token", ""),
                (data or {}).get("session_id", ""),
            )
            await push_event(conn_id, "connected", identity.to_payload())
            logger.info("ws_manager", f"Client initialized: {identity.client_id}")
            return

        if topic.startswith("chat/"):
            conn_id = topic[5:]
            from backend.session import sessions

            identity = sessions.resolve_identity(conn_id, (data or {}).get("token", ""))
            if identity is None:
                await push_event(conn_id, "error", {"message": "连接未初始化或令牌无效"})
                return
            session = await sessions.get_or_create(
                identity.client_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
            )
            user_input = (data or {}).get("content", "")
            if not user_input.strip():
                await push_event(conn_id, "error", {"message": "content 为空"})
                return

            async def push(event: str, payload: dict):
                await push_event(conn_id, event, payload)
            await session.agent.handle(user_input, push)
            return

        if topic.startswith("reset/"):
            conn_id = topic[6:]
            from backend.session import sessions

            identity = sessions.resolve_identity(conn_id, (data or {}).get("token", ""))
            if identity is None:
                await push_event(conn_id, "error", {"message": "连接未初始化或令牌无效"})
                return
            session = await sessions.get_or_create(
                identity.client_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
            )
            session.agent.reset()
            await push_event(conn_id, "reset_ack", identity.to_payload())
            return

        if topic.startswith("ping/"):
            conn_id = topic[5:]
            await push_event(conn_id, "pong", {})
            return

        if topic.startswith("feedback/"):
            # 用户对某条回答打「👍 / 👎」——自演化最可靠的质量信号
            conn_id = topic[9:]          # "feedback/" 是 9 个字符
            from backend.session import sessions

            identity = sessions.resolve_identity(conn_id, (data or {}).get("token", ""))
            if identity is None:
                await push_event(conn_id, "error", {"message": "连接未初始化或令牌无效"})
                return
            session = await sessions.get_or_create(
                identity.client_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
            )
            execution_id = (data or {}).get("execution_id", "")
            accepted = bool((data or {}).get("accepted", False))
            comment = (data or {}).get("comment", "")
            ok = session.agent.apply_explicit_feedback(
                execution_id, accepted, comment
            )
            await push_event(conn_id, "feedback_ack", {
                "execution_id": execution_id,
                "accepted": accepted,
                "ok": ok,
            })
            return
    except Exception as e:
        logger.error("ws", f"dispatcher 处理 {topic} 失败: {e}")
        # 尝试从 topic 提取 client_id 推送错误
        if "/" in topic:
            parts = topic.split("/", 1)
            if parts[0] in ("init", "chat", "reset", "ping", "feedback"):
                client_id = parts[1]
                await push_event(client_id, "error", {"message": str(e)})


# ===== 注册订阅 =====

async def register_handlers():
    """注册 ALL_TOPICS 订阅 + 统一 dispatcher"""
    await endpoint.subscribe([ALL_TOPICS], dispatcher)
    logger.info("ws_manager", "PubSub dispatcher registered")