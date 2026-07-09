"""Orchestrator Agent - 流转中枢:整合数据、生成回答"""
from typing import Dict, List, Optional, AsyncIterator

from core.agent_base import BaseAgent
from core.context import Context
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
        context: Optional[Context] = None,
    ) -> str:
        """整合数据,生成回答(异步)"""
        tool_data = self._format_tool_results(tool_results)
        ctx_block = self._build_context_block(context)
        if selected_skill and selected_skill.method:
            return await self._generate_with_methodology(
                user_input, tool_data, selected_skill.method, selected_skill.steps,
                ctx_block=ctx_block,
            )
        return await self._generate_direct(user_input, tool_data, ctx_block=ctx_block)

    async def stream(
        self,
        user_input: str,
        tool_results: Dict[str, ToolResult],
        selected_skill: Optional[Skill] = None,
        context: Optional[Context] = None,
    ) -> AsyncIterator[str]:
        """流式输出(逐 token)"""
        tool_data = self._format_tool_results(tool_results)
        ctx_block = self._build_context_block(context)
        if selected_skill and selected_skill.method:
            prompt = self._build_prompt(user_input, tool_data,
                                        selected_skill.method, selected_skill.steps,
                                        ctx_block=ctx_block)
        else:
            prompt = self._build_direct_prompt(user_input, tool_data, ctx_block=ctx_block)

        async for token in self.llm.stream([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ]):
            yield token

    async def _generate_with_methodology(
        self, user_input: str, tool_data: str, method: str, steps: List,
        ctx_block: str = "",
    ) -> str:
        prompt = self._build_prompt(user_input, tool_data, method, steps, ctx_block=ctx_block)
        resp = await self.llm.chat_with_retry([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ])
        self.logger.info("Orchestrator", "回答生成完成")
        return resp

    async def _generate_direct(self, user_input: str, tool_data: str, ctx_block: str = "") -> str:
        prompt = self._build_direct_prompt(user_input, tool_data, ctx_block=ctx_block)
        resp = await self.llm.chat_with_retry([
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": prompt},
        ])
        self.logger.info("Orchestrator", "回答生成完成")
        return resp

    def _build_prompt(self, user_input, tool_data, method, steps, ctx_block: str = ""):
        if steps:
            rendered = []
            for s in steps:
                if hasattr(s, "name") and s.name:
                    rendered.append(s.name)
                elif hasattr(s, "id"):
                    rendered.append(s.id)
                else:
                    rendered.append(str(s))
            steps_str = " -> ".join(rendered)
        else:
            steps_str = "无"
        history = f"\n{ctx_block}\n" if ctx_block else ""
        return f"""用户需求:{user_input}
{history}
技能方法论:
{method}

技能步骤:{steps_str}

获取到的数据:
{tool_data}

请按上述方法论和步骤,生成一个完整、有条理的回答。"""

    def _build_direct_prompt(self, user_input, tool_data, ctx_block: str = ""):
        history = f"\n{ctx_block}\n" if ctx_block else ""
        return f"""用户需求:{user_input}
{history}
获取到的数据:
{tool_data}

请基于这些数据生成一个准确、简洁的回答。"""

    def _build_context_block(self, context: Optional[Context]) -> str:
        """把对话上下文历史拼接成块,供 LLM 看到之前几轮对话。

        空 / None / 首轮 → 返回空串(不影响 prompt)。
        """
        if context is None or len(context) == 0:
            return ""
        history = context.to_llm_messages(max_tokens=1500)
        recent = [
            m for m in history if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if len(recent) <= 1:
            return ""
        prior = recent[:-1]  # 去掉本轮 user_input
        if not prior:
            return ""
        lines = ["【对话历史】"]
        for m in prior[-6:]:
            role = "用户" if m["role"] == "user" else "助手"
            content = (m["content"] or "").strip()
            if len(content) > 400:
                content = content[:400] + "..."
            lines.append(f"- {role}: {content}")
        return "\n".join(lines)

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
