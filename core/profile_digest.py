"""会话级归纳 —— 从「一段对话」沉淀画像，而不是从「一句话」。

## 为什么要换

用户原话：「记忆的总结不要是单独记住一句话，而是说要综合来看才能沉淀。」

旧路径（`profile_llm.extract_with_llm`）每轮调一次，输入是「当前这一句 +
前 6 轮（仅供消解指代）」，而证据被要求**必须逐字出现在当前这一句里**。
这个设计有两个后果：

1. **一句话就足以生成一条长期结论**。用户说「这次先这样」，只要它不含
   临时词，就能变成一条偏好；上下文再努力也只能帮它猜对主语，不能否掉它。
2. **多轮才能看出来的模式，反而沉淀不下来**。用户三轮都在要求"短一点"、
   两次都在纠正同一个错 —— 没有任何单句能支撑这条结论，于是永远不会入库。

所以判据要从「这句话是否像长期结论」换成「**这条结论有多少轮撑着**」。

## 怎么保证（结构性校验，不靠提示词求它）

模型为每条结论给出 `evidence: [{turn, quote}, ...]`，然后：

- `quote` 必须在**它所声明的那一轮**里逐字出现。不是在整个对话块里找 ——
  否则模型可以拿第 5 轮的原话冒充第 1 轮，跨轮约束就形同虚设。
- `kind="inferred"`（从多次行为归纳的）**必须 ≥2 个不同轮次**的证据。
  只有一条就等于在编，该条整条丢弃。
- `kind="stated"`（用户自己说的"我是做…的""以后都别…"）允许单轮，
  因为这类结论的合法性本来就来自那一句话，不需要多轮佐证。
- content 里出现「这次 / 刚才 / 这一版」等临时词，直接丢弃 —— 那是在描述状态，
  不是描述这个人。

这四道闸合起来，才让"综合来看"成为一个**代码事实**而不是提示词里的请求。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from infra.logger import get_logger

#: 允许的画像类型（与 `memory_writer.PROFILE_TYPES` 一致）
ALLOWED_TYPES = {"preference", "habit", "method", "constraint", "fact"}

#: 结论的来路
#: - stated：用户自己明确说出来的，单轮证据即可成立
#: - inferred：从多轮行为/反复表态里归纳出来的，必须跨轮
ALLOWED_KINDS = {"stated", "inferred"}

#: 归纳类结论至少要来自几个**不同轮次**
MIN_TURNS_FOR_INFERRED = 2

#: 低于这个置信度不收
MIN_CONFIDENCE = 0.6
#: 一次归纳最多收几条（覆盖的是多轮，比单轮的上限宽一点）
MAX_FACTS_PER_DIGEST = 5
#: 单条内容长度范围
MIN_CONTENT_CHARS = 4
MAX_CONTENT_CHARS = 120
#: `applies_to` 是名字，不是句子
MAX_APPLIES_TO_CHARS = 40

#: 带这些词的结论讲的是「这一次」，不是「这个人」。
#:
#: 单轮抽取时它们也会漏网（"这次用 5 秒"），但那只是偶发；归纳时更要紧 ——
#: 模型看到一整段对话，很容易把"这几轮一直在调参数"总结成"用户偏好 5 秒"。
_EPHEMERAL_RE = re.compile(
    r"这次|本次|刚才|刚刚|这一版|这个版本|这版|暂时|先这样|当前这一轮|眼下"
)


DIGEST_PROMPT = """你在读用户和助手的一段**连续对话**，任务是综合归纳这个人的长期特征。

## 最重要的原则：不要从一句话里下结论

单句话经常是临时的（"这次先这样"、"改成 1280x720"、"再试一次"），
它们不代表这个人。只有下面两种情况才值得写进长期记忆：

1. **用户自己说明这是长期的**：「我以后都…」「我是做…的」「记住…」
2. **同一件事在多轮里反复出现**：三轮都在要求"短一点"、两次都在纠正同一个错、
   每一轮都在补充同一个业务背景

第二种才是"综合来看沉淀下来的"。请优先找这种**模式**，不要挑金句。

## 每条结论必须交代它是怎么来的

- `kind="stated"`：用户自己说出来的。证据 ≥1 条。
- `kind="inferred"`：你从多轮里归纳的。证据**必须 ≥2 条，且来自不同轮次**。
  凑不出两条不同轮次的证据，说明这是你的推测，**这条不要输出**。

