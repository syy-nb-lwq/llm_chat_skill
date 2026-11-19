"""智能体身份 —— 谁在跑、跑在哪、谁派的他。

## 为什么单独一个模块

`core/identity.py` 已经有 `IdentityContext`（session/turn/execution），
但那是**一轮对话**的身份。编排型架构还需要**智能体**的身份：

- 一个角色（角色）可以被派出去多次，每次是一次新的执行；
- 每个子智能体要知道自己的**父智能体**是谁（做审计、做依赖、做回溯）；
- 深度（depth）是硬约束的依据（子智能体不能再无限往下派）。

所以这里在 `IdentityContext` 之上加一层 **agent 维度**。

## 关系

```
IdentityContext（一次对话的一轮）      AgentIdentity（一个智能体实例）
  session_id                ←──→     session_id
  turn_id / execution_id              execution_id / agent_id
                                      parent_agent_id / root_agent_id / depth
```

`from_execution_context()` 负责把前者翻译成后者的起点。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@dataclass
class AgentIdentity:
    """一个智能体实例的身份。

    `agent_id` 与 `execution_id` **必须分开**：
    同一个角色被派出去做两件事（第一次失败后重试），是**两个执行**，
    但属于**同一个角色实例**（共享角色配置、共享私有记忆空间）。
    """

    agent_id: str = ""
    role_id: str = ""
    role_name: str = ""
    session_id: str = "default"
    execution_id: str = ""
    parent_agent_id: Optional[str] = None
    root_agent_id: str = ""
    depth: int = 0
    spawned_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.agent_id:
            self.agent_id = _new_id("agent")
        if not self.execution_id:
            self.execution_id = _new_id("exec")
        if not self.root_agent_id:
            self.root_agent_id = self.agent_id
        if not self.spawned_at:
            self.spawned_at = time.time()

    # ===== 派生 =====

    def child(
        self,
        role_id: str,
        role_name: str = "",
        *,
        execution_id: str = "",
    ) -> "AgentIdentity":
        """派出一个子智能体：深度 +1，父指向自己。

        **每次派生都是新的 agent_id 与新的 execution_id** ——
        重试、返工都算新的执行，不能覆盖上一次执行的记录。
        """
        return AgentIdentity(
            agent_id=_new_id("agent"),
            role_id=role_id,
            role_name=role_name or role_id,
            session_id=self.session_id,
            execution_id=execution_id or _new_id("exec"),
            parent_agent_id=self.agent_id,
            root_agent_id=self.root_agent_id or self.agent_id,
            depth=self.depth + 1,
        )

    # ===== 与现有执行上下文的桥接 =====

    @classmethod
    def from_execution_context(
        cls,
        ctx: Any,
        *,
        role_id: str = "main",
        role_name: str = "主智能体",
    ) -> "AgentIdentity":
        """从 `core.identity.IdentityContext` 造一个身份。

        `ctx.parent_execution_id`（重试/分支的父执行）会落到
        `parent_agent_id` —— 语义不完全等价，但**方向一致**：
        能回答"这一轮是从哪一轮派生出来的"。
        """
        parent = getattr(ctx, "parent_execution_id", "") or ""
        return cls(
            role_id=role_id,
            role_name=role_name,
            session_id=getattr(ctx, "session_id", "") or "",
            execution_id=getattr(ctx, "execution_id", "") or "",
            parent_agent_id=parent or None,
        )

    # ===== 序列化 =====

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "role_id": self.role_id,
            "role_name": self.role_name,
            "session_id": self.session_id,
            "execution_id": self.execution_id,
            "parent_agent_id": self.parent_agent_id,
            "root_agent_id": self.root_agent_id,
            "depth": self.depth,
            "spawned_at": self.spawned_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentIdentity":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})
