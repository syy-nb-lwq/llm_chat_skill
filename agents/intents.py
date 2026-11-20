"""意图判别层 —— L0 结构守卫 + L1 闭集分类 + 标签→PlanResult 映射。

## 为什么从 ManagerAgent 里拆出来

`plan()` 原本把三件事串在一个函数里：结构守卫、L1 分类、L2 规划。
前两件是**判别**（"这是什么意图"），第三件是**规划**（"该怎么做"）。
判别层不需要 skill_store / memory_store / contextvar，完全可以独立测：

- 给一句输入，断言判成哪个 `IntentLabel`；
- 给一个标签，断言翻成什么 `PlanResult`；

不用构造整个 ManagerAgent（它带 5 个 store 依赖）。

`ManagerAgent.plan()` 现在只做编排：依次调这三个函数，决定何时往下走。

## 分层不变式（与 ARCHITECTURE.md 一致）

- **L0 结构守卫**：只判"不需理解语义就能确定"的事（空输入）。
  **没有任何"猜用户怎么说"的规则** —— 真机基线 94.7%（规则+LLM）vs 97.7%（只有 LLM）。
- **L1 闭集分类**：意图的**唯一裁决者**，6 个标签，带置信度。
- **L2 规划**：只输出"怎么做"，**不再输出意图**。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.context import Context
from core.intent_labels import IntentLabel
from core.llm_judge import IntentDecision
from core.exception_policy import downgrade

# 从 manager 导入类型与 enum，避免循环定义
from agents.manager_types import IntentType, PlanResult


def plan_from_label(
    label: IntentLabel,
    *,
    detail: str = "",
    context: Optional[Context] = None,
    layer: str = "L1",
    confidence: float = 0.0,
    slots: Optional[Dict[str, Any]] = None,
) -> Optional[PlanResult]:
    """把一个意图标签翻成 PlanResult。

    返回 None 表示"这个标签不能直接定案，需要继续往下走 L2"。
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
        # 重试需要上一轮的执行记录；没有上下文就不能重试
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
        # 置信度不够时由调用方降级到 L2 兜底(见 ManagerAgent.plan()).
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


async def classify_l1(
    classifier,
    user_input: str,
    context: Optional[Context],
    *,
    enabled: bool,
    logger,
) -> Optional[IntentDecision]:
    """调用 L1 闭集分类器。失败返回 None（由调用方降级到 L2）。"""
    if not enabled:
        return None
    try:
        recent = recent_turns(context, limit=2)
        return await classifier.classify(user_input, recent_turns=recent)
    except Exception as exc:
        logger.warning("Manager", f"L1 分类失败: {exc}")
        downgrade(exc, where="agents/intents.py::classify_l1")
        return None


def recent_turns(context: Optional[Context], limit: int = 2) -> List[Dict[str, str]]:
    """取最近几轮对话（给 L1 做上下文参考）。"""
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
