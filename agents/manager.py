"""Manager Agent - 流转中枢:意图识别、技能选择、任务规划

使用分层意图识别架构:
  Layer 0: 快速规则过滤 (无 LLM 调用)
  Layer 1: 关键词扩展分析 (无 LLM 调用)
  Layer 2: LLM 深度理解 (最后手段)
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

from core.agent_base import BaseAgent
from core.context import Context
from core.memory import get_memory_store
from core.semantic_memory import get_semantic_memory, SemanticMemoryStore
from core.intent_detector import (
    IntentCategory, IntentResult, IntentDetector, 
    get_intent_detector, is_chitchat, is_task
)
from skills.manager import Skill, get_skill_store

# Feature Flag
def _semantic_memory_enabled() -> bool:
    try:
        from infra.config import config
        return bool(config.semantic_memory_enabled)
    except Exception:
        return False


@dataclass
class PlanResult:
    """规划结果"""
    intent: str                    # 意图类型: chitchat / skill / teach / retry / unknown
    intent_detail: str = ""        # 意图详情描述
    selected_skill: Optional["Skill"] = None
    is_retry: bool = False        # 是否是重试意图
    tool_tasks: List[Dict] = field(default_factory=list)
    need_llm: bool = False        # 是否需要 LLM 处理


# ===== 意图类型常量 =====
class IntentType:
    CHITCHAT = "chitchat"     # 闲聊:问候、感谢、道别等
    SKILL = "skill"           # 技能需求:需要执行工具完成任务
    TEACH = "teach"           # 教导:用户教 Agent 新技能
    RETRY = "retry"          # 重试:重新执行上次的任务
    MANAGER = "manager"       # 技能管理:列出/查看/版本/回滚(M1-09)
    UNKNOWN = "unknown"        # 未知:需要进一步分析


# ===== 教导意图关键词(补充 IntentDetector) =====
_TEACHING_KEYWORDS = [
    "以后", "记住", "下次", "按这个", "按我的", "原则", "方法论",
    "教你", "应该", "步骤是", "正确做法", "这么做",
    "遇到", "情况", "要", "才能",
]

# ===== 技能管理意图关键词(M1-09) =====
_MANAGEMENT_KEYWORDS = [
    "列出", "查看", "有哪些技能", "技能列表", "版本", "回滚",
    "禁用", "启用", "切换版本", "技能详情", "skill list", "skill version",
    "skills", "show me skills", "what skills",
]


def _is_management_intent(text: str) -> bool:
    t = text.strip().lower()
    return any(kw in t for kw in _MANAGEMENT_KEYWORDS)


class ManagerAgent(BaseAgent):
    """Manager Agent - 流转中枢

    职责:
    1. 意图识别 (使用分层架构)
    2. 技能选择(方法论)
    3. 任务规划(工具调用)
    """

    name = "Manager"

    PLAN_SCHEMA = {
        "type": "object",
        "properties": {
            "intent":         {"type": "string"},
            "selected_skill": {"type": "string"},
            "is_retry":       {"type": "boolean"},
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

1. 识别用户意图(包括重试、上下文关联等)
2. 选择合适的技能(方法论)来完成任务
3. 规划需要的工具来获取数据

重要规则:
- 工具只能从以下列表选择:weather_query, web_search
- 如果用户说"重新回答"、"再说一遍"、"上次"、"之前"等,说明要重试上一次的执行,tool_tasks 要与上次保持一致
- 技能是完成任务的方法论,工具是获取数据的手段

已有技能列表:
{skills_list}

严格按以下 JSON 格式输出:
{{
  "intent": "用户意图简短描述",
  "selected_skill": "选择的技能名称(没有则填空字符串)",
  "is_retry": true/false,  // 用户是否要求重试上一次的执行
  "tool_tasks": [
    {{"type": "weather_query", "params": {{"city": "城市名", "date": "日期"}}}},
    {{"type": "web_search", "params": {{"query": "搜索关键词"}}}}
  ]
}}"""

    def __init__(self):
        super().__init__()
        self.skill_store = get_skill_store()
        self.memory_store = get_memory_store()
        self.intent_detector = get_intent_detector()
        # 语义记忆(延迟初始化)
        self._semantic_memory: Optional[SemanticMemoryStore] = None

    @property
    def semantic_memory(self) -> Optional[SemanticMemoryStore]:
        """延迟获取语义记忆实例"""
        if self._semantic_memory is None and _semantic_memory_enabled():
            try:
                self._semantic_memory = get_semantic_memory()
            except Exception:
                pass
        return self._semantic_memory

    async def plan(self, user_input: str, context: Optional[Context] = None) -> PlanResult:
        """分析用户输入,产出 PlanResult。

        意图识别流程(分层架构):
        1. Layer 0/1: 快速规则匹配(无 LLM)
           - 闲聊类(GREETING/FAREWELL/THANKS 等) → 直接返回
           - 教导类(TEACH) → 直接返回
           - 重试类(RETRY) → 标记 is_retry,继续执行
        2. Layer 2: 需要 LLM 深度理解
           - 任务类(WEATHER/SEARCH 等) → 调用 LLM 规划工具
           - 未知类 → 调用 LLM 分析

        Args:
            user_input: 本轮用户输入
            context:    完整对话上下文(可选)
        """
        self.logger.info("Manager", "开始意图识别和任务规划")

        # ===== Layer 0/1: 快速规则匹配(无 LLM) =====
        intent_result = self.intent_detector.detect(user_input)
        
        # 闲聊类 → 直接返回
        if is_chitchat(intent_result.category):
            self.logger.info("Manager", f"闲聊检测: {intent_result.category.value}")
            return PlanResult(
                intent=IntentType.CHITCHAT,
                intent_detail=intent_result.detail,
                selected_skill=None,
                tool_tasks=[],
            )
        
        # 教导类 → 直接返回
        if intent_result.category == IntentCategory.TEACH:
            self.logger.info("Manager", "教导检测: 检测到教导意图")
            return PlanResult(
                intent=IntentType.TEACH,
                intent_detail=intent_result.detail,
                selected_skill=None,
                tool_tasks=[],
            )
        
        # 教导关键词补充检测
        if self.should_learn_skill(user_input):
            self.logger.info("Manager", "教导检测(补充): 检测到教导意图")
            return PlanResult(
                intent=IntentType.TEACH,
                intent_detail="用户教导新技能",
                selected_skill=None,
                tool_tasks=[],
            )

        # 技能管理意图检测(M1-09)
        if _is_management_intent(user_input):
            self.logger.info("Manager", "技能管理意图检测")
            return PlanResult(
                intent=IntentType.MANAGER,
                intent_detail="技能管理操作",
                selected_skill=None,
                tool_tasks=[],
                need_llm=False,
            )

        # 重试类 → 标记并继续
        if intent_result.category == IntentCategory.RETRY:
            self.logger.info("Manager", "重试检测: 检测到重试意图")
            # 重试意图需要上下文来确定上次的任务
            # 如果没有上下文,返回 unknown
            if not context or len(context) == 0:
                return PlanResult(
                    intent=IntentType.UNKNOWN,
                    intent_detail="重试需要上下文,但没有历史记录",
                    selected_skill=None,
                    is_retry=True,
                    tool_tasks=[],
                    need_llm=False,
                )
            # 有上下文,标记 is_retry 继续执行
            plan = PlanResult(
                intent=IntentType.RETRY,
                intent_detail=intent_result.detail,
                selected_skill=None,
                is_retry=True,
                need_llm=True,  # 需要 LLM 获取上次的 tool_tasks
            )
        else:
            plan = PlanResult(
                intent=IntentType.UNKNOWN,
                intent_detail="需要 LLM 深度理解",
                need_llm=True,
            )
        
        # ===== Layer 2: LLM 深度理解 =====
        if plan.need_llm:
            llm_result = await self._llm_plan(user_input, context)
            if llm_result:
                return llm_result
        
        return plan

    async def _llm_plan(self, user_input: str, context: Optional[Context]) -> Optional[PlanResult]:
        """调用 LLM 进行深度规划"""
        skills = self.skill_store.list_all()

        # 读取历史教训 hints,拼到 user_input 前面
        enriched_input = await self._enrich_with_hints(user_input)

        try:
            llm_plan = await self.think_json(
                self._build_user_prompt(enriched_input, context),
                self.PLAN_SCHEMA,
            )
        except Exception as e:
            self.logger.error("Manager", f"LLM 规划失败: {e}")
            return PlanResult(
                intent=IntentType.UNKNOWN,
                intent_detail=f"LLM 调用失败: {str(e)[:50]}",
                selected_skill=None,
                tool_tasks=[],
            )

        selected_skill = None
        skill_name = llm_plan.get("selected_skill") or ""
        if skill_name:
            for s in skills:
                if s.name == skill_name:
                    selected_skill = s
                    break

        intent_detail = llm_plan.get("intent", "")
        self.logger.info("Manager", f"LLM 识别意图: {intent_detail}")
        if selected_skill:
            self.logger.info("Manager", f"选择技能: {selected_skill.name}")

        tool_tasks = self._auto_id_tool_tasks(llm_plan.get("tool_tasks", []) or [])
        self.logger.info("Manager", f"规划工具: {len(tool_tasks)} 个")

        # 获取 LLM 判断的重试意图
        is_retry = llm_plan.get("is_retry", False)
        if is_retry:
            self.logger.info("Manager", "LLM 判断: 重试意图")

        # 根据结果判断意图类型
        if is_retry:
            return PlanResult(
                intent=IntentType.RETRY,
                intent_detail=f"{intent_detail} (重试)",
                selected_skill=None,
                is_retry=True,
                tool_tasks=tool_tasks,
            )

        if tool_tasks:
            return PlanResult(
                intent=IntentType.SKILL,
                intent_detail=intent_detail,
                selected_skill=selected_skill,
                tool_tasks=tool_tasks,
            )

        # M1-02: 即使没有 tool_tasks,只要选中了 selected_skill
        # (纯方法论技能)就允许以 SKILL 计划返回。
        if selected_skill is not None:
            return PlanResult(
                intent=IntentType.SKILL,
                intent_detail=intent_detail,
                selected_skill=selected_skill,
                tool_tasks=[],
            )

        # 没有工具,也没有技能,但意图明确 → 闲聊
        if self._is_simple_intent(intent_detail):
            return PlanResult(
                intent=IntentType.CHITCHAT,
                intent_detail=intent_detail,
                selected_skill=None,
                tool_tasks=[],
            )

        return PlanResult(
            intent=IntentType.UNKNOWN,
            intent_detail=intent_detail or "无法理解用户意图",
            selected_skill=None,
            tool_tasks=[],
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

    def should_learn_skill(self, user_input: str) -> bool:
        return any(kw in user_input for kw in _TEACHING_KEYWORDS)

    def should_answer_directly(self, user_input: str) -> bool:
        """判断是否直接回答(闲聊) - 兼容旧 API"""
        from core.intent_detector import get_intent_detector, is_chitchat
        detector = get_intent_detector()
        result = detector.detect(user_input)
        return is_chitchat(result.category)

    def _is_simple_intent(self, intent_detail: str) -> bool:
        """判断意图详情是否足够简单(无需工具)"""
        simple_keywords = [
            "问候", "打招呼", "hi", "hello", "你好",
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

    async def _enrich_with_hints(self, user_input: str) -> str:
        """读取历史教训 hints,拼到 user_input 前面

        优先使用语义记忆,回退到普通记忆
        """
        hints: List[str] = []

        # 尝试语义记忆
        if self.semantic_memory:
            try:
                results = await self.semantic_memory.search_context(user_input, limit=3)
                hints.extend(results)
            except Exception:
                pass

        # 回退到普通记忆
        if not hints:
            hints = self.memory_store.get_skill_hints(user_input)

        if not hints:
            return user_input

        hints_text = "\n".join(f"- {h}" for h in hints)
        return f"{user_input}\n\n[历史经验提示]:\n{hints_text}"
