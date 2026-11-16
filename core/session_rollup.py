"""会话滚动摘要 —— 把早期对话压成摘要,给 LLM 当压缩视图。

**原文不删除**:``session_messages`` 里的消息永远保留,摘要只是额外的
一条"索引视图"。这样既满足"会话内容都要保留",又不会让 token 无限膨胀。

触发时机:会话内"尚未被摘要覆盖"的消息数超过阈值时压一次。
覆盖范围记录在 ``context.metadata['rollup_index']``,
并写入 ``session_summaries`` 表(``cover_until_seq``)。
"""
from __future__ import annotations

from typing import Any, List, Optional

from core.context import Context
from core.session_config import (
    session_rollup_keep_recent,
    session_rollup_threshold,
)


_SUMMARY_PROMPT = """请把下面的对话片段压缩成一段简洁的中文摘要,用于后续对话时回顾早期内容。

要求:
1. 只保留对后续对话有用的信息:讨论过的主题、已达成的结论、用户的明确要求与偏好。
2. 保留关键的具体名词(项目名、方案名、数字、结论),不要泛化。
3. 不要输出客套话,直接给摘要正文。
4. 控制在 300 字以内。

对话片段:
{transcript}

摘要:"""


def _build_transcript(messages: List[Any], limit_each: int = 300) -> str:
    lines = []
    for msg in messages:
        role = getattr(msg, "role", "")
        content = (getattr(msg, "content", "") or "").strip()
        if not content:
            continue
        if len(content) > limit_each:
            content = content[:limit_each] + "..."
        who = "用户" if role == "user" else ("助手" if role == "assistant" else role)
        lines.append(f"- {who}: {content}")
    return "\n".join(lines)


def _uncovered(messages: List[Any], covered_seq: int) -> List[Any]:
    """尚未被摘要覆盖的消息。

    ``covered_seq`` 是**会话绝对序号**边界(<= 它表示已被摘要覆盖)。
    内存里的窗口可能只装了一部分(恢复会话时),所以不能拿列表下标当序号用。

    ``seq <= 0`` 表示这条消息没落库(落库失败),一律当作未覆盖。
    """
    out = []
    for msg in messages:
        seq = getattr(msg, "seq", 0) or 0
        if seq <= 0 or seq > covered_seq:
            out.append(msg)
    return out


async def summarize_messages(messages: List[Any]) -> str:
    """用 LLM 把一段消息压成摘要。失败时退化为截断拼接。"""
    transcript = _build_transcript(messages)
    if not transcript:
        return ""
    try:
        from infra.llm import get_llm_client

        client = get_llm_client()
        answer = await client.chat(
            [
                {
                    "role": "system",
                    "content": "你是会话摘要助手,只输出摘要正文,不输出解释。",
                },
                {
                    "role": "user",
                    "content": _SUMMARY_PROMPT.format(transcript=transcript),
                },
            ],
            temperature=0.2,
        )
        answer = (answer or "").strip()
        if answer:
            return answer
    except Exception:
        pass
    # 退化:直接截断,至少不丢内容
    return transcript[:600]


async def maybe_rollup(
    context: Context,
    store,
    *,
    session_id: str,
    user_id: str,
    logger=None,
    threshold: Optional[int] = None,
    keep_recent: Optional[int] = None,
) -> Optional[str]:
    """必要时把早期内容压成滚动摘要。

    Returns:
        新生成的摘要文本;不需要滚动时返回 None
    """
    if store is None:
        return None
    threshold = threshold if threshold is not None else session_rollup_threshold()
    keep_recent = (
        keep_recent if keep_recent is not None else session_rollup_keep_recent()
    )

    # 已被摘要覆盖的绝对序号边界
    covered_seq = int(context.metadata.get("rollup_cover_seq", 0))
    uncovered = _uncovered(context.messages, covered_seq)
    if len(uncovered) <= threshold:
        return None

    # 留最近 keep_recent 条原文不摘要
    target = uncovered[:-keep_recent] if keep_recent > 0 else uncovered
    if not target:
        return None

    summary = await summarize_messages(target)
    if not summary:
        return None

    last_seq = getattr(target[-1], "seq", 0) or 0
    if last_seq <= 0:
        # 没有绝对序号就没法记账 —— 宁可不做,也不要记错
        return None

    try:
        store.save_summary(
            session_id=session_id,
            user_id=user_id,
            cover_until_seq=last_seq,
            summary=summary,
        )
    except Exception as exc:
        if logger:
            logger.warning("SessionRollup", f"摘要落库失败: {exc}")
        return None

    # 摘要视图:已有摘要 + 新摘要(新的是更近的)
    previous = context.get_summary()
    merged = f"{previous}\n{summary}".strip() if previous else summary
    context.set_summary(merged)
    context.metadata["rollup_cover_seq"] = last_seq

    if logger:
        logger.info(
            "SessionRollup",
            f"已摘要 {len(target)} 条(覆盖到 seq={last_seq}),"
            f"当前未覆盖 {len(uncovered) - len(target)} 条",
        )
    return summary
