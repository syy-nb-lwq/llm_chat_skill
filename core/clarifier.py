"""L3 澄清层 —— 意图不明时**问一句**,而不是硬猜。

触发条件(`docs/12-意图分层识别方案.md` §2.5/§2.6):

| 条件 | 结果 |
|---|---|
| L1 判为 `clarify` 且 conf ≥ τ₁ | 直接进 L3 |
| L2 没产出工具任务、也没选中技能,且 L1 conf < τ₁ | 进 L3 |

三条硬约束(缺一不可):

1. **只问一个问题**,并给 2~3 个候选项 —— 一次问一堆等于没问
2. **澄清轮次上限 1 轮** —— 用户不答就直接答,不许把人卡在澄清里
3. **澄清轮不进 critic、不写长期记忆** —— 它不是一次任务执行

失败处理:LLM 不可用 → 退回模板化问题(仍然只问一句);
模板也造不出有意义的问题 → 返回 None,上层走直答兜底。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


# ===== 槽位 → 模板问题 =====

# 缺哪个槽位,就问哪个槽位。模板是 LLM 挂掉时的兜底。
_SLOT_QUESTIONS: Dict[str, str] = {
    "city": "你想查哪个城市?",
    "date": "你想查哪一天?",
    "target_skill": "你想用哪个技能来做?",
    "referent": "你指的是哪一个?",
}

_GENERIC_QUESTION = "我没太明白你想做什么,能再说明一下吗?"

#: 已知槽位(用于推断"缺了哪一项")
KNOWN_SLOTS = ("city", "date", "target_skill", "referent")


def detect_missing_slot(slots: Optional[Dict[str, Any]]) -> str:
    """从槽位表里找出**空的那一项**(缺的关键信息)。

    L1 会把抽到的槽位放进 `slots`;某个已知槽位出现但值为空,
    就说明这一项没抽出来 —— 追问它比泛问一句有用得多。
    """
    if not slots:
        return ""
    for key in KNOWN_SLOTS:
        if key in slots and not str(slots.get(key) or "").strip():
            return key
    return ""


@dataclass
class ClarificationRequest:
    """一次澄清请求。"""

    question: str
    options: List[str] = field(default_factory=list)
    slot: str = ""        # 缺哪个槽位(可空)
    reason: str = ""      # 为什么需要澄清(给日志/可观测看)
    round: int = 1        # 第几轮澄清(从 1 开始)
    source: str = "llm"   # llm / template —— 区分是模型生成还是兜底模板

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "slot": self.slot,
            "reason": self.reason,
            "round": self.round,
            "source": self.source,
        }

    def to_text(self) -> str:
        """给用户看的文本(问题 + 候选项)。"""
        if not self.options:
            return self.question
        lines = [self.question]
        for i, opt in enumerate(self.options, 1):
            lines.append(f"{i}. {opt}")
        return "\n".join(lines)


class L3Clarifier:
    """澄清层:生成**一个**带候选项的问题。"""

    #: 澄清轮次上限 —— 超了就直接答,别把人卡住
    MAX_ROUNDS = 1

    def __init__(self, max_rounds: int = MAX_ROUNDS):
        self.logger = get_logger()
        self.max_rounds = max(1, int(max_rounds))

    # ===== 对外 =====

    async def clarify(
        self,
        user_input: str,
        *,
        reason: str = "",
        slot: str = "",
        slots: Optional[Dict[str, Any]] = None,
        recent_turns: Optional[List[Dict[str, str]]] = None,
        skill_names: Optional[List[str]] = None,
        round_index: int = 1,
    ) -> Optional[ClarificationRequest]:
        """生成澄清问题。返回 None 表示"不该澄清,直接答"。"""
        if round_index > self.max_rounds:
            self.logger.info(
                "L3Clarifier", f"澄清轮次已达上限({self.max_rounds}),不再追问"
            )
            return None

        request = await self._ask_llm(
            user_input,
            reason=reason,
            slot=slot,
            slots=slots or {},
            recent_turns=recent_turns or [],
            skill_names=skill_names or [],
        )
        if request is None:
            request = self._fallback(
                user_input,
                reason=reason,
                slot=slot,
                slots=slots or {},
                round_index=round_index,
            )
        if request is None:
            return None
        request.round = round_index
        return request

    # ===== 内部 =====

    async def _ask_llm(
        self,
        user_input: str,
        *,
        reason: str,
        slot: str,
        slots: Dict[str, Any],
        recent_turns: List[Dict[str, str]],
        skill_names: List[str],
    ) -> Optional[ClarificationRequest]:
        """问模型要一个问题。失败返回 None(走模板)。"""
        from infra.llm import get_llm_client

        try:
            client = get_llm_client()
        except Exception as exc:
            self.logger.warning("L3Clarifier", f"LLM 客户端不可用: {exc}")
            return None

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_user_prompt(
                    user_input,
                    reason=reason,
                    slot=slot,
                    slots=slots,
                    recent_turns=recent_turns,
                    skill_names=skill_names,
                ),
            },
        ]

        raw = None
        try:
            raw = await client.chat_with_retry(
                messages, temperature=0.3, max_retries=1
            )
        except Exception as exc:
            self.logger.warning("L3Clarifier", f"澄清生成失败: {exc}")
            return None

        return self._parse(raw, reason=reason, slot=slot)

    def _parse(
        self, raw: str, *, reason: str, slot: str
    ) -> Optional[ClarificationRequest]:
        """解析并校验模型输出。"""
        if not raw:
            return None
        obj = _extract_json(raw)
        if not isinstance(obj, dict):
            return None

        question = str(obj.get("question") or "").strip()
        if not question:
            self.logger.warning("L3Clarifier", "澄清输出缺少 question")
            return None

        options = obj.get("options")
        options = _normalize_options(options)

        return ClarificationRequest(
            question=question[:200],
            options=options,
            slot=slot or str(obj.get("slot") or ""),
            reason=reason or str(obj.get("reason") or ""),
            source="llm",
        )

    def _fallback(
        self,
        user_input: str,
        *,
        reason: str,
        slot: str,
        slots: Dict[str, Any],
        round_index: int,
    ) -> Optional[ClarificationRequest]:
        """模板兜底:LLM 挂了也要问出一个**具体**的问题。"""
        # 优先用槽位模板 —— 比泛问一句有用得多
        for key in (slot,) if slot else tuple(slots.keys()):
            if key and key in _SLOT_QUESTIONS:
                return ClarificationRequest(
                    question=_SLOT_QUESTIONS[key],
                    options=_slot_options(key, slots),
                    slot=key,
                    reason=reason,
                    round=round_index,
                    source="template",
                )
        return ClarificationRequest(
            question=_GENERIC_QUESTION,
            options=[],
            slot="",
            reason=reason,
            round=round_index,
            source="template",
        )


# ===== 提示词 =====

_SYSTEM_PROMPT = """你在帮助手向用户**追问一个关键问题**。

