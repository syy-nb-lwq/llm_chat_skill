"""Orchestrator Agent - 流转中枢：整合数据、生成回答"""
from typing import Dict, Any, List, Optional
from tools.base import ToolResult
from skills.manager import Skill
from infra.llm import get_llm_client
from infra.logger import get_logger, LogType


class OrchestratorAgent:
    """
    Orchestrator Agent - 流转中枢
    
    职责：
    1. 整合数据 - 将工具获取的数据整合
    2. 生成回答 - 按技能方法论组织回答
    """
    
    SYSTEM_PROMPT = """你是一个回答生成助手。根据获取的数据和技能方法论，生成完整、有条理的回答。

技能方法论（如果有）：
{methodology}

工具数据：
{tool_data}

请按照技能方法论的步骤来组织回答。如果没有方法论，则直接基于数据生成回答。"""
    
    def __init__(self):
        self.llm = get_llm_client()
        self.logger = get_logger()
    
    def orchestrate(
        self,
        user_input: str,
        tool_results: Dict[str, ToolResult],
        selected_skill: Optional[Skill] = None
    ) -> str:
        """
        整合数据，生成回答
        
        作为流转中枢，按技能方法论组织数据生成回答
        
        Args:
            user_input: 用户输入
            tool_results: 工具执行结果
            selected_skill: 选择的技能（方法论）
        
        Returns:
            生成的最终回答
        """
        self.logger.log_flow("Orchestrator", "开始整合数据，生成回答")
        
        # 整理工具数据
        tool_data = self._format_tool_results(tool_results)
        
        # 如果有选中的技能，按方法论组织回答
        if selected_skill and selected_skill.method:
            return self._generate_with_methodology(
                user_input, 
                tool_data, 
                selected_skill.method,
                selected_skill.steps
            )
        
        # 否则直接生成回答
        return self._generate_direct(user_input, tool_data)
    
    def _generate_with_methodology(
        self,
        user_input: str,
        tool_data: str,
        method: str,
        steps: List[str]
    ) -> str:
        """按方法论生成回答"""
        # 构建 prompt
        prompt = f"""用户需求：{user_input}

技能方法论：
{method}

技能步骤：{" -> ".join(steps) if steps else "无"}

获取到的数据：
{tool_data}

请按上述方法论和步骤，生成一个完整、有条理的回答。"""
        
        messages = [
            {"role": "system", "content": "你是一个专业的助手，善于按照方法论组织信息。"},
            {"role": "user", "content": prompt}
        ]
        
        response = self.llm.chat(messages)
        
        self.logger.info(LogType.FLOW_STEP, "Orchestrator", "回答生成完成")
        
        return response
    
    def _generate_direct(self, user_input: str, tool_data: str) -> str:
        """直接生成回答（无技能方法论）"""
        prompt = f"""用户需求：{user_input}

获取到的数据：
{tool_data}

请基于这些数据生成一个准确、简洁的回答。"""
        
        messages = [
            {"role": "system", "content": "你是一个专业的助手。"},
            {"role": "user", "content": prompt}
        ]
        
        response = self.llm.chat(messages)
        
        self.logger.info(LogType.FLOW_STEP, "Orchestrator", "回答生成完成")
        
        return response
    
    def generate_response(self, user_input: str) -> str:
        """直接生成回答（不需要工具调用）"""
        messages = [
            {"role": "system", "content": "你是一个友好、专业的助手。"},
            {"role": "user", "content": user_input}
        ]
        
        return self.llm.chat(messages)
    
    def _format_tool_results(self, tool_results: Dict[str, ToolResult]) -> str:
        """格式化工具结果"""
        if not tool_results:
            return "(无数据)"
        
        lines = []
        for tool_name, result in tool_results.items():
            lines.append(f"### {tool_name}")
            if result.success:
                lines.append(result.content)
            else:
                lines.append(f"[错误] {result.error}")
            lines.append("")
        
        return "\n".join(lines)
