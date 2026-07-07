"""Orchestrator Agent - 流转中枢:整合数据、生成回答"""
from typing import Dict, List, Optional, AsyncIterator

from core.agent_base import BaseAgent
from infra.logger import LogType
from skills.manager import Skill
from tools.base import ToolResult


class OrchestratorAgent(BaseAgent):
    """Orchestrator Agent - 流转中枢

    职责:
    1. 整合数据 - 将工具获取的数据整合
    2. 生成回答 - 按技能方法论组织回答
    """

    name = "Orchestrator"

    def system_prompt(self) -> str:
        return """你是一个专业的回答生成助手。
根据用户需求和获取的数据,生成完整、有条理的回答。
若提供了技能方法论,请严格按方法论组织回答;若未提供,则直接基于数据生成回答。"""

    async def orchestrate(
        self,
        user_input: str,
        tool_results: Dict[str, ToolResult],
        selected_skill: Optional[Skill] = None,
    ) -> str:
        """整合数据,生成回答(异步)"""
        self.logger.log_flow("Orchestrator", "开始整合数据,生成回答")
        tool_data = self._format_tool_results(tool_results)
        if selected_skill and selected_skill.method:
            return await self._generate_with_methodology(
                user_input, tool_data, selected_skill.method, selected_skill.steps
            )
        return await self._generate_direct(user_input, tool_data)

    async def stream(
        self,
        user_input: str,
        tool_results: Dict[str, ToolResult],
        selected_skill: Optional[Skill] = None,
    ) -> AsyncIterator[str]:
        """流式输出(逐 token)"""
        tool_data = self._format_tool_results(tool_results)
        if selected_skill and selected_skill.method:
            prompt = self._build_prompt(user_input, tool_data,
                                        selected_skill.method, selected_skill.steps)
        else:
            prompt = self._build_direct_prompt(user_input, tool_data)

        async for token in self.llm.stream([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ]):
            yield token

    async def _generate_with_methodology(
        self, user_input: str, tool_data: str, method: str, steps: List[str]
    ) -> str:
        prompt = self._build_prompt(user_input, tool_data, method, steps)
        resp = await self.llm.chat_with_retry([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ])
        self.logger.info(LogType.FLOW_STEP, "Orchestrator", "回答生成完成")
        return resp

    async def _generate_direct(self, user_input: str, tool_data: str) -> str:
        prompt = self._build_direct_prompt(user_input, tool_data)
        resp = await self.llm.chat_with_retry([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ])
        self.logger.info(LogType.FLOW_STEP, "Orchestrator", "回答生成完成")
        return resp

    def _build_prompt(self, user_input, tool_data, method, steps):
        steps_str = " -> ".join(steps) if steps else "无"
        return f"""用户需求:{user_input}

技能方法论:
{method}

技能步骤:{steps_str}

获取到的数据:
{tool_data}

请按上述方法论和步骤,生成一个完整、有条理的回答。"""

    def _build_direct_prompt(self, user_input, tool_data):
        return f"""用户需求:{user_input}

获取到的数据:
{tool_data}

请基于这些数据生成一个准确、简洁的回答。"""

    # ----- 同步兼容 -----
    def generate_response(self, user_input: str) -> str:
        """直接生成(无工具)"""
        import asyncio
        async def _run():
            return await self.llm.chat_with_retry([
                {"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": user_input},
            ])
        return asyncio.run(_run())

    def _format_tool_results(self, tool_results: Dict[str, ToolResult]) -> str:
        if not tool_results:
            return "(无数据)"
        lines = []
        for name, r in tool_results.items():
            lines.append(f"### {name}")
            if r.success:
                lines.append(r.content if hasattr(r, "content") else str(r.data))
            else:
                lines.append(f"[错误] {r.error}")
            lines.append("")
        return "\n".join(lines)