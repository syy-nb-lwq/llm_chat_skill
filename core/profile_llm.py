"""画像抽取（LLM 版）—— 取代原来那套只认固定句式的规则。

## 为什么必须换掉规则

原来的实现只认「我习惯…」「我喜欢…」「记住…」这类句式。
结果是：用户一整天正常下任务，**一条都抽不出来**；
偶尔抽出来的还是错的 —— 实机记录：

    constraint: 不要用国际站然后可以开始生成视频

它把一句**一次性任务指令**当成了长期雷区。

## 为什么当初不敢用 LLM

因为画像会**持续注入到之后每一轮的 system prompt**：
一条错的"用户偏好"会污染后面所有回答，而且用户看不出来。
所以写路径的判据一直是「宁可漏，不可错」。

## 用 LLM 之后怎么保住这个判据

关键设计：**证据必须能在用户原话里逐字找到，否则丢弃。**

    LLM 说：用户的偏好是「不喜欢国际站」
      → 要求它给出 evidence
      → evidence 必须是 user_input 的子串
      → 给不出 / 对不上 → 整条丢弃

这一条把"模型编造偏好"变成结构性不可能 —— 不是靠提示词求它别编，
而是代码层面校验不过就扔。除此之外还有：

- 类型必须在白名单里（`preference/habit/method/constraint/fact`）
- 置信度低于阈值不收（默认 0.6）
- 一轮最多收 3 条（防止一次性灌一堆）
- 明确列出**不要**提取的东西（一次性任务指令、临时参数、对当前话题的评论）

> 判据没变，手段变了：从"规则保证精确" 变成 "**证据校验保证精确**"。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


#: 允许的画像类型（与后端 `_PROFILE_LABELS` 保持一致）
ALLOWED_TYPES = {"preference", "habit", "method", "constraint", "fact"}

#: 低于这个置信度不收
MIN_CONFIDENCE = 0.6
#: 一轮最多收几条
MAX_FACTS_PER_TURN = 3
#: 单条内容长度范围
MIN_CONTENT_CHARS = 4
MAX_CONTENT_CHARS = 120


PROFILE_PROMPT = """你要从用户**这一句话**里，判断有没有值得长期记住的信息。

## 判据：只记「长期稳定」的东西

值得记（如果能从这句话看出来）：
- 身份 / 长期角色（"我是做短剧的"）
- 长期偏好（"我一般不喜欢太啰嗦的解释"）
- 固定习惯（"我们周会都在周一"）
- 硬性约束 / 雷区（"这个项目不能用国际站"）
- 长期目标（"我今年想把这个产品做上线"）

**绝对不要记**（这些是临时状态，记了会污染以后的每一轮）：
- 一次性的任务指令（"帮我生成一个视频"、"然后可以开始了"）
- 临时参数（"这次用 5 秒"、"改成 1280x720"）
- 对当前话题的评论（"这个不错"、"再试一次"）
- 你自己的推测（用户没明说的，一律不记）
- 助手说过的话

## 已知的反面例子（**必须避免**）

用户说：「不要用国际站然后可以开始生成视频」
错误抽取：`constraint: 不要用国际站然后可以开始生成视频`
为什么错：这是一次性的操作指令，"不要用国际站"是针对**这一次**的，
不构成长期雷区。除非用户明确说"以后都不要用国际站"。

## 输出格式

只输出 JSON：

{{"facts": [
  {{
    "type": "preference|habit|method|constraint|fact",
    "content": "规范化的短句（不超过 60 字，第三人称描述这个用户）",
    "evidence": "用户原话里**逐字复制**的那一小段（必须原样，不能改写）",
    "confidence": 0.0~1.0,
    "importance": 0.0~1.0
  }}
]}}

## 用户这句话

{user_input}

## 提醒

