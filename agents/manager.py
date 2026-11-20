"""Manager Agent - 流转中枢:意图识别、技能选择、任务规划

分层意图识别(四层,见 `docs/12-意图分层识别方案.md`):

    L0 守卫层   纯规则,0 次 LLM。命中强结构信号就定案。
    L1 判别层   一次 LLM 调用,闭集分类(6 标签),带置信度。
    L2 规划层   LLM 规划工具/技能。**不再输出意图** —— 意图由 L1 给。
    L3 澄清层   低置信 / 缺关键信息时提出澄清问题(尚未落地)

关键点:意图与规划**解耦**。意图错了不再污染规划,规划错了也不再回写意图,
两者可以分别评估、分别优化。
"""
from typing import Any, Dict, List, Optional, Tuple
from contextvars import ContextVar

from core.agent_base import BaseAgent
from agents.intents import classify_l1, plan_from_label, recent_turns
from agents.manager_types import IntentType, PlanResult
from agents.prompts import (
    build_plan_schema,
    build_plan_system_prompt,
    health_block,
    workspace_block,
)
from core.context import Context
from core.memory import get_memory_store
from core.semantic_memory import get_semantic_memory, SemanticMemoryStore
from core.intent_labels import IntentLabel
from core.llm_judge import IntentDecision, get_l1_classifier
from core.intent_detector import (
    get_intent_detector,
)
from skills.manager import Skill, get_skill_store
from core.exception_policy import downgrade

#: 当前这一轮正在规划的输入（用于技能召回）。
#:
#: 为什么用 contextvar 而不是实例属性：`system_prompt()` 是基类 `think()`
#: 回调的，拿不到入参；而 `ManagerAgent` 是**每个 session 一个实例**，
#: 同一 session 并发两个请求时实例属性会串。
_current_planning_input: ContextVar[str] = ContextVar(
    "dsh_manager_planning_input", default=""
)

#: 本次规划的**工具白名单**（None = 不限制）。
#:
#: 子智能体按角色裁剪工具后传进来 —— planner 的 schema 枚举、
#: 工具签名清单、未知工具过滤都要跟着变。
#:
#: 用 contextvar 而不是实例属性：ManagerAgent 是每 session 一个实例，
#: 同 session 并发两个请求时实例属性会串。
_plan_allowed_tools: ContextVar[Optional[List[str]]] = ContextVar(
    "dsh_manager_allowed_tools", default=None
)
_plan_allowed_skills: ContextVar[Optional[List[str]]] = ContextVar(
    "dsh_manager_allowed_skills", default=None
)
#: 本次规划使用的**上下文块**（来自 ContextBundle）。
#: 给了就用它替代"主对话全量历史"。
_plan_context_blocks: ContextVar[Optional[List[Dict[str, Any]]]] = ContextVar(
    "dsh_manager_context_blocks", default=None
)

# Feature Flag
def _semantic_memory_enabled() -> bool:
    try:
        from infra.config import config
        return bool(config.semantic_memory_enabled)
    except Exception:
        return False


def _l1_enabled() -> bool:
    """L1 判别层是否启用(默认启用)。"""
    try:
        from infra.config import config
        return bool(getattr(config, "intent_l1_enabled", True))
    except Exception:
        return True


def _l1_threshold() -> float:
    """L1 定案所需的最低置信度(默认 0.75)。"""
    try:
        from infra.config import config
        return float(getattr(config, "intent_l1_threshold", 0.75))
    except Exception:
        return 0.75




# ===== 意图标签(6 闭集,与 core/intent_labels.py::IntentLabel 一一对应) =====


# ===== 意图判定规则 =====
# **这里不再有规则。**
# 意图的唯一裁决者是 L1(`core/llm_judge.py`,一次 LLM 调用,6 标签闭集)。
# 历史:`_TEACH_PATTERNS` / `_MANAGEMENT_PATTERNS` 曾在这里做正则判定,
# 和 L0 共用同一份;真机基线证明它把准确率从 97.7% 拖到 94.7%,已删除。


def decision_is_clarify(decision: Optional[IntentDecision]) -> bool:
    """L1 是否判为"意图不明"。

    只有 L1 明确说了 clarify,规划层又拿不出任何动作时,才值得追问一句 ——
    别的标签(比如低置信的 chitchat)直接答更合适,不该把用户问懵。
    """
    return decision is not None and decision.label is IntentLabel.CLARIFY


# ===== 指代型提问标记 =====
# 出现这些词时,才去检索本会话历史 —— 避免每次都注入噪声
_REFERENTIAL_MARKERS = [
    "上次", "上次那个", "之前", "先前", "刚才", "刚刚", "刚说",
    "那个", "这个", "前面", "方才", "以往", "之前提到",
]


