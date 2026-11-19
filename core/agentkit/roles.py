"""角色画像 —— 一个岗位是什么，以及它被授权做什么。

## 核心思想：角色不是提示词，是**配置**

常见做法是给每个角色写一段人设提示词，靠"提示模型请不要做 X"来约束。
本项目不这么做：**角色画像直接决定工具表、技能集、记忆范围、预算**，
运行时按画像**裁剪工具**（未授权的工具**不出现在 schema 里**）。

为什么：提示词约束是**软的** —— 模型会照做但不保证；而"工具根本不在表里"
是**硬的** —— 它想做也做不到。

## 能力模型（docs/18 §3）

十项能力，分四组。角色在每项能力上是"主责 / 受限 / 不做"三态：

- 认知：目标解析、情境装配、计划分解
- 执行：资源获取、执行与自检、恢复
- 协同：沟通、交接
- 自我约束：预算管理、升级与求助

`ROLE_CAPABILITY_MATRIX` 把这张表固化成代码，
`RoleProfile.validate()` 会检查角色声明的能力项是否合法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ===== 能力项 =====

CAP_GOAL_PARSE = "goal_parse"
CAP_CONTEXT_ASSEMBLE = "context_assemble"
CAP_PLAN_DECOMPOSE = "plan_decompose"
CAP_RESOURCE_ACQUIRE = "resource_acquire"
CAP_EXECUTE_SELFCHECK = "execute_selfcheck"
CAP_RECOVER = "recover"
CAP_COMMUNICATE = "communicate"
CAP_HANDOFF = "handoff"
CAP_BUDGET = "budget"
CAP_ESCALATE = "escalate"

#: 全部合法能力项
CAPABILITIES: Tuple[str, ...] = (
    CAP_GOAL_PARSE,
    CAP_CONTEXT_ASSEMBLE,
    CAP_PLAN_DECOMPOSE,
    CAP_RESOURCE_ACQUIRE,
    CAP_EXECUTE_SELFCHECK,
    CAP_RECOVER,
    CAP_COMMUNICATE,
    CAP_HANDOFF,
    CAP_BUDGET,
    CAP_ESCALATE,
)

#: 合法作用域
MEMORY_SCOPES: Tuple[str, ...] = ("agent", "session", "user", "global")

#: 合法副作用级别
SIDE_EFFECT_READ = "read"
SIDE_EFFECT_WRITE = "write"
SIDE_EFFECT_NET = "net"
SIDE_EFFECT_EXEC = "exec"
SIDE_EFFECT_MONEY = "money"
SIDE_EFFECTS: Tuple[str, ...] = (
    SIDE_EFFECT_READ,
    SIDE_EFFECT_WRITE,
    SIDE_EFFECT_NET,
    SIDE_EFFECT_EXEC,
    SIDE_EFFECT_MONEY,
)


class Capability:
    """能力项常量集合（用类属性便于 `Capability.PLAN_DECOMPOSE` 这样引用）。"""

    GOAL_PARSE = CAP_GOAL_PARSE
    CONTEXT_ASSEMBLE = CAP_CONTEXT_ASSEMBLE
    PLAN_DECOMPOSE = CAP_PLAN_DECOMPOSE
    RESOURCE_ACQUIRE = CAP_RESOURCE_ACQUIRE
    EXECUTE_SELFCHECK = CAP_EXECUTE_SELFCHECK
    RECOVER = CAP_RECOVER
    COMMUNICATE = CAP_COMMUNICATE
    HANDOFF = CAP_HANDOFF
    BUDGET = CAP_BUDGET
    ESCALATE = CAP_ESCALATE

    ALL = CAPABILITIES


# ===== 授权与预算 =====


@dataclass
class ToolGrant:
    """工具授权。

    `allow` 是**白名单** —— 不在表里的工具**不出现在这个角色的 schema 里**。
    `allow_all=True` 表示"不裁剪"（通才角色），此时 `allow` 只作说明用。
    """

    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)
    side_effect: str = SIDE_EFFECT_READ
    max_calls: int = 0          # 0 = 不限（仍受父预算约束）
    #: 不裁剪（= 全部已注册工具）。与"白名单为空"不同 —— 这是显式声明。
    allow_all: bool = False

    def allows(self, name: str) -> bool:
        if name in self.deny:
            return False
        if self.allow_all:
            return True
        return name in self.allow

    def validate(self) -> List[str]:
        problems: List[str] = []
        if not self.allow and not self.allow_all:
            problems.append("tools.allow 不能为空（没有工具的角色等于没有岗位）")
        if len(set(self.allow)) != len(self.allow):
            problems.append("tools.allow 里有重复项")
        if self.side_effect not in SIDE_EFFECTS:
            problems.append(
                f"tools.side_effect 非法: {self.side_effect}"
                f"（允许: {', '.join(SIDE_EFFECTS)}）"
            )
        if self.max_calls < 0:
            problems.append("tools.max_calls 不能为负")
        overlap = set(self.allow) & set(self.deny)
        if overlap:
            problems.append(f"工具同时在 allow 和 deny 里: {', '.join(sorted(overlap))}")
        return problems

    def to_dict(self) -> Dict:
        return {
            "allow": list(self.allow),
            "deny": list(self.deny),
            "side_effect": self.side_effect,
            "max_calls": self.max_calls,
            "allow_all": self.allow_all,
        }


@dataclass
class Budget:
    """预算。父子继承：**子预算每一维都不得超过父预算**。"""

    max_rounds: int = 4
    max_seconds: int = 120
    max_tool_calls: int = 20
    max_messages: int = 8
    max_context_chars: int = 12000
    max_tokens: int = 0        # 0 = 不限

    def fits_within(self, parent: "Budget") -> Tuple[bool, str]:
        """是否在父预算之内。返回 `(是否合法, 原因)`。

        `max_tokens == 0` 视为不限 —— 0 不参与比较。
        """
        checks = (
            ("max_rounds", self.max_rounds, parent.max_rounds),
            ("max_seconds", self.max_seconds, parent.max_seconds),
            ("max_tool_calls", self.max_tool_calls, parent.max_tool_calls),
            ("max_messages", self.max_messages, parent.max_messages),
            ("max_context_chars", self.max_context_chars, parent.max_context_chars),
        )
        for name, mine, theirs in checks:
            if mine > theirs:
                return False, f"预算超父：{name}={mine} > 父预算 {theirs}"
        if self.max_tokens and parent.max_tokens and self.max_tokens > parent.max_tokens:
            return False, f"预算超父：max_tokens={self.max_tokens} > 父预算 {parent.max_tokens}"
        return True, ""

    def to_dict(self) -> Dict:
        return {
            "max_rounds": self.max_rounds,
            "max_seconds": self.max_seconds,
            "max_tool_calls": self.max_tool_calls,
            "max_messages": self.max_messages,
            "max_context_chars": self.max_context_chars,
            "max_tokens": self.max_tokens,
        }


# ===== 角色画像 =====


@dataclass
class RoleProfile:
    """一个岗位的完整画像。"""

    role_id: str
    title: str = ""
    purpose: str = ""
    capabilities: List[str] = field(default_factory=list)
    tools: ToolGrant = field(default_factory=ToolGrant)
    skills: List[str] = field(default_factory=list)
    memory_scopes: List[str] = field(default_factory=lambda: ["agent", "session"])
    workspace: str = ""
    budget: Budget = field(default_factory=Budget)
    report_schema: str = "report.v1"
    escalate_when: List[str] = field(default_factory=list)

    def validate(self) -> List[str]:
        """返回问题列表（空 = 合法）。不抛异常 —— 调用方要一次性看到所有问题。"""
        problems: List[str] = []
        if not self.role_id or not self.role_id.strip():
            problems.append("role_id 不能为空")
        if not self.capabilities:
            problems.append("capabilities 至少要有 1 项")
        unknown = [c for c in self.capabilities if c not in CAPABILITIES]
        if unknown:
            problems.append(f"未知能力项: {', '.join(unknown)}")
        problems.extend(self.tools.validate())
        for skill in self.skills:
            if "@" not in skill or not skill.split("@", 1)[1].strip():
                problems.append(f"技能引用格式应为 name@version: {skill}")
        bad_scope = [s for s in self.memory_scopes if s not in MEMORY_SCOPES]
        if bad_scope:
            problems.append(
                f"非法记忆作用域: {', '.join(bad_scope)}"
                f"（允许: {', '.join(MEMORY_SCOPES)}）"
            )
        if not self.memory_scopes:
            problems.append("memory_scopes 不能为空")
        if not self.workspace:
            problems.append("workspace 不能为空（子智能体要有自己的工位）")
        return problems

    def to_dict(self) -> Dict:
        return {
            "role_id": self.role_id,
            "title": self.title,
            "purpose": self.purpose,
            "capabilities": list(self.capabilities),
            "tools": self.tools.to_dict(),
            "skills": list(self.skills),
            "memory_scopes": list(self.memory_scopes),
            "workspace": self.workspace,
            "budget": self.budget.to_dict(),
            "report_schema": self.report_schema,
            "escalate_when": list(self.escalate_when),
        }

    def as_prompt(self) -> str:
        """给 LLM 看的一句角色说明（**只是说明，不是授权**）。"""
        caps = "、".join(self.capabilities)
        return (
            f"{self.title or self.role_id}：{self.purpose}\n"
            f"  能力: {caps}\n"
            f"  可用工具: {', '.join(self.tools.allow) or '（无）'}\n"
            f"  记忆范围: {', '.join(self.memory_scopes)}"
        )


class RoleRegistry:
    """角色模板注册表。"""

    def __init__(self, roles: Optional[List[RoleProfile]] = None):
        self._roles: Dict[str, RoleProfile] = {}
        for role in roles or []:
            self.register(role)

    def register(self, profile: RoleProfile) -> None:
        if profile.role_id in self._roles:
            raise ValueError(f"角色已存在: {profile.role_id}")
        self._roles[profile.role_id] = profile

    def get(self, role_id: str) -> Optional[RoleProfile]:
        return self._roles.get(role_id)

    def names(self) -> List[str]:
        return list(self._roles.keys())

    def all(self) -> List[RoleProfile]:
        return list(self._roles.values())

    def as_prompts(self) -> Dict[str, str]:
        return {rid: p.as_prompt() for rid, p in self._roles.items()}


# ===== 预置角色 =====


def default_roles() -> List[RoleProfile]:
    """起步角色。

    **不要一次写十个** —— 角色多了以后没人记得住谁干什么，
    编排质量反而下降。先把这几个用顺，再按真实任务加。

    `generalist` 是**兜底角色**：工具白名单在运行时按当前已注册工具动态填充，
    所以它等价于"什么都能干，但仍然是受约束的岗位"（有预算、有工位、
    有上下文预算）。主智能体给子任务指定了别的角色，就按别的角色裁。
    """
    return [
        RoleProfile(
            role_id="generalist",
            title="通才",
            purpose="没有更合适的岗位时的默认执行者",
            capabilities=[
                Capability.GOAL_PARSE,
                Capability.CONTEXT_ASSEMBLE,
                Capability.RESOURCE_ACQUIRE,
                Capability.EXECUTE_SELFCHECK,
                Capability.RECOVER,
                Capability.COMMUNICATE,
                Capability.HANDOFF,
                Capability.BUDGET,
                Capability.ESCALATE,
            ],
            tools=ToolGrant(allow=[], allow_all=True, side_effect=SIDE_EFFECT_EXEC),
            memory_scopes=["agent", "session", "user"],
            workspace="workspace/generalist",
            budget=Budget(max_rounds=3, max_seconds=150, max_tool_calls=25),
            report_schema="report.v1",
        ),
        RoleProfile(
            role_id="researcher",
            title="研究员",
            purpose="检索与取证，产出带来源的事实清单",
            capabilities=[
                Capability.GOAL_PARSE,
                Capability.CONTEXT_ASSEMBLE,
                Capability.RESOURCE_ACQUIRE,
                Capability.COMMUNICATE,
                Capability.HANDOFF,
                Capability.BUDGET,
                Capability.ESCALATE,
            ],
            tools=ToolGrant(
                allow=["web_search", "web_fetch", "read_file", "read_document"],
                side_effect=SIDE_EFFECT_READ,
            ),
            memory_scopes=["agent", "session", "user"],
            workspace="workspace/researcher",
            budget=Budget(max_rounds=4, max_seconds=120, max_tool_calls=20),
            report_schema="report.v1",
            escalate_when=["搜索不到关键信息", "来源互相矛盾"],
        ),
        RoleProfile(
            role_id="implementer",
            title="实现工程师",
            purpose="写代码、造工具、跑通验证，产出可运行的东西",
            capabilities=[
                Capability.GOAL_PARSE,
                Capability.CONTEXT_ASSEMBLE,
                Capability.RESOURCE_ACQUIRE,
                Capability.EXECUTE_SELFCHECK,
                Capability.RECOVER,
                Capability.COMMUNICATE,
                Capability.HANDOFF,
                Capability.BUDGET,
                Capability.ESCALATE,
            ],
            tools=ToolGrant(
                allow=[
                    "python_exec", "write_script", "create_tool",
                    "read_file", "read_document",
                ],
                side_effect=SIDE_EFFECT_EXEC,
            ),
            memory_scopes=["agent", "session"],
            workspace="workspace/implementer",
            budget=Budget(max_rounds=5, max_seconds=180, max_tool_calls=30),
            report_schema="report.v1",
            escalate_when=["需要高风险权限", "连续两轮跑不通"],
        ),
        RoleProfile(
            role_id="reviewer",
            title="审查员",
            purpose="只读地检查交付物是否满足验收标准",
            capabilities=[
                Capability.CONTEXT_ASSEMBLE,
                Capability.RESOURCE_ACQUIRE,
                Capability.EXECUTE_SELFCHECK,
                Capability.COMMUNICATE,
                Capability.HANDOFF,
                Capability.BUDGET,
                Capability.ESCALATE,
            ],
            tools=ToolGrant(
                allow=["read_file", "read_document", "video_probe"],
                side_effect=SIDE_EFFECT_READ,
            ),
            memory_scopes=["agent", "session"],
            workspace="workspace/reviewer",
            budget=Budget(max_rounds=2, max_seconds=90, max_tool_calls=10),
            report_schema="report.v1",
            escalate_when=["发现交付物与验收标准不符"],
        ),
        RoleProfile(
            role_id="reporter",
            title="汇报员",
            purpose="把结论整理成用户可读的材料",
            capabilities=[
                Capability.CONTEXT_ASSEMBLE,
                Capability.COMMUNICATE,
                Capability.HANDOFF,
                Capability.BUDGET,
            ],
            tools=ToolGrant(
                allow=["read_file", "read_document"],
                side_effect=SIDE_EFFECT_READ,
            ),
            memory_scopes=["agent", "session", "user"],
            workspace="workspace/reporter",
            budget=Budget(max_rounds=2, max_seconds=90, max_tool_calls=12),
            report_schema="report.v1",
        ),
    ]


#: 能力 × 角色 授权矩阵（docs/18 §3.2）
#:
#: `●` 主责 ／ `◐` 受限 ／ `—` 不做
ROLE_CAPABILITY_MATRIX: Dict[str, Dict[str, str]] = {
    "main": {
        Capability.GOAL_PARSE: "●",
        Capability.CONTEXT_ASSEMBLE: "●",
        Capability.PLAN_DECOMPOSE: "●",
        Capability.RESOURCE_ACQUIRE: "—",
        Capability.EXECUTE_SELFCHECK: "—",
        Capability.RECOVER: "◐",
        Capability.COMMUNICATE: "●",
        Capability.HANDOFF: "●",
        Capability.BUDGET: "●",
        Capability.ESCALATE: "●",
    },
    "researcher": {
        Capability.GOAL_PARSE: "◐",
        Capability.CONTEXT_ASSEMBLE: "◐",
        Capability.PLAN_DECOMPOSE: "—",
        Capability.RESOURCE_ACQUIRE: "●",
        Capability.EXECUTE_SELFCHECK: "●",
        Capability.RECOVER: "●",
        Capability.COMMUNICATE: "◐",
        Capability.HANDOFF: "●",
        Capability.BUDGET: "◐",
        Capability.ESCALATE: "●",
    },
    "implementer": {
        Capability.GOAL_PARSE: "◐",
        Capability.CONTEXT_ASSEMBLE: "◐",
        Capability.PLAN_DECOMPOSE: "—",
        Capability.RESOURCE_ACQUIRE: "●",
        Capability.EXECUTE_SELFCHECK: "●",
        Capability.RECOVER: "●",
        Capability.COMMUNICATE: "◐",
        Capability.HANDOFF: "●",
        Capability.BUDGET: "◐",
        Capability.ESCALATE: "●",
    },
    "reviewer": {
        Capability.GOAL_PARSE: "—",
        Capability.CONTEXT_ASSEMBLE: "◐",
        Capability.PLAN_DECOMPOSE: "—",
        Capability.RESOURCE_ACQUIRE: "◐",
        Capability.EXECUTE_SELFCHECK: "●",
        Capability.RECOVER: "◐",
        Capability.COMMUNICATE: "◐",
        Capability.HANDOFF: "●",
        Capability.BUDGET: "◐",
        Capability.ESCALATE: "●",
    },
    "reporter": {
        Capability.GOAL_PARSE: "—",
        Capability.CONTEXT_ASSEMBLE: "◐",
        Capability.PLAN_DECOMPOSE: "—",
        Capability.RESOURCE_ACQUIRE: "—",
        Capability.EXECUTE_SELFCHECK: "—",
        Capability.RECOVER: "—",
        Capability.COMMUNICATE: "●",
        Capability.HANDOFF: "●",
        Capability.BUDGET: "◐",
        Capability.ESCALATE: "◐",
    },
}
