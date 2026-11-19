"""智能体基础设施（Agent Kit）—— 契约层。

这里的模块**不接主链**，只定义"智能体这件事"的结构与规则：

| 模块 | 是什么 |
|---|---|
| `identity` | 智能体身份（谁在跑、谁派的他、多深） |
| `roles` | 岗位画像（能力矩阵、工具授权、预算、预置角色） |
| `brief` | 任务简报 / 报告包 / 编排图 —— 主与子之间唯一的契约 |
| `context` | 上下文装配器（带来源、理由、预算、裁剪记录） |

对应 `docs/18-智能体基础设施规划.md` 的 F1 阶段。

**为什么先做这些**：上面每一项现在都是散落的约定（提示词里的"必须"、
工具表的全量注入、子智能体自己现编上下文）。把它们变成**机器可校验的数据结构**，
后面接主链才有地基，否则接一次改一次。
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