## 证据规则

每条证据写成 `{{"turn": 轮次号, "quote": "该轮用户原话里逐字出现的一段"}}`。

- quote 必须取自**该轮用户**说的话，不能是助手的，不能改写、不能概括
- 不要把后面轮次的话标成前面的轮次号
- 助手的回答只能用来理解上下文，**不能当证据**

## 不要记

- 一次性任务指令、临时参数、对当前结果的评论
- 助手说过的话、你自己的推测
- 只在单轮出现、且用户没有明说"以后都这样"的内容
- 带「这次 / 刚才 / 这一版」的临时状态

## 关于 applies_to（限定范围）

只有结论**只对某个具体对象成立**时才填：

- ✓ 「在 minimax 上优先用国内站」→ applies_to: minimax
  （换个平台，这个结论就不成立了）
- ✗ 「用户叫老王，做电商，主要跑拼多多」→ applies_to **留空**
  （身份、行业、姓名永远是全局的。句子里提到某个平台，只是说明他做的事
   包含那一块 —— 不要把「用户叫老王」限定成「只在聊拼多多时成立」）
- ✗ 「用户偏好极简回答」→ 留空（对任何话题都成立）

拿不准时：问自己「换一个话题，这句话还成立吗」。成立就留空。

## 输出

只输出 JSON：

{{"facts": [
  {{
    "type": "preference|habit|method|constraint|fact",
    "kind": "stated|inferred",
    "content": "第三人称短句（不超过 60 字）；有前提就把前提写进句子里",
    "applies_to": "只适用于某个产品/平台/项目时填它的名字，否则空字符串",
    "evidence": [{{"turn": 1, "quote": "原话"}}, {{"turn": 3, "quote": "原话"}}],
    "confidence": 0.0~1.0,
    "importance": 0.0~1.0
  }}
]}}

没有值得记的就输出 `{{"facts": []}}` —— 这在短对话里是最常见、最正确的结果。

## 对话

{transcript}
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
    except ValueError:
        return None


def build_transcript(turns: Sequence[Dict[str, Any]]) -> str:
    """把一段对话渲染成带轮次号的文本。

    轮次号必须显式给出来 —— 证据校验要按号对账，模型没有号就只能瞎标。
    助手内容也带上，但只用于理解上下文（提示词里已写明不能当证据）。
    """
    lines: List[str] = []
    for turn in turns:
        idx = turn.get("index")
        user = (turn.get("user") or "").strip()
        assistant = (turn.get("assistant") or "").strip()
        lines.append(f"[第 {idx} 轮]")
        lines.append(f"用户：{user}")
        if assistant:
            # 回答太长会把上下文预算吃光，取前 200 字足够理解"用户在回应什么"
            lines.append(f"助手：{assistant[:200]}")
    return "\n".join(lines)


