"""L1 判别层 —— 闭集意图分类器。

为什么单独一层(见 `docs/12-意图分层识别方案.md` §6.2):
不是为了省钱,而是

1. **可评估** —— 意图准确率可以单独度量,不被规划质量干扰
2. **提示词干净** —— 分类提示词只放标签定义;规划提示词只放技能/工具清单
3. **参数可分开调** —— 分类要 `temperature=0`;规划需要创造性

用哪个模型:**默认复用主模型**。`config.intent_model` 留空即复用;
显式配置时才切到别的模型(将来发现主模型太慢/太贵,改一行配置即可)。

失败处理:LLM 不可用 / 输出不合法 → 返回 None,由调用方降级
(退到 L0 规则,再退到"直接走规划层"),**绝不让分类失败拖垮整条链路**。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.intent_labels import (
    IntentLabel,
    build_intent_prompt,
    build_system_prompt,
    parse_label,
)
from infra.logger import get_logger


@dataclass
class IntentDecision:
    """L1 的判别结果。"""

    label: IntentLabel
    confidence: float = 0.0
    slots: Dict[str, Any] = field(default_factory=dict)
    needs_clarify: bool = False
    reason: str = ""
    layer: str = "L1"
    degraded: bool = False  # True = LLM 不可用,靠兜底猜的

    @property
    def label_value(self) -> str:
        return self.label.value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label.value,
            "confidence": round(self.confidence, 3),
            "slots": dict(self.slots),
            "needs_clarify": self.needs_clarify,
            "reason": self.reason,
            "layer": self.layer,
            "degraded": self.degraded,
        }


class L1Classifier:
    """闭集意图分类器(一次 LLM 调用)。"""

    def __init__(
        self,
        threshold: Optional[float] = None,
        model: str = "",
        max_retries: int = 2,
    ):
        self.logger = get_logger()
        self.threshold = (
            threshold if threshold is not None else _default_threshold()
        )
        self.model = model or _default_model()
        self.max_retries = max(1, int(max_retries))

    # ===== 对外 =====

    async def classify(
        self,
        user_input: str,
        recent_turns: Optional[List[Dict[str, str]]] = None,
        profile: str = "",
    ) -> Optional[IntentDecision]:
        """闭集分类。失败返回 None(调用方负责降级)。"""
        text = (user_input or "").strip()
        if not text:
            return None

        messages = [
            {
                "role": "system",
                "content": build_system_prompt(),
            },
            {
                "role": "user",
                "content": build_intent_prompt(text, recent_turns, profile),
            },
        ]

        raw = await self._call(messages)
        if raw is None:
            return None
        parsed = self._parse(raw)
        if parsed is None:
            return None
        return parsed

    def is_confident(self, decision: Optional[IntentDecision]) -> bool:
        """置信度是否达到阈值(可定案)。"""
        if decision is None:
            return False
        return decision.confidence >= self.threshold

    # ===== 内部 =====

    async def _call(self, messages: List[Dict[str, str]]) -> Optional[str]:
        from infra.llm import get_llm_client

        try:
            client = get_llm_client()
        except Exception as exc:
            self.logger.warning("L1Classifier", f"LLM 客户端不可用: {exc}")
            return None

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return await client.chat_with_retry(
                    messages,
                    temperature=0.0,
                    model=self.model or None,
                    max_retries=1,
                )
            except Exception as exc:
                last_err = exc
                self.logger.warning(
                    "L1Classifier", f"分类调用失败(尝试 {attempt}/{self.max_retries}): {exc}"
                )
                # 重试之间要**退避** —— 连续两次立刻重试等于把同一个请求
                # 再打一遍,对方正在限流/抖动的时候只会更快被拒。
                # 真机跑基线时抓到的:突发流量下,无间隔重试会把
                # "连接失败"放大成整轮 unavailable。
                if attempt < self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
        self.logger.warning("L1Classifier", f"分类放弃: {last_err}")
        return None

    def _parse(self, raw: str) -> Optional[IntentDecision]:
        """解析并校验 LLM 输出。"""
        if not raw:
            return None
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            return None

        label = parse_label(obj.get("label"))
        if label is None:
            self.logger.warning(
                "L1Classifier", f"非法 label: {obj.get('label')!r}"
            )
            return None

        confidence = _coerce_confidence(obj.get("confidence"))
        if confidence is None:
            self.logger.warning("L1Classifier", "缺少合法 confidence,按 0 处理")
            confidence = 0.0

        slots = obj.get("slots")
        if not isinstance(slots, dict):
            slots = {}

        return IntentDecision(
            label=label,
            confidence=confidence,
            slots={k: v for k, v in slots.items() if isinstance(v, (str, int, float))},
            needs_clarify=bool(obj.get("needs_clarify")),
            reason=str(obj.get("reason") or "")[:200],
            layer="L1",
        )


# ===== 工具函数 =====

_JSON_RE = None


def _extract_json(raw: str) -> Any:
    """从可能带围栏/前后缀的文本里抠出 JSON 对象。"""
    global _JSON_RE
    if _JSON_RE is None:
        import re

        _JSON_RE = re.compile(r"\{[\s\S]*\}")
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _coerce_confidence(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v < 0:
            return 0.0
        if v > 1:
            return 1.0
        return v
    if isinstance(value, str):
        try:
            return _coerce_confidence(float(value.strip()))
        except Exception:
            return None
    return None


def _default_threshold() -> float:
    try:
        from infra.config import config

        return float(getattr(config, "intent_l1_threshold", 0.75))
    except Exception:
        return 0.75


def _default_model() -> str:
    """`intent_model` 留空 = 复用主模型。"""
    try:
        from infra.config import config

        return str(getattr(config, "intent_model", "") or "")
    except Exception:
        return ""


# ===== 单例 =====

_classifier: Optional[L1Classifier] = None


def get_l1_classifier() -> L1Classifier:
    global _classifier
    if _classifier is None:
        _classifier = L1Classifier()
    return _classifier


def reset_l1_classifier() -> None:
    global _classifier
    _classifier = None
