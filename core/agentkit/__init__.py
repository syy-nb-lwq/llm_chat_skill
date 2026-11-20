"""智能体基础设施（Agent Kit）—— 子智能体契约层 + 已接线的装配组件。

## 当前定位（2026-11-20 收口）

这个包**不是纯实验代码**：它有两个模块已被主链真实使用，其余是契约层/规则层。

**主链直接 import 的只有两个模块**：`roles`、`sources`。其余要么被这两个间接使用，
要么纯属契约层。

| 模块 | 状态 | 谁在用 |
|---|---|---|
| `roles` | ✅ 主链直接引用 | `core/subagents.py`（岗位注册表、角色解析）、`tools/subagent_tool.py`（岗位一览） |
| `sources` | ✅ 主链直接引用 | `core/subagents.py`（`ContextBuilder` 装配子智能体上下文） |
| `context` | ✅ 间接依赖 | 被 `sources` 使用（上下文装配） |
| `memory` | ✅ 间接依赖 | 被 `sources` 使用（`get_memory_facade`） |
| `identity` | 契约层 | 定义"谁在跑、谁派的、多深"；测试覆盖 |
| `brief` | 契约层 | TaskBrief / ReportPacket / DelegationPlan；测试覆盖 |
| `blackboard` | 契约层 | 共享白板；测试覆盖 |
| `messaging` | 契约层 | 消息总线与路由规则；测试覆盖 |

## 为什么这个划分是稳定的

`roles` / `context` / `sources` 回答的是**子智能体运行时必须回答的问题**
（谁来干、能看到什么、预算多少），所以主链直接依赖它们。

`identity` / `brief` / `blackboard` / `messaging` 回答的是"多智能体协作**将来**需要什么"，
目前主链只用单角色 + 事件推送，还没有协作场景 —— 它们作为契约先定义好，由
`tests/test_agentkit_contracts.py`、`test_f2_context_memory.py`、`test_f3_messaging.py` 守着。

## 不允许的漂移

不要把这里改回"完全不接主链"或"全部接进主链"的二元说法。
当前事实就是**部分接线**，上面的表格是唯一真相；
`tests/test_agentkit_contracts.py::test_declared_wiring_matches_reality` 会校验这张表。

对应 `docs/18-智能体基础设施规划.md`。
"""
from core.agentkit.brief import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_TIMEOUT,
    STATUSES,
    BriefValidator,
    DelegationPlan,
    ReportPacket,
    TaskBrief,
)
from core.agentkit.context import (
    BLOCK_LIMITS,
    DEFAULT_TOTAL,
    ContextAssembler,
    ContextBlock,
    ContextBundle,
)
from core.agentkit.identity import AgentIdentity
from core.agentkit.roles import (
    CAPABILITIES,
    ROLE_CAPABILITY_MATRIX,
    Budget,
    Capability,
    RoleProfile,
    RoleRegistry,
    ToolGrant,
    default_roles,
)

__all__ = [
    # identity
    "AgentIdentity",
    # roles
    "Budget",
    "Capability",
    "RoleProfile",
    "RoleRegistry",
    "ToolGrant",
    "CAPABILITIES",
    "ROLE_CAPABILITY_MATRIX",
    "default_roles",
    # brief
    "TaskBrief",
    "ReportPacket",
    "DelegationPlan",
    "BriefValidator",
    "STATUSES",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_FAILED",
    "STATUS_TIMEOUT",
    "STATUS_BLOCKED",
    "STATUS_CANCELLED",
    # context
    "ContextBlock",
    "ContextBundle",
    "ContextAssembler",
    "BLOCK_LIMITS",
    "DEFAULT_TOTAL",
]
