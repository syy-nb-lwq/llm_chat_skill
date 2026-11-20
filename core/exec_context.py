"""执行上下文 —— 当前这一轮"跑在哪个隔离空间里"。

## 为什么需要它

编排型架构下，同一时刻多个子智能体并行跑代码是常态。`python_exec`
若用全局单例执行目录 + 固定脚本名，并行任务会互相覆盖文件，导致数据错误。
所以必须有"隔离空间"的概念。

本项目是**本地单用户**工具，隔离维度只需要 `session_id` + `agent_id`：
不同会话、不同子智能体各有自己的执行目录，没必要引入 user_id / tenant_id。

## 为什么用 contextvars 而不是一路传参

工具函数的签名不该为了传上下文而变脏（`python_exec(code=...)`、
`web_fetch(url=...)` 这种签名是给 LLM 用的）。

`contextvars` 在 `asyncio.create_task()` 时会**复制**上下文，
所以 `asyncio.gather` 出来的并行子任务各自看到自己那份 —— 正好合用。

注意：`run_in_executor` / 线程池**不会**复制上下文，那种场景取不到，
读不到就退回默认作用域（不隔离），**不能因此抛异常**。

## 作用域键（scope_key）

隔离目录名 = `会话slug + 哈希`（agent 隔离由脚本名 execution_id 后缀承担）：

- 带会话前缀：人工排查时一眼看出是哪个会话的目录；
- 带哈希：避免不同 id 拼出同一个目录名（slug 化可能有碰撞）。

默认（没有任何上下文时）是 `default` —— 行为和以前一样。
"""
from __future__ import annotations

import hashlib
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Tuple

#: 当前这一轮的会话 / 执行 / 子智能体
_session_id: ContextVar[str] = ContextVar("dsh_exec_session", default="")
_execution_id: ContextVar[str] = ContextVar("dsh_exec_execution", default="")
_agent_id: ContextVar[str] = ContextVar("dsh_exec_agent", default="")


def _slug(value: str, *, max_len: int = 16) -> str:
    """把任意 id 变成安全的目录名片段。"""
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())[:max_len]
    return text or "anon"


@dataclass(frozen=True)
class ExecContext:
    """一次执行的隔离坐标。"""

    session_id: str = ""
    execution_id: str = ""
    agent_id: str = ""

    @property
    def scope_key(self) -> str:
        """隔离目录名。没有 session 时返回 `default`。"""
        if not self.session_id:
            return "default"
        digest = hashlib.sha1(self.session_id.encode("utf-8")).hexdigest()[:8]
        return f"{_slug(self.session_id)}_{digest}"

    @property
    def is_scoped(self) -> bool:
        """是否有真实的作用域（没有就是默认模式）。"""
        return bool(self.session_id)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "execution_id": self.execution_id,
            "agent_id": self.agent_id,
            "scope_key": self.scope_key,
        }


def set_exec_context(
    *,
    session_id: str = "",
    execution_id: str = "",
    agent_id: str = "",
) -> Tuple[Token, ...]:
    """设置当前执行上下文，返回可用于回滚的 token 组。

    只覆盖**显式传入**的字段（空串视为"不设置"），避免子智能体只想改
    agent_id 时把 session_id 抹掉。
    """
    tokens = []
    if session_id:
        tokens.append(_session_id.set(session_id))
    if execution_id:
        tokens.append(_execution_id.set(execution_id))
    if agent_id:
        tokens.append(_agent_id.set(agent_id))
    return tuple(tokens)


def reset_exec_context(tokens: Tuple[Token, ...]) -> None:
    """回滚上下文。传空组是安全的。"""
    for token in reversed(list(tokens or ())):
        try:
            token.var.reset(token)
        except Exception:
            pass


def get_exec_context() -> ExecContext:
    """读当前执行上下文。读不到就返回空上下文（不抛异常）。"""
    try:
        return ExecContext(
            session_id=_session_id.get() or "",
            execution_id=_execution_id.get() or "",
            agent_id=_agent_id.get() or "",
        )
    except Exception:
        return ExecContext()


def current_scope_key() -> str:
    """当前隔离作用域的目录名。"""
    return get_exec_context().scope_key


def current_agent_id() -> str:
    """当前智能体 id（子智能体运行时是它自己的，主智能体是 `main`）。"""
    return get_exec_context().agent_id


def scope_prefix(*, fallback: str = "default") -> str:
    """给 `owner_xxx` 这类文件名用的作用域前缀。"""
    ctx = get_exec_context()
    if not ctx.is_scoped:
        return fallback
    return ctx.scope_key
