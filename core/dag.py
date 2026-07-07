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
                params=dict(step.input_schema) if step.input_schema else {},
                depends_on=list(step.depends_on),
                parallel_group=step.parallel_group,
                retry=step.retry,
                timeout_s=step.timeout_s,
                fallback_to=step.fallback,
            ))
        return tasks, issues

    async def run_skill(self, skill: Skill, user_input: str,
                        on_event=None) -> Dict[str, ToolResult]:
        """执行一个技能(DAG)"""
        tasks, issues = self.skill_to_tasks(skill)
        for i in issues:
            self.logger.warning("flow_step", "DAGExecutor", i)
        if not tasks:
            return {}
        return await self.learning.execute_dag(tasks, on_event)