def _has_referential_marker(text: str) -> bool:
    return any(marker in text for marker in _REFERENTIAL_MARKERS)


class ManagerAgent(BaseAgent):
    """Manager Agent - 流转中枢

    职责:
    1. 意图识别 (使用分层架构)
    2. 技能选择(方法论)
    3. 任务规划(工具调用)
    """

    name = "Manager"

    # ToolHub 不可用时的兜底工具列表
    FALLBACK_TOOLS = ["weather_query", "web_search"]

    # --- 技能注入的两阶段召回（docs/18 F0-3）---
    #: 目录里最多列多少个技能（名字 + 一句话能力）
    SKILL_CATALOG_LIMIT = 40
    #: 召回多少个技能的完整方法（详情）
    SKILL_DETAIL_TOP_K = 5

    def _available_tool_names(self) -> List[str]:
        """当前可用工具名(来自 ToolHub,含 MCP / 用户注册的工具)。

        有白名单时**只返回白名单内的** —— 子智能体的 planner 不该看到
        它没有权限的工具（docs/18 §4.2：授权靠裁剪，不靠提示词）。
        """
        try:
            from tools.hub import get_tool_hub

            names = list(get_tool_hub().names() or [])
        except Exception:
            names = []
        allow = _plan_allowed_tools.get()
        if allow is not None:
            allowed = set(allow)
            names = [n for n in names if n in allowed]
        return names or (
            list(self.FALLBACK_TOOLS)
            if allow is None
            else []
        )

    def _tool_signatures(self) -> str:
        """工具签名清单(名字 + 参数 + 一句说明)。

        **为什么不能只给名字**:planner 只看到 `web_fetch` 三个字,
        它不知道参数叫 `url`,于是会把"从结果里选一个链接"这种文字
        塞进参数值里 —— 执行期必然失败。

        实机踩过:造工具时它不知道 `create_tool` 有哪些字段,
        只能凭想象拼,连着两次校验失败。

        格式紧凑(`?` 表示可选),工具多时不至于挤爆上下文。
        """
        try:
            from tools.hub import get_tool_hub

            schemas = get_tool_hub().schemas()
        except Exception:
            return ", ".join(self._available_tool_names())

        allow = _plan_allowed_tools.get()
        if allow is not None:
            allowed = set(allow)
            schemas = [s for s in schemas if s.name in allowed]

        lines = []
        for sch in schemas:
            try:
                parts = []
                for pm in sch.params:
                    mark = "" if getattr(pm, "required", True) else "?"
                    ptype = (getattr(pm, "type", "") or "string").replace("string", "str")
                    parts.append(f"{pm.name}{mark}: {ptype}")
                sig = ", ".join(parts) if parts else "无参数"
                desc = (sch.description or "").strip().splitlines()[0][:80]
                lines.append(f"- {sch.name}({sig}) — {desc}")
            except Exception:
                continue
        return "\n".join(lines) if lines else ", ".join(self._available_tool_names())

    @property
    def plan_schema(self) -> Dict:
        """规划 schema —— 模板在 `agents/prompts.py`，这里只提供可用工具名。"""
        return build_plan_schema(self._available_tool_names())

    def system_prompt(self) -> str:
        """规划提示词 —— 模板在 `agents/prompts.py`，这里只负责取数据。"""
        skills = self.skill_store.list_all()
        allow_skills = _plan_allowed_skills.get()
        if allow_skills is not None:
            allowed = set(allow_skills)
            skills = [
                s for s in skills
                if s.name in allowed or f"{s.name}@{s.version}" in allowed
            ]
        return build_plan_system_prompt(
            skills_list=self._format_skills(
                skills, query=_current_planning_input.get() or ""
            ),
            workspace_block=workspace_block(),
            health_block=health_block(),
            tools=self._tool_signatures(),
        )

    def __init__(self):
        super().__init__()
        self.skill_store = get_skill_store()
        self.memory_store = get_memory_store()
        self.intent_detector = get_intent_detector()
        # L1 闭集分类器
        self.l1_classifier = get_l1_classifier()
        self._l1_enabled = _l1_enabled()
        self._l1_threshold = _l1_threshold()
        # 语义记忆(延迟初始化)
        self._semantic_memory: Optional[SemanticMemoryStore] = None

    @property
    def semantic_memory(self) -> Optional[SemanticMemoryStore]:
        """延迟获取语义记忆实例"""
        if self._semantic_memory is None and _semantic_memory_enabled():
            try:
                self._semantic_memory = get_semantic_memory()
            except Exception as exc:
                downgrade(exc, where='agents/manager.py')
        return self._semantic_memory

    async def plan(
        self,
        user_input: str,
        context: Optional[Context] = None,
        session_id: str = "",
        profile_block: str = "",
        prior_findings: str = "",
        allowed_tools: Optional[List[str]] = None,
        allowed_skills: Optional[List[str]] = None,
        context_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> PlanResult:
        """分析用户输入,产出 PlanResult(三层编排)。

        `prior_findings` 是**目标循环**里前几轮已经拿到的结果（空 = 第一轮）。

        - **结构守卫**:只判断"不需理解语义就能确定"的事(空输入 → 澄清)。
          **没有任何"猜用户怎么说"的规则** —— 那种规则会静默地判错,
          而且会抢在 L1 前面把话拿走(真机基线:94.7% vs 97.7%)。
        - **L1 判别层**:**意图的唯一裁决者**。一次 LLM 调用做闭集分类
          (6 标签),带置信度。置信度 ≥ 阈值(默认 0.75)才可定案。
        - **L2 规划层**:规划工具/技能。**不再输出意图** —— 意图由 L1 给。

        低置信时**不放弃**,而是带着 L1 的判断升级到 L2 兜底
        (宁可多调一次工具,也不要漏答)。

        Args:
            user_input: 本轮用户输入
            context:    完整对话上下文(可选)
        """
        self.logger.info("Manager", "开始意图识别和任务规划")

        # ===== L0 结构守卫(0 次 LLM) =====
        decided = self._guard_plan(user_input, context)
        if decided is not None:
            return decided

        # ===== L1 判别层(一次 LLM 调用,闭集分类) =====
        decision, decided = await self._l1_plan(user_input, context)
        if decided is not None:
            return decided

        # ===== L2 规划层 =====
        plan = await self._llm_plan(
            user_input,
            context,
            session_id=session_id,
            intent_hint=decision,
            profile_block=profile_block,
            prior_findings=prior_findings,
            allowed_tools=allowed_tools,
            allowed_skills=allowed_skills,
            context_blocks=context_blocks,
        )
        if plan is not None:
            return plan

        # 规划也没产出 → 兜底
        return self._fallback_plan(decision)

    def _guard_plan(
        self, user_input: str, context: Optional[Context]
    ) -> Optional[PlanResult]:
        """L0 结构守卫：只判断"不需理解语义就能确定"的事（0 次 LLM）。

        现在只有一条规则：空输入 → 澄清。

        **刻意没有任何"猜用户怎么说"的规则** —— 那种规则会静默地判错，
        而且会抢在 L1 前面把话拿走（真机基线：加规则的 94.7% vs 只留空输入
        守卫的 97.7%）。想加规则进来，先拿基线说话。

        Returns:
            定案则返回 `PlanResult`，未定案返回 `None`（继续走 L1）。
        """
        intent_result = self.intent_detector.detect(user_input)
        label = intent_result.label
        if label is None:
            return None

        self.logger.info(
            "Manager", f"结构守卫定案: {label.value} ({intent_result.detail})"
        )
        return self._plan_from_label(
            label,
            detail=intent_result.detail,
            context=context,
            layer="guard",
            confidence=intent_result.confidence,
            slots=intent_result.entities,
        )

    async def _l1_plan(
        self, user_input: str, context: Optional[Context]
    ) -> Tuple[Optional[IntentDecision], Optional[PlanResult]]:
        """L1 判别层：**意图的唯一裁决者**。

        一次 LLM 调用做闭集分类（6 标签）。三种出口：

        - 置信度 < 阈值 → **不定案**，把判断一起升级到 L2 兜底
          （宁可多调一次工具，也不要漏答）；
        - 置信度够 + 非 task → 直接定案；
        - 置信度够 + task → 不定案，交给 L2 规划"怎么做"。

        Returns:
            `(decision, decided)` —— `decided` 非空表示已定案；
            `decision` 无论如何都返回，供 L2 当强约束注入提示词。
        """
        decision = await self._l1_classify(user_input, context)
        if decision is None:
            return None, None

        if decision.confidence < self._l1_threshold:
            # 低置信:不放弃,带 L1 结果升级 L2 兜底(D4)
            self.logger.info(
                "Manager",
                f"L1 置信不足({decision.confidence:.2f},"
                f"判为 {decision.label.value}),升级 L2 兜底",
            )
            return decision, None

        if decision.label is IntentLabel.TASK:
            self.logger.info(
                "Manager",
                f"L1 判为 task (置信度 {decision.confidence}),进入 L2 规划",
            )
            return decision, None

        self.logger.info(
            "Manager",
            f"L1 定案: {decision.label.value} (置信度 {decision.confidence})",
        )
        return decision, self._plan_from_label(
            decision.label,
            detail=decision.reason or f"L1 判别为 {decision.label.value}",
            context=context,
            layer="L1",
            confidence=decision.confidence,
            slots=decision.slots,
        )

    @staticmethod
    def _fallback_plan(decision: Optional[IntentDecision]) -> PlanResult:
        """L2 也没产出计划时的兜底：当成"直接回答"，不要卡住用户。"""
        return PlanResult(
            intent=IntentType.TASK,
            intent_detail="无法规划出具体动作,直接回答",
            selected_skill=None,
            tool_tasks=[],
            intent_layer="L2",
            intent_confidence=decision.confidence if decision else 0.0,
            intent_uncertain=decision is not None,
        )

    def _plan_from_label(
        self,
        label: IntentLabel,
        *,
        detail: str = "",
        context: Optional[Context] = None,
        layer: str = "L1",
        confidence: float = 0.0,
        slots: Optional[Dict[str, Any]] = None,
    ) -> Optional[PlanResult]:
        """标签 → PlanResult（实现在 `agents/intents.py`，独立可测）。"""
        return plan_from_label(
            label, detail=detail, context=context,
            layer=layer, confidence=confidence, slots=slots,
        )

    async def _l1_classify(
        self, user_input: str, context: Optional[Context]
    ) -> Optional[IntentDecision]:
        """L1 闭集分类（实现在 `agents/intents.py`，独立可测）。"""
        return await classify_l1(
            self.l1_classifier,
            user_input,
            context,
            enabled=self._l1_enabled,
            logger=self.logger,
        )

    @staticmethod
    def _recent_turns(context: Optional[Context], limit: int = 2) -> List[Dict[str, str]]:
        """取最近几轮对话（实现在 `agents/intents.py`）。"""
        return recent_turns(context, limit=limit)



    async def _llm_plan(
        self,
        user_input: str,
        context: Optional[Context],
        session_id: str = "",
        intent_hint: Optional[IntentDecision] = None,
        profile_block: str = "",
        prior_findings: str = "",
        allowed_tools: Optional[List[str]] = None,
        allowed_skills: Optional[List[str]] = None,
        context_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[PlanResult]:
        """L2 规划层（受限版入口）。

        受限参数通过 contextvar 传下去，作用域仅限本次规划：
        - `allowed_tools`  —— planner 只能看到这些工具（schema 枚举也变）
        - `allowed_skills` —— 只注入这些技能
        - `context_blocks` —— 用装配好的上下文块替代主对话全量历史
        """
        tokens = []
        try:
            tokens.append(_plan_allowed_tools.set(allowed_tools))
            tokens.append(_plan_allowed_skills.set(allowed_skills))
            tokens.append(_plan_context_blocks.set(context_blocks))
            return await self._llm_plan_inner(
                user_input,
                context,
                session_id=session_id,
                intent_hint=intent_hint,
                profile_block=profile_block,
                prior_findings=prior_findings,
            )
        finally:
            for token in reversed(tokens):
                try:
                    token.var.reset(token)
                except Exception as exc:
                    downgrade(exc, where='agents/manager.py')

    async def _llm_plan_inner(
        self,
        user_input: str,
        context: Optional[Context],
        session_id: str = "",
        intent_hint: Optional[IntentDecision] = None,
        profile_block: str = "",
        prior_findings: str = "",
    ) -> Optional[PlanResult]:
        """L2 规划层:规划要调哪些工具、用哪个技能。

        **不再输出意图** —— 意图由 L0/L1 判定,这里只负责"怎么做"。
        `intent_hint` 是 L1 的判别结果,作为强约束注入提示词。
        `profile_block` 是调用方(Agent)本轮已取好的画像,空则自己取。

        两步：先问模型（`_ask_planner`），再把模型的话翻成 `PlanResult`
        （`_build_plan_result`）。中间不掺业务判断，各自可单独测。
        """
        enriched_input, llm_plan, error = await self._ask_planner(
            user_input,
            context,
            session_id=session_id,
            intent_hint=intent_hint,
            profile_block=profile_block,
            prior_findings=prior_findings,
        )
        if llm_plan is None:
            return PlanResult(
                intent=IntentType.TASK,
                intent_detail=f"LLM 调用失败: {(error or '')[:50]}",
                selected_skill=None,
                tool_tasks=[],
                intent_layer="L2",
                intent_uncertain=True,
            )
        return self._build_plan_result(
            llm_plan, enriched_input=enriched_input, intent_hint=intent_hint
        )

    async def _ask_planner(
        self,
        user_input: str,
        context: Optional[Context],
        *,
        session_id: str,
        intent_hint: Optional[IntentDecision],
        profile_block: str,
        prior_findings: str,
    ) -> Tuple[str, Optional[Dict[str, Any]], str]:
        """拼提示词、调规划模型。

        Returns:
            `(enriched_input, llm_plan, error)` —— `llm_plan` 为 None 表示调用失败，
            `error` 带原始信息（会进 PlanResult.intent_detail，便于排查）。
            返回值里带上 `enriched_input` 是因为后面判"召回准不准"要用同一份输入。
        """
        # 读取历史教训 hints,拼到 user_input 前面
        enriched_input = await self._enrich_with_hints(
            user_input, session_id=session_id
        )

        # 用户画像(长期记忆):**常驻注入**,不依赖 query 命中
        if not profile_block:
            profile_block = self.profile_block()

        try:
            # 让 system_prompt() 知道"这一轮在规划什么" —— 技能召回要用。
            # 用 contextvar 而不是实例属性：ManagerAgent 是每 session 一个实例，
            # 同 session 并发两个请求时实例属性会串。
            _plan_token = _current_planning_input.set(enriched_input)
            try:
                llm_plan = await self.think_json(
                    self._build_user_prompt(
                        enriched_input, context, intent_hint, profile_block,
                        prior_findings=prior_findings,
                    ),
                    self.plan_schema,
                )
            finally:
                _current_planning_input.reset(_plan_token)
        except Exception as e:
            self.logger.error("Manager", f"LLM 规划失败: {e}")
            return enriched_input, None, str(e)
        return enriched_input, llm_plan, ""

    def _resolve_skill(self, skill_name: str, enriched_input: str) -> Optional[Skill]:
        """把模型给的技能名解析成技能对象。解析不了返回 None（不抛）。"""
        if not skill_name:
            return None
        selected_skill = None
        for s in self.skill_store.list_all():
            if s.name == skill_name:
                selected_skill = s
                break
        if selected_skill is None:
            self.logger.warning("Manager", f"选了不存在的技能: {skill_name}")
            return None

        # 选了但**不在召回详情里** → 它只有名字,没有方法细节。
        # 记一笔,用来量"召回准不准"(docs/18 F0-3 的观测点)。
        try:
            recalled = {
                s.name
                for s in self.skill_store.match(enriched_input, self.SKILL_DETAIL_TOP_K)
            }
        except Exception:
            recalled = set()
        if skill_name not in recalled:
            self.logger.info(
                "Manager",
                f"选中的技能未进入召回 top-{self.SKILL_DETAIL_TOP_K}: {skill_name}"
                f"(只有目录信息,没有方法细节)",
            )
        return selected_skill

    def _build_plan_result(
        self,
        llm_plan: Dict[str, Any],
        *,
        enriched_input: str,
        intent_hint: Optional[IntentDecision],
    ) -> PlanResult:
        """把规划模型的输出翻成 `PlanResult`。

        四条出口（顺序即优先级）：重试 → 有工具任务 → 只有技能 → 直答/澄清。
        """
        selected_skill = self._resolve_skill(
            llm_plan.get("selected_skill") or "", enriched_input
        )
        plan_detail = llm_plan.get("plan_detail", "") or ""
        if selected_skill:
            self.logger.info("Manager", f"选择技能: {selected_skill.name}")

        goal_done = llm_plan.get("goal_done", True)
        if not isinstance(goal_done, bool):
            goal_done = True
        remaining_note = str(llm_plan.get("remaining_note") or "")
        if goal_done:
            remaining_note = ""

        tool_tasks = self._auto_id_tool_tasks(llm_plan.get("tool_tasks", []) or [])
        tool_tasks, dropped = self._filter_unknown_tools(tool_tasks)
        if dropped:
            self.logger.warning(
                "Manager",
                f"丢弃未知工具的任务: {dropped}(当前可用: {self._available_tool_names()})",
            )
        self.logger.info("Manager", f"规划工具: {len(tool_tasks)} 个")

        # LLM 判断的重试意图
        is_retry = llm_plan.get("is_retry", False)
        if is_retry:
            self.logger.info("Manager", "L2 判断: 重试意图")

        # 意图层:有 L1 判别结果 → 意图来自 L1;没有 → L1 失败,退到 L2 兜底
        layer = "L1" if intent_hint is not None else "L2"
        confidence = intent_hint.confidence if intent_hint else 0.0
        uncertain = intent_hint is None or intent_hint.confidence < self._l1_threshold
        slots = dict(intent_hint.slots) if intent_hint else {}

        # 重试优先
        if is_retry:
            return PlanResult(
                intent=IntentType.RETRY,
                intent_detail=plan_detail or "重试上一轮",
                selected_skill=None,
                is_retry=True,
                tool_tasks=tool_tasks,
                intent_layer=layer,
                intent_confidence=confidence,
                intent_uncertain=uncertain,
                intent_slots=slots,
            )

        if tool_tasks:
            return PlanResult(
                intent=IntentType.TASK,
                intent_detail=plan_detail or f"规划了 {len(tool_tasks)} 个工具任务",
                selected_skill=selected_skill,
                tool_tasks=tool_tasks,
                intent_layer=layer,
                intent_confidence=confidence,
                intent_uncertain=uncertain,
                intent_slots=slots,
                goal_done=goal_done,
                remaining_note=remaining_note,
            )

        # M1-02: 即使没有 tool_tasks,只要选中了 selected_skill
        # (纯方法论技能)就允许以 TASK 计划返回。
        if selected_skill is not None:
            return PlanResult(
                intent=IntentType.TASK,
                intent_detail=plan_detail or f"使用技能 {selected_skill.name}",
                selected_skill=selected_skill,
                tool_tasks=[],
                intent_layer=layer,
                intent_confidence=confidence,
                intent_uncertain=uncertain,
                intent_slots=slots,
            )

        # 没有工具也没有技能:意图已由 L0/L1 判定,这里不重新分类,
        # 直接以当前意图返回(由上层走直答流程)。
        #
        # 但有一种情况要进 L3 澄清:**L1 判别时就不确定**(conf < τ₁),
        # 规划层也没找出任何可执行的动作 —— 这时"硬答"就是猜,
        # 不如问一句(docs/12 §2.5)。
        if uncertain and decision_is_clarify(intent_hint):
            return PlanResult(
                intent=IntentType.CLARIFY,
                intent_detail=plan_detail or "意图不明且规划不出动作,需要澄清",
                selected_skill=None,
                tool_tasks=[],
                intent_layer=layer,
                intent_confidence=confidence,
                intent_uncertain=True,
                intent_slots=slots,
            )

        return PlanResult(
            intent=(
                intent_hint.label.value
                if intent_hint and intent_hint.label is not IntentLabel.TASK
                else IntentType.TASK
            ),
            intent_detail=plan_detail or "无需调用工具,直接回答",
            selected_skill=None,
            tool_tasks=[],
            intent_layer=layer,
            intent_confidence=confidence,
            intent_uncertain=uncertain,
            intent_slots=slots,
        )

    def _build_user_prompt(
        self,
        user_input: str,
        context: Optional[Context],
        intent_hint: Optional[IntentDecision] = None,
        profile_block: str = "",
        prior_findings: str = "",
    ) -> str:
        """把上下文历史对话 + L1 判别结果 + 用户画像 + 前几轮结果拼到 user_input。

        不会破坏现有调用——若 context 为空/None,直接返回原 user_input。
        """
        summary_block = context.summary_block() if context is not None else ""
        system_block = context.system_block() if context is not None else ""
        hint_block = self._build_intent_hint_block(intent_hint)
        findings_block = ""
        if (prior_findings or "").strip():
            findings_block = (
                "## 前几轮已经拿到的结果(不要重复查这些,继续往下做)\n"
                + prior_findings.strip()
            )

        # --- 已装配的上下文块（子智能体路径）---
        #
        # 有了它就用它**替代**主对话全量历史 —— 这就是"上下文按需投喂"
        # (docs/18 §4.3)。每个块都带来源与理由。
        blocks = _plan_context_blocks.get()
        if blocks:
            return self._build_from_blocks(
                user_input, blocks, intent_hint=hint_block,
                findings_block=findings_block,
            )

        prefix = "\n\n".join(
            x for x in (
                system_block, hint_block, summary_block, profile_block, findings_block
            ) if x
        )

        if context is None or len(context) == 0:
            return f"{prefix}\n\n{user_input}".strip() if prefix else user_input
        # 剔除"本轮 user_input"之后的历史。
        # 不能靠砍最后一条 —— 子智能体并发跑的时候上下文会被别的分支追加,
        # 最后一条未必是本轮输入,砍错了就把真历史砍了。
        history = context.to_llm_messages(
            max_tokens=2000, exclude_content=user_input
        )
        prior = [
            m for m in history
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        lines = []
        if prefix:
            lines.append(prefix)
            lines.append("")
        if not prior:
            lines.append(f"本次用户输入: {user_input}")
            return "\n".join(lines)
        lines.append("以下是此前的对话历史(用户/助手),可能与本次任务相关:")
        for m in prior[-6:]:
            role = "用户" if m["role"] == "user" else "助手"
            content = (m["content"] or "").strip()
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"- {role}: {content}")
        lines.append("")
        lines.append(f"本次用户输入: {user_input}")
        lines.append("请结合对话历史与已判定的意图,规划需要调用的工具。")
        return "\n".join(lines)

    def _build_from_blocks(
        self,
        user_input: str,
        blocks: List[Dict[str, Any]],
        *,
        intent_hint: str = "",
        findings_block: str = "",
    ) -> str:
        """用**已装配的上下文块**拼提示词（替代主对话全量历史）。

        每块带「来源 + 为什么给你」—— 子智能体能看出哪些是必须遵守的
        （目标与验收）、哪些只是参考（工作区清单）。
        """
        parts: List[str] = []
        for block in blocks or []:
            source = str(block.get("source") or "context")
            why = str(block.get("why") or "")
            content = str(block.get("content") or "").strip()
            if not content:
                continue
            head = f"### [{source}]"
            if why:
                head += f"（{why}）"
            parts.append(f"{head}\n{content}")

        if intent_hint:
            parts.insert(0, intent_hint)
        if findings_block:
            parts.append(findings_block)

        parts.append(f"本次任务：{user_input}")
        parts.append("请只使用上面列出的工具完成任务，不要调用没有授权的工具。")
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _build_intent_hint_block(intent_hint: Optional[IntentDecision]) -> str:
        """把 L1 的判别结果渲染成给 L2 的强约束块。"""
        if intent_hint is None:
            return ""
        lines = [
            "【已判定的意图】(上游判别层给出,请直接据此规划,不要重新判断意图)",
            f"- 标签: {intent_hint.label.value}",
            f"- 置信度: {intent_hint.confidence:.2f}",
        ]
        if intent_hint.reason:
            lines.append(f"- 依据: {intent_hint.reason}")
        if intent_hint.slots:
            slot_text = ", ".join(
                f"{k}={v}" for k, v in intent_hint.slots.items() if v
            )
            if slot_text:
                lines.append(f"- 已提取的信息: {slot_text}")
        if intent_hint.confidence < 0.75:
            lines.append(
                "- 注意: 置信度偏低,如果拿不准,宁可多调一次工具,也不要漏答"
            )
        return "\n".join(lines)

    def _auto_id_tool_tasks(self, tasks: List[Dict]) -> List[Dict]:
        result = []
        for i, t in enumerate(tasks):
            task = dict(t)
            if not task.get("id"):
                task["id"] = f"t{i+1}"
            result.append(task)
        return result

    def _filter_unknown_tools(self, tasks: List[Dict]) -> tuple:
        """过滤掉引用了不存在工具的任务。

        schema 里的 enum 只是给 LLM 的提示,实际执行前仍需校验,
        否则 LLM 幻觉出的工具名会在执行期才报错。
        """
        available = set(self._available_tool_names())
        kept: List[Dict] = []
        dropped: List[str] = []
        for t in tasks:
            tool_type = (t.get("type") or "").strip()
            if tool_type in available:
                kept.append(t)
            else:
                dropped.append(tool_type or "<empty>")
        return kept, dropped

    # 注:`should_learn_skill()` / `should_answer_directly()` 已删除。
    #
    # 它们是**纯规则**的意图判断(?是不是 teach / 是不是闲聊),而
    # 1) 生产流程里从来没人调用 —— `analyze()` 走的是 L0/L1 的标签,不是这两个方法
    # 2) 真机基线证明这套规则把准确率从 97.7% 拖到 94.7%
    #
    # 意图现在只有一处判定:`analyze()` 里的 L1,返回 `IntentLabel`。
    # 需要判断"要不要学技能"请看 `plan.intent == IntentType.TEACH`,
    # 需要判断"要不要直接答"请看 `plan.intent == IntentType.CHITCHAT`。

    # 注:原 `_is_simple_intent()` 已删除。
    # 它对 LLM 自己写的自由文本做关键词二次分类 —— 意图已由 L0/L1 闭集判定,
    # 再用关键词解析一遍等于把两套枚举又混到一起(见 docs/12 §3)。

    def _format_skills(self, skills: List[Skill], query: str = "") -> str:
        """技能注入：**两阶段召回**（目录 + 详情），不再全量倾倒。

        ## 为什么要改

        之前是把**所有** active 技能的 name + capability + method 全塞进
        system prompt（`docs/17` P1-4 点了名：技能库养到 10 个就装不下，
        而且 `SkillRegistry.match()` 这个带 top_k 的召回接口**从来没被
        生产代码调用过** —— 是个死接口）。

        全量注入的三个后果：
        1. 提示词随技能数**线性膨胀**；
        2. planner 在几十个技能里挑，选择成本线性上升；
        3. 与"上下文按需投喂"的目标直接冲突。

        ## 现在怎么做

        - **阶段一 · 目录**：所有技能各一行「名字 + 一句话能力」，有上限
          （`SKILL_CATALOG_LIMIT`）。planner 知道"有什么可用"，但看不到细节。
        - **阶段二 · 详情**：按当前输入召回 top-k（`SKILL_DETAIL_TOP_K`），
          给出完整方法与步骤 —— 这才是真正影响执行的细节。

        召回用现成的 `SkillStore.match()`（会过滤被降级的技能）。
        """
        if not skills:
            return "(暂无技能)"

        # --- 阶段一：目录 ---
        matched: List[Skill] = []
        if query:
            try:
                matched = self.skill_store.match(query, self.SKILL_DETAIL_TOP_K)
            except Exception as exc:
                self.logger.warning("Manager", f"技能召回失败: {exc}")
                matched = []

        matched_names = {s.name for s in matched}
        # 召回命中的排前面，其余按原顺序
        ordered = matched + [s for s in skills if s.name not in matched_names]

        catalog: List[str] = []
        for s in ordered[: self.SKILL_CATALOG_LIMIT]:
            cap = (s.capability or "").strip().replace("\n", " ")[:80]
            catalog.append(f"- {s.name}" + (f": {cap}" if cap else ""))
        if len(ordered) > self.SKILL_CATALOG_LIMIT:
            catalog.append(
                f"- …（另有 {len(ordered) - self.SKILL_CATALOG_LIMIT} 个技能未列出，"
                f"需要的话说出技能名）"
            )

        parts = [
            "可用技能目录（只有名字和一句话能力；下面是**与本次任务相关**的技能详情）：",
            *catalog,
        ]

        # --- 阶段二：详情 ---
        if matched:
            parts.append("")
            parts.append("与本次任务相关的技能详情（按方法执行）：")
            for s in matched:
                parts.append(f"### {s.name}")
                if s.capability:
                    parts.append(f"能力: {s.capability}")
                if s.method:
                    parts.append(f"方法: {s.method}")
                steps = getattr(s, "steps", None) or []
                if steps:
                    for st in steps[:8]:
                        tool = f" → {st.tool}" if getattr(st, "tool", "") else ""
                        desc = (getattr(st, "description", "") or "").strip()
                        parts.append(f"  {getattr(st, 'id', '')}.{desc}{tool}")
                parts.append("")
        else:
            parts.append("")
            parts.append("（本次没有匹配到特别相关的技能，按通用方式规划即可。）")

        return "\n".join(parts)


    async def _enrich_with_hints(
        self, user_input: str, session_id: str = ""
    ) -> str:
        """读取历史教训 hints,拼到 user_input 前面

        优先使用语义记忆,回退到普通记忆。
        另外:指代型提问("上次那个方案…")会检索**本会话历史**,
        让"回来继续问答"真正可用。
        """
        hints: List[str] = []

        # 尝试语义记忆(执行经验类);画像走常驻注入,不在这里按需召回
        if self.semantic_memory:
            try:
                results = await self.semantic_memory.search_context(
                    user_input, limit=3
                )
                hints.extend(results)
            except Exception as exc:
                downgrade(exc, where='agents/manager.py')
        # 回退到普通记忆
        if not hints:
            hints = self.memory_store.get_skill_hints(user_input)

        # 会话内检索(短期记忆):只对指代型提问触发,避免噪声
        session_hints = self._search_session_history(user_input, session_id)
        hints.extend(session_hints)

        if not hints:
            return user_input

        hints_text = "\n".join(f"- {h}" for h in hints)
        return f"{user_input}\n\n[历史经验提示]:\n{hints_text}"

    def profile_block(self) -> str:
        """取用户画像文本块(长期记忆常驻注入)。

        **只是兼容转发** —— 画像的唯一读入口是 `core/memorykit/long_term.py`,
        主链已经直接走那里。保留这个方法是为了不打断既有调用方，
        不要再在它里面加逻辑（否则又多一条读路径）。
        """
        try:
            from core.memorykit import get_memory

            return get_memory().long_term.profile_block()
        except Exception as exc:
            self.logger.warning("Manager", f"读取用户画像失败: {exc}")
            return ""

    #: 兼容旧调用名
    def _build_profile_block(self) -> str:
        return self.profile_block()

    @staticmethod
    def _search_session_history(
        user_input: str, session_id: str
    ) -> List[str]:
        """在当前会话历史里检索与本轮相关的片段。

        触发条件:输入含指代词(上次/之前/刚才/那个…)。命中才返回,否则不打扰。
        """
        if not session_id or not _has_referential_marker(user_input):
            return []
        try:
            from core.session_memory import get_session_memory_store

            store = get_session_memory_store()
            hits = store.search(session_id, user_input, limit=3)
        except Exception:
            return []
        out: List[str] = []
        for msg in hits:
            content = (msg.content or "").strip()
            if not content:
                continue
            if len(content) > 300:
                content = content[:300] + "..."
            who = "用户" if msg.role == "user" else "助手"
            out.append(f"[本次会话历史] {who}: {content}")
        return out
