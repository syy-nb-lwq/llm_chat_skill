"""Manager Agent - 流转中枢:意图识别、技能选择、任务规划

分层意图识别(四层,见 `docs/12-意图分层识别方案.md`):

    L0 守卫层   纯规则,0 次 LLM。命中强结构信号就定案。
    L1 判别层   一次 LLM 调用,闭集分类(6 标签),带置信度。
    L2 规划层   LLM 规划工具/技能。**不再输出意图** —— 意图由 L1 给。
    L3 澄清层   低置信 / 缺关键信息时提出澄清问题(尚未落地)

关键点:意图与规划**解耦**。意图错了不再污染规划,规划错了也不再回写意图,
两者可以分别评估、分别优化。
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import re

from core.agent_base import BaseAgent
from core.context import Context
from core.memory import get_memory_store
from core.semantic_memory import get_semantic_memory, SemanticMemoryStore
from core.intent_labels import IntentLabel
from core.llm_judge import IntentDecision, L1Classifier, get_l1_classifier
from core.intent_detector import (
    IntentCategory, IntentResult, IntentDetector,
    get_intent_detector, is_chitchat,
)
from skills.manager import Skill, get_skill_store

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


@dataclass
class PlanResult:
    """规划结果"""
    intent: str                    # 意图标签(6 闭集之一,见 IntentLabel)
    intent_detail: str = ""        # 意图详情描述
    selected_skill: Optional["Skill"] = None
    is_retry: bool = False        # 是否是重试意图
    tool_tasks: List[Dict] = field(default_factory=list)
    need_llm: bool = False        # 是否需要 LLM 处理
    # 意图判别过程的可观测信息
    intent_layer: str = ""         # 产出该意图的层: L0 / L1 / L2
    intent_confidence: float = 0.0  # 置信度
    intent_uncertain: bool = False  # 置信度不足,已升级到 L2 兜底
    intent_slots: Dict[str, Any] = field(default_factory=dict)
    #: 规划层认为「目标已经达成」了吗？
    #:
    #: **这是目标循环的开关**：True（默认）表示这一轮规划就够了，
    #: 走完工具直接生成回答；False 表示还有没做完的事，需要再规划一轮。
    #: 默认 True 保证简单问答的行为和以前完全一样（永远只跑一轮）。
    goal_done: bool = True
    #: 还没做完什么（goal_done=False 时给下一轮看的提示）
    remaining_note: str = ""


# ===== 意图标签(6 闭集,与 core/intent_labels.py::IntentLabel 一一对应) =====
class IntentType:
    CHITCHAT = "chitchat"     # 闲聊:问候、感谢、道别等
    TASK = "task"             # 任务:需要执行工具/技能完成
    TEACH = "teach"           # 教导:用户教 Agent 新技能
    RETRY = "retry"           # 重试:重新执行上次的任务
    MANAGER = "manager"       # 技能管理:列出/查看/版本/回滚(M1-09)
    CLARIFY = "clarify"       # 意图不明:需要澄清

    # 兼容别名:旧代码里的 SKILL / UNKNOWN
    SKILL = "task"
    UNKNOWN = "clarify"


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

    def _available_tool_names(self) -> List[str]:
        """当前可用工具名(来自 ToolHub,含 MCP / 用户注册的工具)。"""
        try:
            from tools.hub import get_tool_hub

            names = list(get_tool_hub().names() or [])
        except Exception:
            names = []
        return names or list(self.FALLBACK_TOOLS)

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
        """规划 schema —— 工具枚举从 ToolHub 动态生成。

        硬编码枚举会导致 MCP / 用户注册的工具永远无法被规划器选中。

        **没有 `intent` 字段**:意图由 L0/L1 判别层给出,规划层只负责"怎么做"。
        (原先这里是自由文本,再由 `_is_simple_intent()` 对 LLM 自己写的散文做关键词
        二次分类 —— 意图和规划耦合,改一边就废另一边。)
        """
        tools = self._available_tool_names()
        return {
            "type": "object",
            "properties": {
                "plan_detail":    {"type": "string"},
                "selected_skill": {"type": "string"},
                "is_retry":       {"type": "boolean"},
                # 目标循环:这一轮规划之后目标达成了吗
                "goal_done":      {"type": "boolean"},
                "remaining_note": {"type": "string"},
                "tool_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":     {"type": "string"},
                            "type":   {"type": "string", "enum": tools},
                            "params": {"type": "object"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                            "parallel_group": {"type": "string"},
                        },
                        "required": ["type", "params"],
                    },
                },
            },
            "required": ["tool_tasks"],
        }

    def system_prompt(self) -> str:
        skills = self.skill_store.list_all()
        skills_list = self._format_skills(skills)

        # 工作区现状：让规划层知道"手上已经有什么"
        # （实机踩过：agent 把 key 存进文件后，下一轮却说自己"拿不到 key"）
        try:
            from core.workspace_view import build_workspace_block

            workspace_block = build_workspace_block()
        except Exception:
            workspace_block = ""
        tools = self._tool_signatures()
        return f"""你是一个任务规划助手。作为流转中枢,你需要:

1. 选择合适的技能(方法论)来完成任务
2. 规划需要的工具来获取数据

重要规则:
- **意图已经由上游判别层判定**,你不需要(也不应该)重新判断意图,只管规划怎么做
- **参数名必须用下面列出的**(带 ? 的表示可选);不要自己编参数名,
  也不要把自然语言描述塞进参数值里
- 能用的工具(名字+参数):
{tools}
- 如果用户说"重新回答"、"再说一遍"、"上次"、"之前"等,说明要重试上一次的执行,tool_tasks 要与上次保持一致
- 技能是完成任务的方法论,工具是获取数据的手段
- 没有合适的工具时,tool_tasks 返回空数组,不要硬凑

**多个工具前后衔接(必须用占位符,不要写文字描述)**
- 后一个任务依赖前一个的结果时:`depends_on` 填前一个任务的 `id`
- 参数里引用上游结果用 `${{任务id.data.字段}}`;**数组用下标,如 `${{t1.data.results[0].url}}`**
- 也支持 `${{任务id.success}}`、`${{任务id.error}}`、`${{user_input}}`、`${{user_input.字段}}`
- **绝对不要写"从结果里选一个链接"这种文字占位** —— 占位符会被替换成真实值,文字描述不会,执行期必失败

**关于 goal_done(很重要)**
- 每次规划都要判断:**执行完你规划的这些工具之后,用户的目标就达成了吗?**
- 达成了 → `goal_done: true`,并让 tool_tasks 覆盖完成任务所需的全部步骤
- 没达成 → `goal_done: false`,并在 `remaining_note` 里写清"还差什么",
  系统会带着已知结果再规划一轮(最多几轮)
- **不要为了"显得在干活"而给 false**;也不要明明还差关键一步就给 true
- 一次能规划全就给 true(把多步拆成多个 tool_task 按顺序执行更高效)

**「准备」不等于「完成」（最容易判错的一条）**
- `create_tool` / `write_script` 只是**把手艺准备好**，任务还没做完
- 用户如果说「先…然后…」「造好之后…」「再用它…」「再帮我…」，
  这是**分步要求**：把用户的话拆成动作清单，**逐项打勾，全打完才能 true**
- 例：用户说「先造一个算 BMI 的工具，然后用它算 1.75/70」
  - 只调了 create_tool → `goal_done: false`，
    `remaining_note: "工具已建好，还没用它计算 1.75m/70kg 的 BMI"`
  - 如果一轮里既造了工具又用了它 → 才能 true
- **判断方法**：问自己「用户拿到我的回答时，他要求的事情**全部做完了**吗？」
  只是"我准备好了"是不够的

**要造新工具时(create_tool)** —— 两个关键字段别弄错:
- 没有现成接口、要自己算 → `"kind": "code"` + `"implementation"`(Python 代码,
  用 `PARAMS['名']` 取参数,用 `return` 给结果)
- 有现成的 REST 接口 → `"kind": "http"` + `"url_template"`(参数用花括号占位)+ `"params"`
- **只读 http 工具立即生效;code / 写操作需人工批准**