规则:
1. **只问一个问题**。不要一次问好几件事。
2. 问题要具体,能一句话说清缺的是什么。不要问"您想要什么"这种废话。
3. 给 2~3 个候选项,让用户点选或照抄。候选项必须是**具体值**,不是"选项一"。
4. 如果上下文里已经有答案(比如上一轮说过城市),就不要重复问,直接确认。
5. 用中文,口语化,一句话。

只输出 JSON,不要解释:
{"question": "要查哪个城市的天气?", "options": ["厦门", "北京"]}
"""


def _build_user_prompt(
    user_input: str,
    *,
    reason: str,
    slot: str,
    slots: Dict[str, Any],
    recent_turns: List[Dict[str, str]],
    skill_names: List[str],
) -> str:
    parts = []
    if reason:
        parts.append(f"为什么需要澄清:{reason}")
    if slot:
        parts.append(f"缺的关键信息:{slot}")
    if slots:
        known = "、".join(f"{k}={v}" for k, v in slots.items() if v)
        if known:
            parts.append(f"已经知道的信息:{known}")
    if skill_names:
        parts.append("可用的技能:" + "、".join(skill_names[:10]))
    if recent_turns:
        history = []
        for turn in recent_turns:
            role = "用户" if turn.get("role") == "user" else "助手"
            content = (turn.get("content") or "").strip()
            if content:
                history.append(f"- {role}: {content}")
        if history:
            parts.append("最近对话:\n" + "\n".join(history))
    parts.append(f"用户这一轮说的是:{user_input}")
    return "\n\n".join(parts)


# ===== 工具函数 =====


def _normalize_options(options: Any) -> List[str]:
    """候选项清洗:去空、去重、最多 3 个。"""
    if not isinstance(options, list):
        return []
    out: List[str] = []
    for opt in options:
        if not isinstance(opt, str):
            continue
        text = opt.strip()
        if not text or text in out:
            continue
        out.append(text[:60])
        if len(out) >= 3:
            break
    return out


def _slot_options(slot: str, slots: Dict[str, Any]) -> List[str]:
    """模板兜底时的候选项:从已知信息里挑同类值。"""
    value = slots.get(slot)
    if isinstance(value, str) and value.strip():
        return [value.strip()[:60]]
    return []


def _extract_json(raw: str) -> Any:
    """从可能带围栏/前后缀的文本里抠出 JSON 对象。"""
    import re

    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


# ===== 单例 =====

_clarifier: Optional[L3Clarifier] = None


def get_clarifier() -> L3Clarifier:
    global _clarifier
    if _clarifier is None:
        _clarifier = L3Clarifier()
    return _clarifier


def reset_clarifier() -> None:
    global _clarifier
    _clarifier = None
