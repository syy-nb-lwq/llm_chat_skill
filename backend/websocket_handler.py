"""WebSocket 通信模块 - 基于 fastapi-websocket-pubsub"""
import asyncio
import time
from typing import Any

from fastapi_websocket_pubsub import PubSubEndpoint, ALL_TOPICS

from core.events import (
    EV_CONNECTED,
    EV_ERROR,
    EV_FEEDBACK_ACK,
    EV_MESSAGE_DELTA,
    EV_PONG,
    EV_RESET_ACK,
)
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


#: 流式增量的合并参数。
#:
#: **为什么必须合并**:一次长回答会产生上千个 `message_delta`,
#: 每个都单独 publish + 等 ACK。实测一条 1900 字的回答推了 **1594 个 delta**,
#: 服务端日志里 1594 条"推送事件超时" —— 每条都要等满 10 秒超时窗口
#: (客户端正常时会秒回,但这么多往返本身就很浪费)。
#:
#: 按"攒够多少字 / 隔多久"合并后,同样一条回答只需要几十次推送。
DELTA_MAX_CHARS = 320      # 攒够这么多字就发
DELTA_MAX_INTERVAL = 0.12  # 或者距上次发送超过这么久就发


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


class DeltaCoalescer:
    """把 `message_delta` 攒成批再推。

    ## 为什么需要它

    一次长回答会产生上千个 `message_delta`。每个都单独
    `publish()` + 等 ACK —— 实测一条 1900 字的回答推了 **1594 次**,
    日志里刷出 1594 条"推送事件超时"。这不只是开销大:
    每个事件都有独立的 10 秒超时窗口,客户端一卡,整轮就被拖慢。

    合并后同样一条回答只需要几十次推送。

    ## 顺序不能乱

    非增量事件(`tool_call` / `message_final` / `error`)必须排在
    已缓存的增量**之后** —— 否则前端会先看到最终回答、再收到零散的
    增量,画面会倒流。

    所以 `send()` 遇到非增量事件时会**先 flush 再发**。

    ## 用法

        batcher = DeltaCoalescer(client_id)
        try:
            await agent.handle(user_input, batcher.send)
        finally:
            await batcher.flush()   # 收尾:把最后一点没发出去的发掉
    """

    def __init__(
        self,
        client_id: str,
        *,
        max_chars: int = DELTA_MAX_CHARS,
        max_interval: float = DELTA_MAX_INTERVAL,
    ):
        self.client_id = client_id
        self.max_chars = max(1, int(max_chars))
        self.max_interval = max(0.0, float(max_interval))
        self._buf: list = []
        self._size = 0
        self._last_flush = time.monotonic()
        #: 统计:收到多少个增量、真正推了多少次(用于日志/测试)
        self.deltas_in = 0
        self.pushes_out = 0

    async def send(self, event: str, payload: dict) -> None:
        """Agent 的 push 回调入口。"""
        if event == "message_delta":
            delta = str((payload or {}).get("delta") or "")
            if delta:
                self._buf.append(delta)
                self._size += len(delta)
                self.deltas_in += 1
            # 攒够了或者等太久了就发;否则继续攒
            if self._size >= self.max_chars or self._due():
                await self.flush()
            return

        # 非增量事件:先把缓存的增量冲出去,保证顺序
        await self.flush()
        await push_event(self.client_id, event, payload)

    def _due(self) -> bool:
        if self.max_interval <= 0:
            return True
        return (time.monotonic() - self._last_flush) >= self.max_interval

    async def flush(self) -> None:
        """把缓存的增量作为**一个** message_delta 推出去。"""
        if not self._buf:
            self._last_flush = time.monotonic()
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._size = 0
        self._last_flush = time.monotonic()
        self.pushes_out += 1
        await push_event(self.client_id, EV_MESSAGE_DELTA, {"delta": text})


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
            # 身份由服务端签发；会话恢复靠客户端带回的 session_id
            from backend.session import sessions

            identity = await sessions.issue_identity(
                conn_id,
                (data or {}).get("session_id", ""),
            )
            await push_event(conn_id, EV_CONNECTED, identity.to_payload())
            logger.info("ws_manager", f"Client initialized: {identity.client_id}")
            return

        if topic.startswith("chat/"):
            conn_id = topic[5:]
            from backend.session import sessions

            identity = sessions.resolve_identity(conn_id)
            if identity is None:
                await push_event(conn_id, EV_ERROR, {"message": "连接未初始化"})
                return
            session = await sessions.get_or_create(
                identity.client_id,
                session_id=identity.session_id,
            )
            user_input = (data or {}).get("content", "")
            if not user_input.strip():
                await push_event(conn_id, EV_ERROR, {"message": "content 为空"})
                return

            # 流式增量合并后再推 —— 一条长回答会产生上千个 delta,
            # 逐个 publish + 等 ACK 是纯浪费(实测 1594 次)。
            batcher = DeltaCoalescer(conn_id)

            async def run_turn() -> None:
                try:
                    await session.agent.handle(user_input, batcher.send)
                except Exception as exc:
                    # 这一轮在 Session 的 worker 里跑，dispatcher 的 try/except
                    # 已经兜不到了，必须就地报给前端
                    logger.error("ws", f"处理 chat 失败: {exc}")
                    await push_event(conn_id, EV_ERROR, {"message": str(exc)})
                finally:
                    # 必须收尾:最后一点没攒够的增量要发出去,
                    # 否则前端会永远缺一小段(通常正好是回答的最后一句)。
                    await batcher.flush()
                    if batcher.deltas_in:
                        logger.info(
                            "ws",
                            f"增量合并: {batcher.deltas_in} 个 delta → "
                            f"{batcher.pushes_out} 次推送",
                        )

            # **投递后立刻返回**：整轮在 Session 自己的队列里串行执行。
            # 不能在这里 await —— dispatcher 是所有连接共用的一条回调，
            # 一次 60s 的回答会把其他人的 ping/init 一起卡住。
            if not session.submit(run_turn):
                await push_event(
                    conn_id, EV_ERROR, {"message": "会话已回收，请刷新页面重试"}
                )
            return

        if topic.startswith("reset/"):
            conn_id = topic[6:]
            from backend.session import sessions

            identity = sessions.resolve_identity(conn_id)
            if identity is None:
                await push_event(conn_id, EV_ERROR, {"message": "连接未初始化"})
                return
            session = await sessions.get_or_create(
                identity.client_id,
                session_id=identity.session_id,
            )

            async def run_reset() -> None:
                # reset 也排队：不能在一轮回答跑到一半时清空 Context
                async with session.lock:
                    session.agent.reset()
                await push_event(conn_id, EV_RESET_ACK, identity.to_payload())

            if not session.submit(run_reset):
                await push_event(
                    conn_id, EV_ERROR, {"message": "会话已回收，请刷新页面重试"}
                )
            return

        if topic.startswith("ping/"):
            conn_id = topic[5:]
            await push_event(conn_id, EV_PONG, {})
            return

        if topic.startswith("feedback/"):
            # 用户对某条回答打「👍 / 👎」——自演化最可靠的质量信号
            conn_id = topic[9:]          # "feedback/" 是 9 个字符
            from backend.session import sessions

            identity = sessions.resolve_identity(conn_id)
            if identity is None:
                await push_event(conn_id, EV_ERROR, {"message": "连接未初始化"})
                return
            session = await sessions.get_or_create(
                identity.client_id,
                session_id=identity.session_id,
            )
            execution_id = (data or {}).get("execution_id", "")
            accepted = bool((data or {}).get("accepted", False))
            comment = (data or {}).get("comment", "")

            async def run_feedback() -> None:
                # 反馈会改 trace/记忆，和正在跑的一轮必须互斥
                async with session.lock:
                    ok = session.agent.apply_explicit_feedback(
                        execution_id, accepted, comment
                    )
                await push_event(conn_id, EV_FEEDBACK_ACK, {
                    "execution_id": execution_id,
                    "accepted": accepted,
                    "ok": ok,
                })

            if not session.submit(run_feedback):
                await push_event(
                    conn_id, EV_ERROR, {"message": "会话已回收，请刷新页面重试"}
                )
            return
    except Exception as e:
        logger.error("ws", f"dispatcher 处理 {topic} 失败: {e}")
        # 尝试从 topic 提取 client_id 推送错误
        if "/" in topic:
            parts = topic.split("/", 1)
            if parts[0] in ("init", "chat", "reset", "ping", "feedback"):
                client_id = parts[1]
                await push_event(client_id, EV_ERROR, {"message": str(e)})


# ===== 注册订阅 =====

async def register_handlers():
    """注册 ALL_TOPICS 订阅 + 统一 dispatcher"""
    await endpoint.subscribe([ALL_TOPICS], dispatcher)
    logger.info("ws_manager", "PubSub dispatcher registered")