示例(造一个计算工具,立即可用):
```json
{{"id": "t1", "type": "create_tool", "params": {{
  "name": "bmi_calc",
  "description": "根据身高体重算 BMI 并给出分级",
  "kind": "code",
  "implementation": "h = float(PARAMS['height']); w = float(PARAMS['weight']); bmi = w / (h * h); return {{'bmi': round(bmi, 1)}}",
  "params": [{{"name": "height", "type": "number", "description": "身高(米)"}}, {{"name": "weight", "type": "number", "description": "体重(公斤)"}}],
  "rationale": "用户需要反复计算 BMI"
}}}}
```

正确示例(先搜索再读最相关的那篇):
```json
"tool_tasks": [
  {{"id": "t1", "type": "web_search", "params": {{"query": "关键词"}}}},
  {{"id": "t2", "type": "web_fetch", "depends_on": ["t1"],
   "params": {{"url": "${{t1.data.results[0].url}}"}}}}
]
```

已有技能列表:
{skills_list}

{workspace_block}

严格按以下 JSON 格式输出:
{{
  "plan_detail": "这次要做什么的简短说明",
  "selected_skill": "选择的技能名称(没有则填空字符串)",
  "is_retry": true/false,  // 用户是否要求重试上一次的执行
  "tool_tasks": [
    {{"id": "t1", "type": "weather_query", "params": {{"city": "城市名", "date": "日期"}}}},
    {{"id": "t2", "type": "web_search", "params": {{"query": "搜索关键词"}}}}
  ]
}}"""

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
            except Exception:
                pass
        return self._semantic_memory

    async def plan(
        self,
        user_input: str,
        context: Optional[Context] = None,
        session_id: str = "",
        user_id: str = "",
        profile_block: str = "",
        prior_findings: str = "",
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

        # ===== 结构守卫(0 次 LLM,只处理空输入这类结构事实) =====
        intent_result = self.intent_detector.detect(user_input)
        label = intent_result.label

        if label is not None:
            self.logger.info(
                "Manager", f"结构守卫定案: {label.value} ({intent_result.detail})"
            )
            decided = self._plan_from_label(
                label,
                detail=intent_result.detail,
                context=context,
                layer="guard",
                confidence=intent_result.confidence,
                slots=intent_result.entities,
            )
            if decided is not None:
                return decided

        # ===== L1 判别层(一次 LLM 调用,闭集分类) =====
        decision = await self._l1_classify(user_input, context)
        if decision is not None:
            if decision.confidence < self._l1_threshold:
                # 低置信:不放弃,带 L1 结果升级 L2 兜底
                # 宁可多调一次工具,也不要漏答(D4)
                self.logger.info(
                    "Manager",
                    f"L1 置信不足({decision.confidence:.2f},"
                    f"判为 {decision.label.value}),升级 L2 兜底",
                )
            elif decision.label is not IntentLabel.TASK:
                # 非任务类意图:高置信可直接定案
                self.logger.info(
                    "Manager",
                    f"L1 定案: {decision.label.value} (置信度 {decision.confidence})",
                )
                decided = self._plan_from_label(
                    decision.label,
                    detail=decision.reason or f"L1 判别为 {decision.label.value}",
                    context=context,
                    layer="L1",
                    confidence=decision.confidence,
                    slots=decision.slots,
                )
                if decided is not None:
                    return decided
            else:
                self.logger.info(
                    "Manager",
                    f"L1 判为 task (置信度 {decision.confidence}),进入 L2 规划",
                )

        # ===== L2 规划层 =====
        plan = await self._llm_plan(
            user_input,
            context,
            session_id=session_id,
            user_id=user_id,
            intent_hint=decision,
            profile_block=profile_block,
            prior_findings=prior_findings,
        )
        if plan is not None:
            return plan

        # 规划也没产出 → 兜底
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
        """把一个意图标签翻成 PlanResult。

        返回 None 表示"这个标签不能直接定案,需要继续往下走"。
        """
        if label is IntentLabel.CHITCHAT:
            return PlanResult(
                intent=IntentType.CHITCHAT,
                intent_detail=detail,
                selected_skill=None,
                tool_tasks=[],
                intent_layer=layer,
                intent_confidence=confidence,
                intent_slots=dict(slots or {}),
            )

        if label is IntentLabel.TEACH:
            return PlanResult(
                intent=IntentType.TEACH,
                intent_detail=detail,
                selected_skill=None,
                tool_tasks=[],
                intent_layer=layer,
                intent_confidence=confidence,
                intent_slots=dict(slots or {}),
            )

        if label is IntentLabel.MANAGE:
            return PlanResult(
                intent=IntentType.MANAGER,
                intent_detail=detail or "技能管理操作",
                selected_skill=None,
                tool_tasks=[],
                need_llm=False,
                intent_layer=layer,
                intent_confidence=confidence,
                intent_slots=dict(slots or {}),
            )

        if label is IntentLabel.RETRY:
            # 重试需要上一轮的执行记录;没有上下文就不能重试
            if not context or len(context) == 0:
                return PlanResult(
                    intent=IntentType.TASK,
                    intent_detail="重试需要上下文,但没有历史记录",
                    selected_skill=None,
                    is_retry=True,
                    tool_tasks=[],
                    need_llm=False,
                    intent_layer=layer,
                    intent_confidence=confidence,
                )
            return PlanResult(
                intent=IntentType.RETRY,
                intent_detail=detail,
                selected_skill=None,
                is_retry=True,
                need_llm=True,  # 需要 LLM 取上次的 tool_tasks
                intent_layer=layer,
                intent_confidence=confidence,
                intent_slots=dict(slots or {}),
            )

        if label is IntentLabel.CLARIFY:
            # L1 判为"意图不明"且置信度够高 → 直接进 L3 澄清。
            # 置信度不够时由调用方降级到 L2 兜底(见 plan()).
            return PlanResult(
                intent=IntentType.CLARIFY,
                intent_detail=detail or "意图不明,需要澄清",
                selected_skill=None,
                tool_tasks=[],
                need_llm=False,
                intent_layer=layer,
                intent_confidence=confidence,
                intent_slots=dict(slots or {}),
            )

        # TASK:交给 L2 规划层
        return None

    async def _l1_classify(
        self, user_input: str, context: Optional[Context]
    ) -> Optional[IntentDecision]:
        """调用 L1 闭集分类器。失败返回 None(由调用方降级)。"""
        if not self._l1_enabled:
            return None
        try:
            recent = self._recent_turns(context, limit=2)
            return await self.l1_classifier.classify(
                user_input, recent_turns=recent
            )
        except Exception as exc:
            self.logger.warning("Manager", f"L1 分类失败: {exc}")
            return None

    @staticmethod
    def _recent_turns(context: Optional[Context], limit: int = 2) -> List[Dict[str, str]]:
        """取最近几轮对话(给 L1 做上下文参考)。"""
        if context is None or len(context) == 0:
            return []
        msgs = [
            m for m in context.messages
            if m.role in ("user", "assistant") and (m.content or "").strip()
        ]
        out = []
        for m in msgs[-limit * 2:]:
            out.append({"role": m.role, "content": (m.content or "").strip()[:500]})
        return out


    async def _llm_plan(
        self,
        user_input: str,
        context: Optional[Context],
        session_id: str = "",
        user_id: str = "",
        intent_hint: Optional[IntentDecision] = None,
        profile_block: str = "",
        prior_findings: str = "",
    ) -> Optional[PlanResult]:
        """L2 规划层:规划要调哪些工具、用哪个技能。

        **不再输出意图** —— 意图由 L0/L1 判定,这里只负责"怎么做"。
        `intent_hint` 是 L1 的判别结果,作为强约束注入提示词。
        `profile_block` 是调用方(Agent)本轮已取好的画像,空则自己取。
        """
        skills = self.skill_store.list_all()

        # 读取历史教训 hints,拼到 user_input 前面
        enriched_input = await self._enrich_with_hints(
            user_input, session_id=session_id, user_id=user_id
        )

        # 用户画像(长期记忆):**常驻注入**,不依赖 query 命中
        if not profile_block:
            profile_block = self.profile_block(user_id)

        try:
            llm_plan = await self.think_json(
                self._build_user_prompt(
                    enriched_input, context, intent_hint, profile_block,
                    prior_findings=prior_findings,
                ),
                self.plan_schema,
            )
        except Exception as e:
            self.logger.error("Manager", f"LLM 规划失败: {e}")
            return PlanResult(
                intent=IntentType.TASK,
                intent_detail=f"LLM 调用失败: {str(e)[:50]}",
                selected_skill=None,
                tool_tasks=[],
                intent_layer="L2",
                intent_uncertain=True,
            )

        selected_skill = None
        skill_name = llm_plan.get("selected_skill") or ""
        if skill_name:
            for s in skills:
                if s.name == skill_name:
                    selected_skill = s
                    break

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
        hint_block = self._build_intent_hint_block(intent_hint)
        findings_block = ""
        if (prior_findings or "").strip():
            findings_block = (
                "## 前几轮已经拿到的结果(不要重复查这些,继续往下做)\n"
                + prior_findings.strip()
            )
        prefix = "\n\n".join(
            x for x in (hint_block, summary_block, profile_block, findings_block) if x
        )

        if context is None or len(context) == 0:
            return f"{prefix}\n\n{user_input}".strip() if prefix else user_input
        history = context.to_llm_messages(max_tokens=2000)
        if not history:
            return f"{prefix}\n\n{user_input}".strip() if prefix else user_input
        # 跳过 system 和 tool 类型,只保留可见的对话
        recent = [
            m for m in history if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if len(recent) <= 1:
            if prefix:
                return f"{prefix}\n\n{user_input}"
            return user_input
        # 最后一条是当前 user_input 的内容(Agent.handle 已 add_user_message);
        # 把它剔除,只拼历史。
        prior = recent[:-1]
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

    async def _enrich_with_hints(
        self, user_input: str, session_id: str = "", user_id: str = ""
    ) -> str:
        """读取历史教训 hints,拼到 user_input 前面

        优先使用语义记忆,回退到普通记忆。
        另外:指代型提问("上次那个方案…")会检索**本会话历史**,
        让"回来继续问答"真正可用。
        """
        hints: List[str] = []

        # 尝试语义记忆(执行经验类);画像走常驻注入,不在这里按需召回
        if self.semantic_memory and user_id:
            try:
                results = await self.semantic_memory.search_context(
                    user_input, user_id=user_id, limit=3
                )
                hints.extend(results)
            except Exception:
                pass

        # 回退到普通记忆
        if not hints:
            hints = self.memory_store.get_skill_hints(user_input)

        # 会话内检索(短期记忆):只对指代型提问触发,避免噪声
        session_hints = self._search_session_history(user_input, session_id, user_id)
        hints.extend(session_hints)

        if not hints:
            return user_input

        hints_text = "\n".join(f"- {h}" for h in hints)
        return f"{user_input}\n\n[历史经验提示]:\n{hints_text}"

    def profile_block(self, user_id: str) -> str:
        """取用户画像文本块(长期记忆常驻注入)。

        公开方法 —— `Agent` 用它每轮取一次,同时喂给规划器和回答生成器,
        避免两处各读一次库、拿到不一致的内容。

        失败一律返回空串 —— 画像读不到不能影响正常回答。
        """
        if not user_id or not self.semantic_memory:
            return ""
        try:
            return self.semantic_memory.build_profile_block(user_id)
        except Exception as exc:
            self.logger.warning("Manager", f"读取用户画像失败: {exc}")
            return ""

    #: 兼容旧调用名
    def _build_profile_block(self, user_id: str) -> str:
        return self.profile_block(user_id)

    @staticmethod
    def _search_session_history(
        user_input: str, session_id: str, user_id: str
    ) -> List[str]:
        """在当前会话历史里检索与本轮相关的片段。

        触发条件:输入含指代词(上次/之前/刚才/那个…)。命中才返回,否则不打扰。
        """
        if not session_id or not _has_referential_marker(user_input):
            return []
        try:
            from core.session_memory import get_session_memory_store

            store = get_session_memory_store()
            hits = store.search(session_id, user_input, limit=3, user_id=user_id or None)
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
