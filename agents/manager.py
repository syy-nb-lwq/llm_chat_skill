"""Manager Agent - 流转中枢:意图识别、技能选择、任务规划"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from core.agent_base import BaseAgent
from skills.manager import Skill, get_skill_store


@dataclass
class PlanResult:
    """规划结果"""
    intent: str
    selected_skill: Optional["Skill"] = None
    tool_tasks: List[Dict] = field(default_factory=list)


_SIMPLE_INTENTS = ["hi", "hello", "你好", "thanks", "谢谢", "再见", "ok", "好的"]
_TEACHING_KEYWORDS = [
    "以后", "记住", "下次", "按这个", "按我的", "原则", "方法论",
    "教你", "应该", "步骤是", "正确做法",
]


class ManagerAgent(BaseAgent):
    """Manager Agent - 流转中枢

    职责:
    1. 意图识别
    2. 技能选择(方法论)
    3. 任务规划(工具调用)
    """

    name = "Manager"

    PLAN_SCHEMA = {
        "type": "object",
        "properties": {
            "intent":         {"type": "string"},
            "selected_skill": {"type": "string"},
            "tool_tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":     {"type": "string"},
                        "type":   {"type": "string", "enum": ["weather_query", "web_search"]},
                        "params": {"type": "object"},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                        "parallel_group": {"type": "string"},
                    },
                    "required": ["type", "params"],
                },
            },
        },
        "required": ["intent", "tool_tasks"],
    }

    def system_prompt(self) -> str:
        skills = self.skill_store.list_all()
        skills_list = self._format_skills(skills)
        return f"""你是一个任务规划助手。作为流转中枢,你需要:

1. 识别用户意图
2. 选择合适的技能(方法论)来完成任务
3. 规划需要的工具来获取数据

重要规则:
- 工具只能从以下列表选择:weather_query, web_search
- 不要创建不存在的工具
- 技能是完成任务的方法论,工具是获取数据的手段

已有技能列表:
{skills_list}

严格按以下 JSON 格式输出:
{{
  "intent": "用户意图简短描述",
  "selected_skill": "选择的技能名称(没有则填空字符串)",
  "tool_tasks": [
    {{"type": "weather_query", "params": {{"city": "城市名", "date": "日期"}}}},
    {{"type": "web_search", "params": {{"query": "搜索关键词"}}}}
  ]
}}"""

    def __init__(self):
        super().__init__()
        self.skill_store = get_skill_store()

    async def plan(self, user_input: str) -> PlanResult:
        """分析用户输入,产出 PlanResult"""
        self.logger.info("Manager", "开始意图识别和任务规划")
        skills = self.skill_store.list_all()
        try:
            plan = await self.think_json(user_input, self.PLAN_SCHEMA)
        except Exception as e:
            self.logger.error("Manager", f"规划失败,降级: {e}")
            return PlanResult(intent=user_input, selected_skill=None, tool_tasks=[])

        selected_skill = None
        skill_name = plan.get("selected_skill") or ""
        if skill_name:
            for s in skills:
                if s.name == skill_name:
                    selected_skill = s
                    break

        self.logger.info("Manager", f"识别意图: {plan.get('intent', '')}")
        if selected_skill:
            self.logger.info("Manager", f"选择技能: {selected_skill.name}")

        tool_tasks = self._auto_id_tool_tasks(plan.get("tool_tasks", []) or [])
        self.logger.info("Manager", f"规划工具: {len(tool_tasks)} 个")

        return PlanResult(
            intent=plan.get("intent", ""),
            selected_skill=selected_skill,
            tool_tasks=tool_tasks,
        )

    def _auto_id_tool_tasks(self, tasks: List[Dict]) -> List[Dict]:
        result = []
        for i, t in enumerate(tasks):
            task = dict(t)
            if not task.get("id"):
                task["id"] = f"t{i+1}"
            result.append(task)
        return result

    def should_answer_directly(self, user_input: str) -> bool:
        text = user_input.lower().strip()
        return any(intent in text for intent in _SIMPLE_INTENTS)

    def should_learn_skill(self, user_input: str) -> bool:
        return any(kw in user_input for kw in _TEACHING_KEYWORDS)

    def _format_skills(self, skills: List[Skill]) -> str:
        if not skills:
            return "(暂无技能)"
        lines = []
        for s in skills:
            lines.append(f"- {s.name}")
            lines.append(f"  能力: {s.capability}")
            lines.append(f"  方法: {s.method}")
            lines.append("")
        return "\n".join(lines)
