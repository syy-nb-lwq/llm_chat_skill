"""Identity & Execution 标识生成器

统一 user_id / session_id / turn_id / execution_id 四级标识，避免
不同模块各自拼字符串导致 trace 文件互相覆盖的问题。

设计：
- 标识只承担"区分身份/回合/执行"职责，不绑定任何存储路径前缀。
- 真正的存储路径由调用方决定，这样 e2e 测试可以用 tmp_path 隔离。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def new_id(prefix: str = "") -> str:
    """生成一个带可选前缀的短 id(时间戳 + 8 位随机)。"""
    suffix = uuid.uuid4().hex[:8]
    if prefix:
        return f"{prefix}-{int(time.time() * 1000)}-{suffix}"
    return f"{int(time.time() * 1000)}-{suffix}"


@dataclass
class IdentityContext:
    """一次请求的身份与执行上下文。

    由 Agent.handle() 在入口创建，确保每次 handle 调用拥有独立的
    execution_id，同一 session 多次 handle 不会相互覆盖记录。
    """

    user_id: str = "default"
    session_id: str = "default"
    turn_id: str = ""
    execution_id: str = ""
    parent_execution_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.turn_id:
            self.turn_id = new_id("turn")
        if not self.execution_id:
            self.execution_id = new_id("exec")

    def child(self) -> "IdentityContext":
        """为重试/分支派生 execution_id，同时保留父指针。"""
        return IdentityContext(
            user_id=self.user_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            execution_id=new_id("exec"),
            parent_execution_id=self.execution_id,
        )
