"""Manager Agent - 流转中枢：意图识别、技能选择、任务规划"""
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from infra.llm import get_llm_client
from skills.manager import Skill, get_skill_store
from infra.logger import get_logger, LogType


@dataclass
class PlanResult:
    """规划结果"""
    intent: str
    # 选择的技能（方法论）
    selected_skill: Optional["Skill"] = None
    # 需要的工具任务
    tool_tasks: List[Dict] = None
    # 是否需要用户指导
    needs_guidance: bool = False
    guidance_prompt: str = ""
    
    def __post_init__(self):
        if self.tool_tasks is None:
            self.tool_tasks = []


class ManagerAgent:
    """
    Manager Agent - 流转中枢
    
    职责：
    1. 意图识别 - 分析用户想要什么
    2. 技能选择 - 判断使用哪个技能（方法论）
    3. 任务规划 - 确定需要哪些工具来获取数据
    """
    
    SYSTEM_PROMPT = """你是一个任务规划助手。作为流转中枢，你需要：

1. 识别用户意图
2. 选择合适的技能（方法论）来完成任务
3. 规划需要的工具来获取数据

重要规则：
- 工具只能从以下列表选择：weather_query, web_search
- 不要创建不存在的工具
- 技能是完成任务的方法论，工具是获取数据的手段

已有技能列表：
{skills_list}

Output JSON format:
{{
  "intent": "用户意图简短描述",
  "selected_skill": "选择的技能名称（如果没有合适的可以为空）",
  "tool_tasks": [
    {{"type": "weather_query", "params": {{"city": "城市名", "date": "日期"}}},
    {{"type": "web_search", "params": {{"query": "搜索关键词"}}}}
  ]
}}"""
    
    def __init__(self):
        self.llm = get_llm_client()
        self.skill_store = get_skill_store()
        self.logger = get_logger()
    
    def analyze(self, user_input: str) -> PlanResult:
        """
        分析用户输入，规划任务
        
        作为流转中枢，协调 Skill 和 Tool 的使用
        """
        self.logger.log_flow("Manager", "开始意图识别和任务规划")
        
        # 获取所有技能
        skills = self.skill_store.list_all()
        
        # 格式化技能列表
        skills_list = self._format_skills(skills)
        
        # 调用 LLM 进行规划
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT.format(skills_list=skills_list)},
            {"role": "user", "content": f"用户输入: {user_input}"}
        ]
        
        response = self.llm.chat(messages)
        
        # 解析结果
        import json
        import re
        
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                plan = json.loads(json_match.group())
                
                # 查找选中的技能
                selected_skill_name = plan.get("selected_skill", "")
                selected_skill = None
                if selected_skill_name:
                    for skill in skills:
                        if skill.name == selected_skill_name:
                            selected_skill = skill
                            break
                
                self.logger.info(
                    LogType.AGENT_INTENT, 
                    "Manager", 
                    f"识别意图: {plan.get('intent', '')}"
                )
                
                if selected_skill:
                    self.logger.info(
                        LogType.AGENT_PLAN, 
                        "Manager", 
                        f"选择技能: {selected_skill.name}"
                    )
                
                self.logger.info(
                    LogType.AGENT_PLAN, 
                    "Manager", 
                    f"规划工具: {len(plan.get('tool_tasks', []))} 个"
                )
                
                return PlanResult(
                    intent=plan.get("intent", ""),
                    selected_skill=selected_skill,
                    tool_tasks=plan.get("tool_tasks", [])
                )
                
            except json.JSONDecodeError:
                pass
        
        # 默认规划
        return PlanResult(
            intent="信息查询",
            selected_skill=None,
            tool_tasks=[{"type": "weather_query", "params": {"city": "厦门", "date": "today"}}]
        )
    
    def should_answer_directly(self, user_input: str) -> bool:
        """检查是否应该直接回答"""
        simple_intents = ["hi", "hello", "你好", "thanks", "谢谢", "再见"]
        return any(intent in user_input.lower() for intent in simple_intents)
    
    def should_learn_skill(self, user_input: str) -> bool:
        """检查是否在教导技能"""
        guidance_keywords = ["应该这样做", "按我的方法", "正确做法", "教你", "学一下", "步骤是"]
        return any(kw in user_input for kw in guidance_keywords)
    
    def _format_skills(self, skills: List[Skill]) -> str:
        """格式化技能列表"""
        if not skills:
            return "(暂无技能)"
        
        lines = []
        for skill in skills:
            lines.append(f"- {skill.name}")
            lines.append(f"  能力: {skill.capability}")
            lines.append(f"  方法: {skill.method}")
            lines.append("")
        
        return "\n".join(lines)
