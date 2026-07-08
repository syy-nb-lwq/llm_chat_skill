"""WebSocket 通信模块 - 基于 fastapi-websocket-pubsub"""
from typing import Any

from fastapi_websocket_pubsub import PubSubEndpoint, ALL_TOPICS

from infra.logger import get_logger


# ===== PubSub 端点 =====
endpoint = PubSubEndpoint()
logger = get_logger()


# ===== 服务端主动推送 =====

async def push_event(client_id: str, event: str, payload: dict):
    await endpoint.publish([f"events/{client_id}"], {
        "event": event,
        "payload": payload,
    })


async def push_log(client_id: str, entry: dict):
    await endpoint.publish([f"log/{client_id}"], entry)


# ===== 业务处理器 (统一一个分发器) =====

async def dispatcher(subscription, data: Any):
    """所有 topic 的统一入口 (回调签名: (subscription, data))"""
    topic = subscription.topic
    try:
        if topic.startswith("init/"):
            client_id = topic[5:]
            await push_event(client_id, "connected", {"client_id": client_id})
            logger.info("ws_manager", f"Client initialized: {client_id}")
            return

        if topic.startswith("chat/"):
            client_id = topic[5:]
            from backend.session import sessions
            session = await sessions.get_or_create(client_id)
            user_input = (data or {}).get("content", "")
            if not user_input.strip():
                await push_event(client_id, "error", {"message": "content 为空"})
                return
            async def push(event: str, payload: dict):
                await push_event(client_id, event, payload)
            await session.agent.handle(user_input, push)
            return

        if topic.startswith("reset/"):
            client_id = topic[6:]
            from backend.session import sessions
            session = await sessions.get_or_create(client_id)
            session.agent.reset()
            await push_event(client_id, "reset_ack", {})
            return

        if topic.startswith("ping/"):
            client_id = topic[5:]
            await push_event(client_id, "pong", {})
            return
    except Exception as e:
        logger.error("ws", f"dispatcher 处理 {topic} 失败: {e}")
        # 尝试从 topic 提取 client_id 推送错误
        if "/" in topic:
            client_id = topic.split("/", 1)[1]
            await push_event(client_id, "error", {"message": str(e)})


# ===== 注册订阅 =====

async def register_handlers():
    """注册 ALL_TOPICS 订阅 + 统一 dispatcher"""
    await endpoint.subscribe([ALL_TOPICS], dispatcher)
    logger.info("ws_manager", "PubSub dispatcher registered")