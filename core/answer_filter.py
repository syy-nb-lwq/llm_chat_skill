"""回答流过滤器 —— 挡住模型幻觉出来的工具调用标记。

## 为什么需要

模型在生成"给用户看的回答"时，偶尔会幻觉出 `<tool_call>...</tool_call>`、
`<function>...</function>` 这类标签，把本该走结构化工具调用的东西当成正文写出来。
这些标签**不是本系统的输出格式**（规划走 JSON schema，不走文本标记），
纯粹是模型从训练数据里学来的坏习惯。

系统的回答是**逐 token 流式推给前端**的（`_stream_answer` → `EV_MESSAGE_DELTA`），
一旦幻觉标记流出去，用户就看到一堆看不懂的标签 + 半截代码，以为系统坏了。
实机踩过（2026-11-20 报销表加行那次）。

prompt 里已明确禁止（见 OrchestratorAgent._BASE_PROMPT），这里是第二道防线：
即使模型违反了，也在流式输出时把它过滤掉。

## 为什么必须是"有状态跨 token"的

`<tool_call>` 会被切成多个 token（如 `<tool` + `_call>`）。如果逐块独立判断，
`<tool` 已经被推出去了，就撤不回来了。所以过滤器要：

- 遇到"可能是标记开头的前缀"先 hold 住，不立即输出
- 确认是标记 → 进入丢弃态；确认不是 → 把 hold 的内容原样放出

## 用法

    f = ToolCallFilter()
    for token in stream:
        for safe in f.feed(token):
            emit(safe)
    for safe in f.flush():       # 收尾：放出还在 hold 的内容
        emit(safe)
"""
from __future__ import annotations

from typing import Iterator, List

#: 要过滤的成对标记（开标签, 闭标签）。大小写敏感 —— 模型一般照抄小写。
_PAIRED_TAGS = (
    ("<tool_call>", "</tool_call>"),
    ("<function>", "</function>"),
    ("<tool>", "</tool>"),
    ("<function_call>", "</function_call>"),
)

#: 所有开标签，用于判断"当前 pending 是不是某个开标签的前缀"
_OPENERS = tuple(op for op, _ in _PAIRED_TAGS)
#: 最长开标签长度 —— pending 超过它就不可能只是个前缀
_MAX_OPENER_LEN = max(len(op) for op in _OPENERS)


class ToolCallFilter:
    """跨 token 识别并丢弃幻觉工具调用标记的有状态过滤器。"""

    def __init__(self) -> None:
        self._pending = ""        # hold 住的、可能是标记前缀的文本
        self._discarding = ""     # 当前正在丢弃的标记的闭标签；空 = 不在丢弃态

    def feed(self, token: str) -> Iterator[str]:
        """吃进一个 token，产出 0~N 个可以安全推送的片段。"""
        if not token:
            return
        buf = self._pending + token
        self._pending = ""
        i = 0
        while i < len(buf):
            if self._discarding:
                close = self._discarding
                # 丢弃态：找闭标签
                end = buf.find(close, i)
                if end >= 0:
                    i = end + len(close)
                    self._discarding = ""
                    continue
                # 没找到完整闭标签：buf 尾部可能是闭标签的"部分前缀"，
                # 必须 hold 住（否则跨 token 的 </tool_call> 永远凑不齐，
                # 闭标签之后的正常文本会被一起吞掉）。
                tail = buf[i:]
                hold = self._longest_close_prefix_suffix(tail, close)
                if hold:
                    self._pending = tail[-hold:]
                    tail = tail[:-hold]
                # tail 里不含完整闭标签、也不含其前缀 → 全部丢弃
                return

            # 正常态：看当前位置是不是某个开标签的开头
            matched = None
            for op, cl in _PAIRED_TAGS:
                if buf.startswith(op, i):
                    matched = (op, cl)
                    break
            if matched:
                op, cl = matched
                i += len(op)
                self._discarding = cl
                continue

            # 检查 buf[i:] 是不是某个开标签的"前缀"（还没凑全，先 hold）
            tail = buf[i:]
            if self._is_opener_prefix(tail):
                self._pending = tail
                return

            # 普通字符：输出
            yield tail[0]
            i += 1

    def flush(self) -> Iterator[str]:
        """流结束：把还在 hold 的文本放出去。

        - 正常态 hold 的是"疑似开标签前缀"，它不是标记 → 放行
        - 丢弃态 hold 的是"闭标签残片"，它属于被丢弃的内容 → 不放行
        """
        if self._pending and not self._discarding:
            yield self._pending
        self._pending = ""

    @staticmethod
    def _is_opener_prefix(text: str) -> bool:
        """text 是否是某个开标签的非空真前缀（还没有凑完整）。"""
        if not text or len(text) > _MAX_OPENER_LEN:
            return False
        return any(op.startswith(text) and text != op for op in _OPENERS)

    @staticmethod
    def _longest_close_prefix_suffix(tail: str, close: str) -> int:
        """tail 末尾最多有多少字符是 close 的真前缀。

        用于丢弃态：闭标签被切跨 token 时，把这部分 hold 住，
        等后续 token 补全；不把它当垃圾丢掉。
        """
        max_n = min(len(tail), len(close) - 1)
        for n in range(max_n, 0, -1):
            if close.startswith(tail[-n:]):
                return n
        return 0


def filter_answer(text: str) -> str:
    """一次性清洗整段回答（用于已攒好的完整文本，如落库/兜底）。"""
    f = ToolCallFilter()
    out: List[str] = list(f.feed(text))
    out.extend(f.flush())
    return "".join(out)
