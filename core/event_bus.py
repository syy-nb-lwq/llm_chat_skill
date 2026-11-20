"""事件总线 —— 让深处（工具、子智能体）也能往当前这一轮推事件。

## 为什么需要它

Agent 的 `handle(user_input, on_event)` 里有个 `on_event` 回调，用来把
`thinking` / `tool_call` / `message_delta` 推给前端。但**工具内部拿不到它**：

```
Agent.handle(on_event=...)
  -> LearningAgent.execute_dag(...)
     -> hub.call_tool("run_subagents", params)      # ← 这里断了
        -> SubAgentPool.run_many(...)               # 想推 subagent_progress，没渠道
```

一个个往下传参数会让所有工具签名都变脏。所以用 `contextvars`：
Agent 在一轮开始时把 `on_event` 放进上下文，任何深度的代码用
`await emit("...", {...})` 就能推出去。

## 一个要紧的细节：contextvars 与 asyncio 任务

`contextvars` 在 `asyncio.create_task()` 时会**复制**当前上下文给子任务，
所以子智能体（跑在 gather 出来的任务里）能读到同一个 emitter。这正是我们要的。

反过来，`run_in_executor` / 线程池**不会**复制上下文 —— 那种场景下取不到
emitter，`emit()` 就静默跳过（不能让观测失败影响业务）。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from infra.logger import get_logger


#: 当前轮的 emit 回调
_emitter: ContextVar[Optional[Callable[[str, dict], Awaitable[None]]]] = ContextVar(
    "dsh_event_emitter", default=None
)


def set_emitter(fn: Optional[Callable[[str, dict], Awaitable[None]]]) -> None:
    """在一轮开始时设置 emitter（Agent.handle 里调用）。"""
    _emitter.set(fn)


def clear_emitter() -> None:
    _emitter.set(None)


def get_emitter() -> Optional[Callable[[str, dict], Awaitable[None]]]:
    return _emitter.get()


async def emit(event: str, payload: dict) -> None:
    """往当前轮推一个事件。

    **永远不会抛异常**：观测是附加价值，它坏了不能拖垮任务。
    """
    fn = _emitter.get()
    if fn is None:
        return
    try:
        await fn(event, payload or {})
    except Exception as exc:
        try:
            get_logger().warning("EventBus", f"推送事件失败({event}): {exc}")
        except Exception:
            pass
