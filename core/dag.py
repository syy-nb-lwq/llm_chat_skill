"""技能 DAG 执行器 - 把 Skill.steps 转成 ToolTask,交给 Learning 执行"""
from typing import Dict, List, Optional, Tuple

from agents.learning import ToolTask
from infra.logger import get_logger
from skills.models import Skill, SkillStep
from tools.base import ToolResult


class DAGExecutor:
    """把 Skill.steps 翻译成 ToolTask DAG,委托 LearningAgent 执行"""

    def __init__(self, learning):
        self.learning = learning
        self.logger = get_logger()

    def skill_to_tasks(self, skill: Skill) -> Tuple[List[ToolTask], List[str]]:
        """把 skill.steps 转成 ToolTask 列表。

        Returns:
            (tasks, issues)
            - tasks: 可执行的 ToolTask 列表
            - issues: 转换过程中发现的问题(空 tool / 未知 tool 等)
        """
        tasks: List[ToolTask] = []
        issues: List[str] = []

        tool_names = self.learning.registry.names()

        for step in skill.steps:
            if not step.tool:
                # 无 tool 的 step 是 LLM-only,跳过(由 Orchestrator 处理)
                issues.append(f"step {step.id}: 无 tool,DAG 不调度")
                continue
            if step.tool not in tool_names:
                issues.append(f"step {step.id}: 未知工具 {step.tool}")
                continue
            tasks.append(ToolTask(
                id=step.id,
                type=step.tool,
                params=dict(step.params) if getattr(step, "params", None) else {},
                depends_on=list(step.depends_on),
                parallel_group=step.parallel_group,
                retry=getattr(step, "retry", 0) or 0,
                timeout_s=getattr(step, "timeout_s", 30) or 30,
                fallback_to=getattr(step, "fallback", None),
            ))
        return tasks, issues

    async def run_skill(self, skill: Skill, user_input,
                        on_event=None) -> Dict[str, ToolResult]:
        """执行一个技能(DAG)。

        user_input 可以是 str 或 dict:
        - str:  作为 user_input.text 注入
        - dict: 整体注入,params 中可用 ${user_input.xxx}
        """
        tasks, issues = self.skill_to_tasks(skill)
        for i in issues:
            self.logger.warning("flow_step", "DAGExecutor", i)
        if not tasks:
            return {}
        if isinstance(user_input, str):
            user_input_ctx = {"text": user_input}
        else:
            user_input_ctx = user_input or {}
        return await self.learning.execute_dag(tasks, on_event, user_input=user_input_ctx)