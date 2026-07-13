"""Manager Agent - 流转中枢:意图识别、技能选择、任务规划"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from core.agent_base import BaseAgent
from core.context import Context
from core.memory import get_memory_store
from skills.manager import Skill, get_skill_store


@dataclass
class PlanResult:
    """规划结果"""
    intent: str                    # 意图类型: chitchat / skill / teach / unknown
    intent_detail: str = ""        # 意图详情描述
    selected_skill: Optional["Skill"] = None
    tool_tasks: List[Dict] = field(default_factory=list)


# ===== 意图类型常量 =====
class IntentType:
    CHITCHAT = "chitchat"     # 闲聊:问候、感谢、道别等
    SKILL = "skill"           # 技能需求:需要执行工具完成任务
    TEACH = "teach"           # 教导:用户教 Agent 新技能
    UNKNOWN = "unknown"       # 未知:需要进一步分析


# ===== 闲聊意图关键词 =====
_CHITCHAT_GREETINGS = [
    "hi", "hello", "你好", "您好", "嗨", "hey", "yo", "哟",
    "早上好", "下午好", "晚上好", "晚安",
]
_CHITCHAT_THANKS = [
    "谢谢", "感谢", "thx", "thanks", "谢啦", "多谢", "感恩",
]
_CHITCHAT_FAREWELL = [
    "再见", "拜拜", "bye", "下次见", "回头见", "告辞",
]
_CHITCHAT_AFFIRMATIVE = [
    "ok", "好的", "好", "嗯", "行", "可以", "没问题", "收到",
    "了解", "明白", "知道了", "好的好的",
]
_CHITCHAT_NEGATIVE = [
    "不要", "不用", "算了", "算了算了",
]
_CHITCHAT_RANDOM = [
    "你是谁", "你叫什么", "你会什么", "你是机器人吗",
    "今天天气", "现在几点", "你是干嘛的",
    "在吗", "在不在", "有人吗",
]
# 组合闲聊关键词
_CHITCHAT_KEYWORDS = (
    _CHITCHAT_GREETINGS + _CHITCHAT_THANKS + _CHITCHAT_FAREWELL +
    _CHITCHAT_AFFIRMATIVE + _CHITCHAT_NEGATIVE + _CHITCHAT_RANDOM
)


# ===== 教导意图关键词 =====
_TEACHING_KEYWORDS = [
    "以后", "记住", "下次", "按这个", "按我的", "原则", "方法论",
    "教你", "应该", "步骤是", "正确做法", "这么做",
    "遇到", "情况", "要", "才能",
]


# ===== 闲聊判断:最短/最简单输入 =====
_SHORT_CHITCHAT_THRESHOLD = 5   # 字符数阈值(避免误判中文短词)


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
        self.memory_store = get_memory_store()

    async def plan(self, user_input: str, context: Optional[Context] = None) -> PlanResult:
        """分析用户输入,产出 PlanResult。

        意图识别优先级:
        1. 闲聊(chitchat) - 直接返回,不调用工具
        2. 教导(teach) - 进入技能学习流程
        3. 技能(skill) - 调用工具执行任务
        4. 未知(unknown) - 需要进一步分析

        Args:
            user_input: 本轮用户输入
            context:    完整对话上下文(可选)。传入后,最近几条历史会作为额外
                        上下文提供给 LLM,以支持多轮对话。
        """
        self.logger.info("Manager", "开始意图识别和任务规划")

        # ===== 第一步:快速闲聊检测(不调用 LLM) =====
        chitchat_type, chitchat_detail = self._detect_chitchat(user_input)
        if chitchat_type:
            self.logger.info("Manager", f"闲聊检测: {chitchat_detail}")
            return PlanResult(
                intent=IntentType.CHITCHAT,
                intent_detail=chitchat_detail,
                selected_skill=None,
                tool_tasks=[],
            )

        # ===== 第二步:教导检测 =====
        if self.should_learn_skill(user_input):
            self.logger.info("Manager", "教导检测: 检测到教导意图")
            return PlanResult(
                intent=IntentType.TEACH,
                intent_detail="用户教导新技能",
                selected_skill=None,
                tool_tasks=[],
            )

        # ===== 第三步:技能需求分析(调用 LLM) =====
        skills = self.skill_store.list_all()

        # 读取历史教训 hints,拼到 user_input 前面
        enriched_input = self._enrich_with_hints(user_input)

        # 把多轮上下文拼接进来,token 超限由 to_llm_messages 内部截断
        try:
            plan = await self.think_json(
                self._build_user_prompt(enriched_input, context),
                self.PLAN_SCHEMA,
            )
        except Exception as e:
            self.logger.error("Manager", f"规划失败,降级: {e}")
            return PlanResult(
                intent=IntentType.UNKNOWN,
                intent_detail=f"LLM 调用失败: {str(e)[:50]}",
                selected_skill=None,
                tool_tasks=[],
            )

        selected_skill = None
        skill_name = plan.get("selected_skill") or ""
        if skill_name:
            for s in skills:
                if s.name == skill_name:
                    selected_skill = s
                    break

        intent_detail = plan.get("intent", "")
        self.logger.info("Manager", f"识别意图: {intent_detail}")
        if selected_skill:
            self.logger.info("Manager", f"选择技能: {selected_skill.name}")

        tool_tasks = self._auto_id_tool_tasks(plan.get("tool_tasks", []) or [])
        self.logger.info("Manager", f"规划工具: {len(tool_tasks)} 个")

        # ===== 判断是否为闲聊(即使有 tool_tasks 规划) =====
        # 如果工具规划为空且意图简单,仍归类为闲聊
        if not tool_tasks and self._is_simple_intent(intent_detail):
            return PlanResult(
                intent=IntentType.CHITCHAT,
                intent_detail=intent_detail,
                selected_skill=None,
                tool_tasks=[],
            )

        return PlanResult(
            intent=IntentType.SKILL if tool_tasks else IntentType.UNKNOWN,
            intent_detail=intent_detail,
            selected_skill=selected_skill,
            tool_tasks=tool_tasks,
        )

    def _build_user_prompt(self, user_input: str, context: Optional[Context]) -> str:
        """把上下文历史对话拼到当前 user_input。

        不会破坏现有调用——若 context 为空/None,直接返回原 user_input。
        """
        if context is None or len(context) == 0:
            return user_input
        history = context.to_llm_messages(max_tokens=2000)
        if not history:
            return user_input
        # 跳过 system 和 tool 类型,只保留可见的对话
        recent = [
            m for m in history if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if len(recent) <= 1:
            return user_input
        # 最后一条是当前 user_input 的内容(Agent.handle 已 add_user_message);
        # 把它剔除,只拼历史。
        prior = recent[:-1]
        if not prior:
            return user_input
        lines = ["以下是此前的对话历史(用户/助手),可能与本次任务相关:"]
        for m in prior[-6:]:
            role = "用户" if m["role"] == "user" else "助手"
            content = (m["content"] or "").strip()
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"- {role}: {content}")
        lines.append("")
        lines.append(f"本次用户输入: {user_input}")
        lines.append("请结合对话历史,识别本轮真实意图。")
        return "\n".join(lines)

    def _auto_id_tool_tasks(self, tasks: List[Dict]) -> List[Dict]:
        result = []
        for i, t in enumerate(tasks):
            task = dict(t)
            if not task.get("id"):
                task["id"] = f"t{i+1}"
            result.append(task)
        return result

    def should_answer_directly(self, user_input: str) -> bool:
        """判断是否直接回答(闲聊) - 兼容旧 API"""
        text = user_input.lower().strip()
        return any(intent in text for intent in _CHITCHAT_KEYWORDS)

    def should_learn_skill(self, user_input: str) -> bool:
        return any(kw in user_input for kw in _TEACHING_KEYWORDS)

    def _detect_chitchat(self, user_input: str) -> tuple[str, str]:
        """快速闲聊检测,返回 (类型, 详情) 或 (None, "")。
        
        检测策略:
        1. 极短输入(<=8字符) → 闲聊
        2. 纯关键词匹配 → 对应闲聊类型
        """
        text = user_input.strip()
        
        # 策略1: 极短输入直接判定为闲聊
        if len(text) <= _SHORT_CHITCHAT_THRESHOLD:
            # 但排除可能是命令的短输入
            if self._looks_like_command(text):
                return None, ""
            return IntentType.CHITCHAT, f"短输入: {text}"
        
        # 策略2: 关键词匹配
        lower = text.lower()
        
        # 问候
        if any(kw in lower for kw in _CHITCHAT_GREETINGS):
            return IntentType.CHITCHAT, "问候"
        
        # 感谢
        if any(kw in lower for kw in _CHITCHAT_THANKS):
            return IntentType.CHITCHAT, "感谢"
        
        # 道别
        if any(kw in lower for kw in _CHITCHAT_FAREWELL):
            return IntentType.CHITCHAT, "道别"
        
        # 肯定回复
        if any(kw in lower for kw in _CHITCHAT_AFFIRMATIVE):
            # "好的" 后面跟具体内容可能不是闲聊
            if len(text) > 15 and any(kw in text for kw in ["，", "。", "?"]):
                return None, ""
            return IntentType.CHITCHAT, "肯定回复"
        
        # 否定回复
        if any(kw in text for kw in _CHITCHAT_NEGATIVE):
            return IntentType.CHITCHAT, "否定回复"
        
        # 询问 Agent 身份
        if any(kw in text for kw in _CHITCHAT_RANDOM):
            return IntentType.CHITCHAT, "询问身份/状态"
        
        return None, ""

    def _looks_like_command(self, text: str) -> bool:
        """判断短文本是否像命令(避免误判)"""
        # 以特定符号开头可能是命令
        cmd_prefixes = ["/", "-", "--", "!", "?", "#"]
        return any(text.startswith(p) for p in cmd_prefixes)

    def _is_simple_intent(self, intent_detail: str) -> bool:
        """判断意图详情是否足够简单(无需工具)"""
        simple_keywords = [
            "问候", "打招呼", "打招呼", "hi", "hello", "你好",
            "感谢", "谢谢", "道别", "再见", "确认", "好的",
        ]
        return any(kw in intent_detail.lower() for kw in simple_keywords)

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

    def _enrich_with_hints(self, user_input: str) -> str:
        """读取历史教训 hints,拼到 user_input 前面"""
        hints = self.memory_store.get_skill_hints(user_input)
        if not hints:
            return user_input
        hints_text = "\n".join(f"- {h}" for h in hints)
        return f"{user_input}\n\n[历史经验提示]:\n{hints_text}"
