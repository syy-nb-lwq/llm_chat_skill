"""任务简报与报告包 —— 主智能体与子智能体之间**唯一**的两种契约。

## 为什么要有"契约"

没有契约时，主智能体派活靠一句自然语言（"你去查一下政策"），
子智能体回来靠一段散文（"我查到了一些东西"）。后果是：

- 子智能体不知道**做到什么程度算完成**（没有验收标准）；
- 主智能体不知道**哪些做成了、哪些没做成**（没有结构化状态）；
- 出问题没法复盘（没有 task_id 把派活和回报对上）。

所以定两个结构：**TaskBrief（派下去）** 与 **ReportPacket（交上来）**，
中间再有一个 **DelegationPlan（一批任务的依赖图）**。

## 三条硬规则

1. **简报必须有验收标准** —— 没有验收标准的任务不允许下发；
2. **报告必须自检** —— `status=ok` 但 `acceptance_check.passed=false`，
   在主智能体那里**不算成功**；
3. **状态必须如实** —— `partial/failed` 不许报成 `ok`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.agentkit.roles import Budget, RoleProfile

# ===== 报告状态 =====

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_BLOCKED = "blocked"
STATUS_CANCELLED = "cancelled"

STATUSES: Tuple[str, ...] = (
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
)


@dataclass
class TaskBrief:
    """主智能体 → 子智能体的任务简报。"""

    task_id: str
    goal: str
    #: 这个任务派给哪个角色（用于按角色裁剪工具/技能、校验授权）
    role_id: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    deliverable: Dict[str, Any] = field(default_factory=dict)
    #: 已装配好的上下文块（`ContextBlock.to_dict()` 的列表）
    context: List[Dict[str, Any]] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    #: 从黑板读哪些槽位
    inputs: List[str] = field(default_factory=list)
    #: 往黑板写哪些槽位
    outputs: List[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    escalate_when: List[str] = field(default_factory=list)
    report_schema: str = "report.v1"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "role_id": self.role_id,
            "acceptance_criteria": list(self.acceptance_criteria),
            "deliverable": dict(self.deliverable),
            "context": list(self.context),
            "tools": list(self.tools),
            "skills": list(self.skills),
            "depends_on": list(self.depends_on),
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "budget": self.budget.to_dict(),
            "escalate_when": list(self.escalate_when),
            "report_schema": self.report_schema,
        }


@dataclass
class ReportPacket:
    """子智能体 → 主智能体的报告包。"""

    task_id: str
    status: str = STATUS_FAILED
    summary: str = ""
    #: 结论要点（**压过**，不给原始大对象）
    findings: List[str] = field(default_factory=list)
    #: 证据（来源链接、文件、命令输出摘要）
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    #: 产物路径或引用
    artifacts: List[str] = field(default_factory=list)
    #: 自检结果：{"passed": bool, "items": [{"criterion": ..., "met": bool, "note": ...}]}
    acceptance_check: Dict[str, Any] = field(default_factory=dict)
    open_questions: List[str] = field(default_factory=list)
    confidence: float = 0.0
    tools_used: List[str] = field(default_factory=list)
    #: 自造工具的提案摘要
    proposed_tools: List[Dict[str, Any]] = field(default_factory=list)
    cost: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """真正成功 = 状态 ok **且** 自检通过。

        只有 `status == "ok"` 是不够的 —— 模型对自己的产出过于乐观
        （`docs/17` P1-6：5.6 秒的假成片被当成成片交付）。
        """
        if self.status != STATUS_OK:
            return False
        return bool(self.acceptance_check.get("passed"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "findings": list(self.findings),
            "evidence": list(self.evidence),
            "artifacts": list(self.artifacts),
            "acceptance_check": dict(self.acceptance_check),
            "open_questions": list(self.open_questions),
            "confidence": self.confidence,
            "tools_used": list(self.tools_used),
            "proposed_tools": list(self.proposed_tools),
            "cost": dict(self.cost),
        }


@dataclass
class DelegationPlan:
    """一批任务及其依赖图。"""

    tasks: List[TaskBrief] = field(default_factory=list)

    def task_ids(self) -> List[str]:
        return [t.task_id for t in self.tasks]

    def edges(self) -> List[Tuple[str, str]]:
        """从 depends_on 推导出的边：`[(上游, 下游), ...]`。"""
        out: List[Tuple[str, str]] = []
        for task in self.tasks:
            for dep in task.depends_on or []:
                out.append((dep, task.task_id))
        return out

    def validate(
        self,
        *,
        parent_budget: Optional[Budget] = None,
        roles: Optional[Dict[str, RoleProfile]] = None,
    ) -> List[str]:
        """校验整个计划。返回问题列表（空 = 合法）。"""
        problems: List[str] = []
        seen: Dict[str, TaskBrief] = {}
        for task in self.tasks:
            if task.task_id in seen:
                problems.append(f"task_id 重复: {task.task_id}")
            seen[task.task_id] = task

        for task in self.tasks:
            role = None
            if roles and task.role_id:
                role = roles.get(task.role_id)
            problems.extend(
                BriefValidator.validate_task(
                    task,
                    role=role,
                    parent_budget=parent_budget,
                )
            )
            for dep in task.depends_on or []:
                if dep not in seen:
                    problems.append(
                        f"任务 {task.task_id} 依赖不存在的任务: {dep}"
                    )

        problems.extend(_find_cycles(self.tasks))
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tasks": [t.to_dict() for t in self.tasks],
            "edges": [list(e) for e in self.edges()],
        }


class BriefValidator:
    """简报校验器 —— 校验不过就不许下发。"""

    @staticmethod
    def validate_task(
        brief: TaskBrief,
        *,
        role: Optional[RoleProfile] = None,
        parent_budget: Optional[Budget] = None,
    ) -> List[str]:
        problems: List[str] = []
        if not (brief.task_id or "").strip():
            problems.append("task_id 不能为空")
        if not (brief.goal or "").strip():
            problems.append("goal 为空")
        if not brief.acceptance_criteria:
            problems.append("缺少验收标准")
        if not brief.tools:
            problems.append("没有授权任何工具（子智能体会无事可做）")

        if parent_budget is not None:
            ok, reason = brief.budget.fits_within(parent_budget)
            if not ok:
                problems.append(reason)

        if role is not None:
            for tool in brief.tools:
                if not role.tools.allows(tool):
                    problems.append(
                        f"越权工具: {tool}"
                        f"（角色 {role.role_id} 只允许: "
                        f"{', '.join(role.tools.allow) or '（无）'}）"
                    )
            if role.skills:
                for skill in brief.skills:
                    if skill not in role.skills:
                        problems.append(
                            f"越权技能: {skill}"
                            f"（角色 {role.role_id} 只允许: {', '.join(role.skills)}）"
                        )
        return problems


def _find_cycles(tasks: Sequence[TaskBrief]) -> List[str]:
    """依赖成环检测（DFS 三色）。返回问题列表。"""
    graph: Dict[str, List[str]] = {t.task_id: list(t.depends_on or []) for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {node: WHITE for node in graph}
    problems: List[str] = []

    def dfs(node: str, stack: List[str]) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                cycle = stack[stack.index(dep):] + [dep]
                problems.append("依赖成环: " + " → ".join(cycle))
                return
            if color[dep] == WHITE:
                dfs(dep, stack)
        stack.pop()
        color[node] = BLACK

    for node in graph:
        if color[node] == WHITE:
            dfs(node, [])
    return problems