- **没有值得记的就输出 `{{"facts": []}}`** —— 这是最常见、最正确的结果
- `evidence` 必须是上面那句话里**连续出现**的一段原文，不能是 paraphrase
- 不确定的宁可不记
"""


def _norm(text: str) -> str:
    """去掉所有空白，用于证据比对（容忍换行/空格差异）。"""
    return re.sub(r"\s+", "", text or "")


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    blob = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", blob, re.S)
    if m:
        blob = m.group(1)
    else:
        start, end = blob.find("{"), blob.rfind("}")
        if start >= 0 and end > start:
            blob = blob[start:end + 1]
    try:
        return json.loads(blob)
    except Exception:
        return None


def validate_facts(
    payload: Dict[str, Any],
    user_input: str,
    *,
    min_confidence: float = MIN_CONFIDENCE,
    max_facts: int = MAX_FACTS_PER_TURN,
) -> List[Dict[str, Any]]:
    """把模型输出过一遍闸门。返回**通过校验**的事实列表。

    这里是整个模块的核心：**证据必须能在原话里找到**。
    """
    facts = (payload or {}).get("facts")
    if not isinstance(facts, list):
        return []

    haystack = _norm(user_input)
    accepted: List[Dict[str, Any]] = []
    seen = set()

    for raw in facts:
        if not isinstance(raw, dict):
            continue

        ftype = str(raw.get("type") or "").strip().lower()
        content = str(raw.get("content") or "").strip()
        evidence = str(raw.get("evidence") or "").strip()

        # --- 类型白名单 ---
        if ftype not in ALLOWED_TYPES:
            continue
        # --- 内容长度 ---
        if not (MIN_CONTENT_CHARS <= len(content) <= MAX_CONTENT_CHARS):
            continue
        # --- **证据校验（反幻觉的核心）** ---
        if not evidence or _norm(evidence) not in haystack:
            continue
        # --- 置信度 ---
        try:
            confidence = float(raw.get("confidence", 0.0))
        except Exception:
            continue
        if confidence < min_confidence:
            continue
        try:
            importance = float(raw.get("importance", 0.5))
        except Exception:
            importance = 0.5
        importance = min(max(importance, 0.0), 1.0)

        # --- 去重（同类型同内容） ---
        key = (ftype, _norm(content))
        if key in seen:
            continue
        seen.add(key)

        accepted.append({
            "type": ftype,
            "content": content,
            "evidence": evidence,
            "confidence": round(confidence, 3),
            "importance": round(importance, 3),
        })
        if len(accepted) >= max_facts:
            break

    return accepted


async def extract_with_llm(
    user_input: str,
    *,
    llm=None,
    min_confidence: float = MIN_CONFIDENCE,
    max_facts: int = MAX_FACTS_PER_TURN,
) -> List[Any]:
    """用 LLM 抽取画像候选。

    返回 `MemoryCandidate` 列表（与旧规则版同构）。
    **任何异常都返回空列表** —— 记忆写入失败不能影响主流程。
    """
    logger = get_logger()
    text = (user_input or "").strip()
    # 太短的话基本没有信息量，省一次调用
    if len(text) < 6:
        return []

    try:
        if llm is None:
            from infra.llm import get_llm_client

            llm = get_llm_client()
        raw = await llm.chat_with_retry(
            [{"role": "user", "content": PROFILE_PROMPT.format(user_input=text)}],
            temperature=0.0,
            max_retries=2,
        )
    except Exception as exc:
        logger.warning("ProfileLLM", f"画像抽取调用失败(忽略): {exc}")
        return []

    payload = _extract_json(raw or "")
    if not payload:
        return []

    facts = validate_facts(
        payload, text, min_confidence=min_confidence, max_facts=max_facts
    )

    # 惰性导入，避免与 memory_writer 循环依赖
    from core.memory_writer import MemoryCandidate

    out: List[Any] = []
    for f in facts:
        out.append(
            MemoryCandidate(
                type=f["type"],
                content=f["content"],
                confidence=f["confidence"],
                importance=f["importance"],
                scope="user",
                normalized_key=f"{f['type']}:{_norm(f['content'])[:60]}",
                rule="llm",
                evidence=f["evidence"],
            )
        )
    return out