def validate_digest(
    payload: Dict[str, Any],
    turns: Sequence[Dict[str, Any]],
    *,
    min_confidence: float = MIN_CONFIDENCE,
    max_facts: int = MAX_FACTS_PER_DIGEST,
) -> List[Dict[str, Any]]:
    """把模型输出过一遍闸门，返回**通过校验**的结论。

    这是整个模块的核心：**结论的合法性来自它有多少轮证据**。
    """
    facts = (payload or {}).get("facts")
    if not isinstance(facts, list):
        return []

    #: 轮次号 → 该轮用户原话（去掉空白）
    user_by_turn: Dict[int, str] = {}
    #: 全段对话（用户 + 助手），只用于校验 applies_to 是不是真实出现过的名字
    whole = ""
    for turn in turns:
        try:
            idx = int(turn.get("index"))
        except (TypeError, ValueError):
            continue
        user_norm = _norm(turn.get("user") or "")
        user_by_turn[idx] = user_norm
        whole += user_norm + _norm(turn.get("assistant") or "")

    accepted: List[Dict[str, Any]] = []
    seen = set()

    for raw in facts:
        if not isinstance(raw, dict):
            continue

        ftype = str(raw.get("type") or "").strip().lower()
        # 缺省按 inferred 处理：宁可少记，也不让漏标 kind 的结论走宽松通道
        kind = str(raw.get("kind") or "inferred").strip().lower()
        content = str(raw.get("content") or "").strip()
        applies_to = str(raw.get("applies_to") or "").strip()

        if ftype not in ALLOWED_TYPES:
            continue
        if kind not in ALLOWED_KINDS:
            continue
        if not (MIN_CONTENT_CHARS <= len(content) <= MAX_CONTENT_CHARS):
            continue
        # --- 临时状态不许当画像 ---
        if _EPHEMERAL_RE.search(content):
            continue

        # --- 证据校验 ---
        evidence = raw.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            continue

        turns_used: List[int] = []
        quotes: List[str] = []
        bad = False
        for item in evidence:
            if not isinstance(item, dict):
                bad = True
                break
            try:
                idx = int(item.get("turn"))
            except (TypeError, ValueError):
                bad = True
                break
            quote = str(item.get("quote") or "").strip()
            haystack = user_by_turn.get(idx)
            # 轮次必须真实存在，且 quote 必须逐字出现在**那一轮用户的**话里
            if haystack is None or not quote or _norm(quote) not in haystack:
                bad = True
                break
            turns_used.append(idx)
            quotes.append(quote)
        if bad:
            continue

        # --- 核心：归纳类结论必须跨轮 ---
        distinct = set(turns_used)
        if kind == "inferred" and len(distinct) < MIN_TURNS_FOR_INFERRED:
            continue

        # --- 限定名必须真的在对话里出现过 ---
        if applies_to:
            if len(applies_to) > MAX_APPLIES_TO_CHARS:
                continue
            if _norm(applies_to) not in whole:
                continue

        try:
            confidence = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        try:
            importance = float(raw.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        importance = min(max(importance, 0.0), 1.0)

        key = (ftype, _norm(content))
        if key in seen:
            continue
        seen.add(key)

        accepted.append({
            "type": ftype,
            "kind": kind,
            "content": content,
            "applies_to": applies_to,
            "evidence": quotes,
            "evidence_turns": sorted(distinct),
            "confidence": round(confidence, 3),
            "importance": round(importance, 3),
        })
        if len(accepted) >= max_facts:
            break

    return accepted


async def digest_with_llm(
    turns: Sequence[Dict[str, Any]],
    *,
    llm=None,
    min_confidence: float = MIN_CONFIDENCE,
    max_facts: int = MAX_FACTS_PER_DIGEST,
) -> List[Any]:
    """把一段对话整体归纳成画像候选。

    返回 `MemoryCandidate` 列表。**任何异常都返回空列表** ——
    归纳失败只意味着"这一段没沉淀下来"，不能影响主流程。
    """
    logger = get_logger()
    if not turns:
        return []

    # 全段用户话太短说明没有信息量，省一次调用。
    #
    # 阈值刻意设得很低：这句「我叫老王，自己是做电商的」只有 19 字，
    # 却是一条完整的身份陈述 —— 用 20 字卡会把人间蒸发。
    # 「继续」「好」这类早已被 `DigestWindow` 挡在窗口外，这里只是兜底。
    total = sum(len((t.get("user") or "").strip()) for t in turns)
    if total < 10:
        return []

    try:
        if llm is None:
            from infra.llm import get_llm_client

            llm = get_llm_client()
        raw = await llm.chat_with_retry(
            [{
                "role": "user",
                "content": DIGEST_PROMPT.format(transcript=build_transcript(turns)),
            }],
            temperature=0.0,
            max_retries=2,
        )
    except Exception as exc:
        logger.warning("ProfileDigest", f"会话归纳调用失败(忽略): {exc}")
        return []

    payload = _extract_json(raw or "")
    if not payload:
        return []

    facts = validate_digest(
        payload, turns, min_confidence=min_confidence, max_facts=max_facts
    )

    # 惰性导入，避免与 memory_writer 循环依赖
    from core.memory_writer import MemoryCandidate

    out: List[Any] = []
    for f in facts:
        applies_to = f.get("applies_to", "")
        # key 带限定名：同一结论挂在两个不同平台上是两条独立记忆
        key = f"{f['type']}:{applies_to}|{_norm(f['content'])[:60]}"
        out.append(
            MemoryCandidate(
                type=f["type"],
                content=f["content"],
                confidence=f["confidence"],
                importance=f["importance"],
                scope="user",
                normalized_key=key,
                applies_to=applies_to,
                rule="digest",
                evidence=" / ".join(f["evidence"]),
                evidence_turns=f["evidence_turns"],
            )
        )
    return out
