"""意图识别的**结构守卫** —— 不做意图判断,判断交给 L1(`core/llm_judge.py`)。

## 为什么这里几乎没有规则了

这个模块以前是 L0 守卫层:一大堆关键词表和正则,命中就直接定案,
而且**排在 L1 前面**,命中后 `analyze()` 直接 return,L1 根本看不到这句话。

真机基线把代价算出来了:

| 做法 | 准确率(130 条) |
|---|---|
| L0 规则 + L1(旧) | **94.7%** |
| 全部交给 L1(现在) | **97.7%** |

规则**不是"没帮上忙",是在拖低准确率**。根因不是某条正则写错了,是
**这种事就不该用规则做**:

- 意图是**语义**问题,规则只能看到**字面**。
  「教我怎么用这个工具」和「教我一种排查方法」字面都含「教我」,语义相反。
- 用户有一万种说法。每条规则都是在**猜用户会怎么说**,
  猜不完,而且猜错的时候是**静默地错** —— 没人会发现。
- 最严重的是它抢在 LLM 前面。这次的 10 条错判里,
  我把提示词连改 5 版都没效果,因为**那段代码根本没被执行**。

## 现在这里只剩什么

只保留**结构级**判断 —— 也就是"不需要理解语义就能确定"的事实:

- 空输入 / 纯空白 → 需要澄清。

就这一条。**没有任何"猜用户怎么说"的规则。**

> 判据:如果一条规则的成立依赖"用户大概会这样表达",它就不该在这里。

## 架构

    analyze()
      ├─ 结构守卫(本模块,0 次 LLM,只处理空输入)
      ├─ L1 闭集判别(1 次 LLM,6 标签)—— **意图的唯一裁决者**
      └─ L2 规划(工具/技能)

历史模块保留说明:`IntentCategory` 枚举仍被部分老代码/测试引用,
所以枚举和映射表留着,但**不再有任何规则产出它们**(除了 CLARIFY)。
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from core.intent_labels import IntentLabel, TaskKind
from infra.logger import get_logger


# ===== 意图类别 =====
# 保留这个枚举只是为了兼容老引用(测试、`is_chitchat` 等)。
# `IntentDetector` 现在只会产出 UNKNOWN / CLARIFY,不再产出其余值。
class IntentCategory(Enum):
    # 闲聊类
    GREETING = "greeting"
    FAREWELL = "farewell"
    THANKS = "thanks"
    ACKNOWLEDGE = "acknowledge"
    NEGATIVE = "negative"
    SELF_QUERY = "self_query"

    # 任务类
    WEATHER = "weather"
    SEARCH = "search"
    CALCULATE = "calculate"
    TRANSLATE = "translate"
    CODE = "code"
    CUSTOM = "custom"

    # 特殊意图
    TEACH = "teach"
    RETRY = "retry"
    MANAGE = "manage"
    CLARIFY = "clarify"

    # 未定案(交给 L1)
    UNKNOWN = "unknown"


_CATEGORY_TO_LABEL: Dict[IntentCategory, IntentLabel] = {
    IntentCategory.GREETING: IntentLabel.CHITCHAT,
    IntentCategory.FAREWELL: IntentLabel.CHITCHAT,
    IntentCategory.THANKS: IntentLabel.CHITCHAT,
    IntentCategory.ACKNOWLEDGE: IntentLabel.CHITCHAT,
    IntentCategory.NEGATIVE: IntentLabel.CHITCHAT,
    IntentCategory.SELF_QUERY: IntentLabel.CHITCHAT,
    IntentCategory.WEATHER: IntentLabel.TASK,
    IntentCategory.SEARCH: IntentLabel.TASK,
    IntentCategory.CALCULATE: IntentLabel.TASK,
    IntentCategory.TRANSLATE: IntentLabel.TASK,
    IntentCategory.CODE: IntentLabel.TASK,
    IntentCategory.CUSTOM: IntentLabel.TASK,
    IntentCategory.TEACH: IntentLabel.TEACH,
    IntentCategory.RETRY: IntentLabel.RETRY,
    IntentCategory.MANAGE: IntentLabel.MANAGE,
    IntentCategory.CLARIFY: IntentLabel.CLARIFY,
}

_CATEGORY_TO_TASK_KIND: Dict[IntentCategory, TaskKind] = {
    IntentCategory.WEATHER: TaskKind.WEATHER,
    IntentCategory.SEARCH: TaskKind.SEARCH,
    IntentCategory.CALCULATE: TaskKind.CALCULATE,
    IntentCategory.TRANSLATE: TaskKind.TRANSLATE,
    IntentCategory.CODE: TaskKind.CODE,
    IntentCategory.CUSTOM: TaskKind.CUSTOM,
}


@dataclass
class IntentResult:
    """结构守卫的结果。

    绝大多数情况下 `label is None` —— 表示"这里不做判断,交给 L1"。
    """

    category: IntentCategory
    confidence: float = 1.0
    detail: str = ""
    need_llm: bool = True
    tool_type: Optional[str] = None
    entities: Dict[str, Any] = field(default_factory=dict)
    layer: str = "guard"

    @property
    def label(self) -> Optional[IntentLabel]:
        """路由标签。None 表示未定案 → 交给 L1。"""
        return _CATEGORY_TO_LABEL.get(self.category)

    @property
    def task_kind(self) -> Optional[TaskKind]:
        return _CATEGORY_TO_TASK_KIND.get(self.category)


class IntentDetector:
    """结构守卫(名字保留,行为已从"规则判意图"改为"只做结构判断")。"""

    def __init__(self):
        self.logger = get_logger()

    def detect(self, text: Optional[str]) -> IntentResult:
        """**只判断结构上确定的事**,不判断意图。

        契约(与历史一致,别改成返回 None):
        - 能定案 → 返回带 `label` 的 IntentResult(**目前只有空输入**)
        - 不能定案 → 返回 `category=UNKNOWN` 的 IntentResult,其 `.label is None`
          → 调用方据此交给 L1

        "不能定案"是一个**正常结果**,不是 None —— 返回 None 会让所有
        `result.category` 的调用方炸掉(而且分不清"没判"和"调用失败")。
        """
        raw = text or ""
        if not raw.strip():
            return IntentResult(
                category=IntentCategory.CLARIFY,
                confidence=1.0,
                detail="空输入",
                need_llm=False,
            )
        # 其余全部未定案 —— 意图由 L1 判
        return IntentResult(
            category=IntentCategory.UNKNOWN,
            confidence=0.0,
            detail="结构守卫不判意图,交给 L1",
            need_llm=True,
        )


# ===== 兼容老引用的辅助函数 =====

def is_chitchat(category: IntentCategory) -> bool:
    """是否为闲聊类别(仅用于老代码/测试,不再由规则产出)。"""
    return category in (
        IntentCategory.GREETING,
        IntentCategory.FAREWELL,
        IntentCategory.THANKS,
        IntentCategory.ACKNOWLEDGE,
        IntentCategory.NEGATIVE,
        IntentCategory.SELF_QUERY,
    )


def is_task(category: IntentCategory) -> bool:
    """是否为任务类别(仅用于老代码/测试,不再由规则产出)。"""
    return category in (
        IntentCategory.WEATHER,
        IntentCategory.SEARCH,
        IntentCategory.CALCULATE,
        IntentCategory.TRANSLATE,
        IntentCategory.CODE,
        IntentCategory.CUSTOM,
    )


# ===== 单例 =====

_intent_detector: Optional[IntentDetector] = None


def get_intent_detector() -> IntentDetector:
    global _intent_detector
    if _intent_detector is None:
        _intent_detector = IntentDetector()
    return _intent_detector
