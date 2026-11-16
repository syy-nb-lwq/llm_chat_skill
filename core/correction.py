"""用户纠正检测 —— 让「回答不对」这个信号真正进得去

## 为什么需要它

`core/critic.py` 里有一行 `user_corrected = context.user_feedback is not None`，
但**全仓没有任何地方写入 `user_feedback`** —— 这条链是死的。

后果是 critic 只能看到"工具调用是否成功"：

| 情况 | 系统判断 |
|---|---|
| 工具成功 + 答案完全跑题 | 成功 |
| 没调工具 + 答案胡说 | 成功 |
| 用户下一轮纠正"不对，应该是…" | **完全不知情** |

对一个"越用越好"的产品来说，**用户纠正是最重要的质量信号** —— 它是唯一的
ground truth。检测它就是让自演化有东西可学。

## 两条获取途径

1. **显式**（可靠）：前端「👍 / 👎」按钮 → `POST /api/feedback`
2. **隐式**（轻量）：下一轮输入里出现纠正语 → 本模块检测

本模块负责第 2 条。显式那条由 `backend/main.py::submit_feedback` 走同一条链路。

## 设计取舍

**只做规则，不做 LLM。** 纠正检测的误判代价不对称：
- 漏判一个纠正 → 少学一次（可接受）
- 误判一个正常输入 → 污染记忆库，系统把正常输入当"错答"学习（不可接受）

所以宁可漏判。
"""
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


#: 明确否定 —— 强信号
_NEGATION = [
    r"不对",
    r"错了",
    r"搞错",
    r"说错",
    r"答非所问",
    r"你没懂",
    r"没理解",
    r"理解错",
    r"不是这个",
    r"不是我要",
    r"不是我问",
    r"我要的不是",
    r"问的不是",
    r"完全不对",
    r"根本不对",
    r"你理解有误",
    r"偏题",
    r"跑题",
]

#: 替换指示 —— "应该是 A，不是 B"
_REPLACEMENT = [
    r"应该是",
    r"应该是.{0,20}而不是",
    r"我要的是",
    r"我问的是",
    r"我说的(?:是|就是)",
    r"不是[^，。？！!?]{1,8}[,，]\s*是",
    r"不是[^，。？！!?]{1,8}而是",
]

#: 要求重做
_REDO = [
    r"重来",
    r"重新(?:来|做|写|生成|整理|算|回答)?",
    r"再改",
    r"改一下",
    r"再试一次",
    r"换一种",
    r"换一[个种]",
]

#: 不满意但没明确否定
_DISSATISFACTION = [
    r"太啰嗦",
    r"太长",
    r"太长了",
    r"看不懂",
    r"没说清楚",
    r"不清楚",
    r"不准确",
    r"不具体",
    r"没回答(我的)?问题",
    r"答得不好",
    r"不满意",
]

#: 纯确认 —— 必须排除,否则"对"这种会被当纠正
_AFFIRM = [
    r"^(对|好的|好吧|嗯|是|是的|没错|可以|行|收到|明白|知道了|了解)[。.!！~ ]*$",
    r"^(谢谢|多谢|感谢|辛苦了|ok|okay|yes|good|thanks)[。.!！~ ]*$",
    r"^(不错|挺好|很好|棒|厉害|可以啊|可以的)",
]

#: 否定词的例外 —— "不是吧"是惊讶,不是纠正
_NEGATION_EXCEPT = [
    r"不是吧",
    r"不是吗",
    r"是不是",
    r"不只是",
    r"不仅",
    r"不错",
    r"不断",
    r"不对吗",
]

#: 每类的置信度
_KIND_SCORE = {
    "negation": 0.9,
    "replacement": 0.8,
    "redo": 0.75,
    "dissatisfaction": 0.65,
}


@dataclass
class CorrectionSignal:
    """一次纠正信号"""

    text: str                      # 用户原话(纠正内容)
    kind: str                      # negation / replacement / redo / dissatisfaction
    confidence: float = 0.0
    matched: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "matched": list(self.matched),
        }


def _compile(patterns: List[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


_RE_NEGATION = _compile(_NEGATION)
_RE_REPLACEMENT = _compile(_REPLACEMENT)
_RE_REDO = _compile(_REDO)
_RE_DISSATISFACTION = _compile(_DISSATISFACTION)
_RE_AFFIRM = _compile(_AFFIRM)
_RE_NEG_EXCEPT = _compile(_NEGATION_EXCEPT)

#: 输入超过这个长度时,即使含纠正词也更可能是"新任务 + 抱怨"
_LONG_INPUT = 60


class CorrectionDetector:
    """从用户输入里识别「这是在纠正上一条回答」。

    用法::

        d = CorrectionDetector()
        sig = d.detect("不对,应该是上海", previous_answer="北京今天晴")
        if sig:
            ...  # 把 sig.text 作为 user_feedback 喂给 critic
    """

    def __init__(self, min_confidence: float = 0.7):
        self.min_confidence = min_confidence

    def detect(
        self,
        user_input: str,
        previous_answer: str = "",
        *,
        has_previous_answer: Optional[bool] = None,
    ) -> Optional[CorrectionSignal]:
        """检测纠正信号。

        Args:
            user_input: 本轮用户输入
            previous_answer: 上一条助手回答(有内容才可能"被纠正")
            has_previous_answer: 显式声明"有没有上一条回答"。
                传 None 时按 `previous_answer` 非空判断。

        Returns:
            命中返回 `CorrectionSignal`,否则 None
        """
        text = (user_input or "").strip()
        if not text:
            return None

        # 纯确认不是纠正
        if _RE_AFFIRM.match(text):
            return None

        # 没有"被纠正的对象" → 不构成纠正
        has_prev = (
            bool((previous_answer or "").strip())
            if has_previous_answer is None
            else has_previous_answer
        )
        if not has_prev:
            return None

        matched: List[str] = []
        kind = ""
        score = 0.0

        # 例外词先过滤("不是吧"是惊讶,不算纠正)
        filtered = _RE_NEG_EXCEPT.sub("", text)

        # 用 finditer + group(0) 而不是 findall:
        # findall 在有捕获组时返回的是**组内容**而不是整个匹配,
        # 会静默返回空串(踩过这个坑)。
        hits_by_kind: Dict[str, List[str]] = {}
        for name, regex in (
            ("negation", _RE_NEGATION),
            ("replacement", _RE_REPLACEMENT),
            ("redo", _RE_REDO),
            ("dissatisfaction", _RE_DISSATISFACTION),
        ):
            hits = [m.group(0) for m in regex.finditer(filtered) if m.group(0)]
            if hits:
                hits_by_kind[name] = hits
                matched.extend(hits)
                score = max(score, _KIND_SCORE[name])

        if not matched:
            return None

        # kind 取**最具体**的那一类:
        # "不对,应该是上海" 同时命中否定和替换,但"应该是上海"信息量更大
        for name in ("replacement", "negation", "redo", "dissatisfaction"):
            if name in hits_by_kind:
                kind = name
                break

        # 长输入更像"新任务顺带吐槽",降权避免误伤
        if len(text) > _LONG_INPUT:
            score *= 0.7

        if score < self.min_confidence:
            return None

        return CorrectionSignal(
            text=text,
            kind=kind,
            confidence=score,
            matched=list(dict.fromkeys(matched)),
        )


# ---- 全局单例 ----

_detector: Optional[CorrectionDetector] = None


def get_correction_detector() -> CorrectionDetector:
    global _detector
    if _detector is None:
        _detector = CorrectionDetector()
    return _detector


def reset_correction_detector() -> None:
    global _detector
    _detector = None
