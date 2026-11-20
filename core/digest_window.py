"""待归纳轮次的累积窗口。

## 它解决什么

「综合来看才能沉淀」这句话落到工程上就是：**别在一轮结束时下结论，先攒着**。

`Agent` 每轮结束时把这一轮丢进来，窗口没满就只存着（零 LLM 调用）；
满了才把整段交出去做一次归纳。这个"攒"的动作本身，就是"综合"的前提 ——
没有它，再好的归纳提示词也只能看到一句话。

## 两个刻意的设计

**① 满窗后保留尾部若干轮，不清空。**
下一批带上上一批的最后两轮，跨批的"反复出现"才不会在边界上断掉。
代价是同一轮可能被归纳两次，由写入侧的去重兜掉（`_write_one` 已有）。

**② 太短的轮次不占位。**
「继续」「好」这类不携带任何画像信息，如果让它们占满 6 个位置，
窗口就会被一串"继续"撑满，然后白跑一次 LLM 归纳。
它们既不占位也不入库 —— 它们本来就不该产生任何记忆。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: 一轮用户输入短于这个长度，视为无信息量（「好」「嗯」「继续」）
MIN_TURN_CHARS = 3


@dataclass
class PendingTurn:
    """窗口里的一轮。`index` 只在窗口内连续，用于证据对账。"""
    index: int
    user: str
    assistant: str = ""


@dataclass
class DigestWindow:
    """按会话累积待归纳的轮次。

    Args:
        size: 攒够几轮触发一次归纳。
        keep: 触发后保留尾部几轮，作为下一批的上下文。
    """
    size: int = 6
    keep: int = 2
    _turns: List[PendingTurn] = field(default_factory=list)
    _next: int = 1

    def add(self, user: str, assistant: str = "") -> Optional[List[Dict[str, object]]]:
        """记一轮。

        返回 `None` 表示还没攒够；返回列表表示"这一批可以归纳了"。
        返回的是**渲染好的 turn 字典**，直接能喂给 `profile_digest`。
        """
        text = (user or "").strip()
        if len(text) < MIN_TURN_CHARS:
            # 无信息量的轮次：不占位、不推进序号 —— 但它不该打断已有累积
            return None

        self._turns.append(
            PendingTurn(
                index=self._next,
                user=text,
                assistant=(assistant or "").strip(),
            )
        )
        self._next += 1

        if len(self._turns) < self.size:
            return None
        return self._take()

    def flush(self) -> Optional[List[Dict[str, object]]]:
        """把剩下的轮次全部交出去（reset / 切换会话时调用）。

        窗口没满时也归纳，因为**这些轮次以后不会再有机会**了 ——
        用户已经清空上下文，它们不会再参与任何一次归纳。

        注意这里**不能**复用 `_take()`：`_take` 会保留尾部几轮做下一批的
        上下文，而 flush 之后上下文已经清了，留着只会让下次 reset 把同一批
        再归纳一遍（重复入库，白白多一次 LLM 调用）。
        """
        if not self._turns:
            return None
        batch = [
            {"index": t.index, "user": t.user, "assistant": t.assistant}
            for t in self._turns
        ]
        self._turns = []
        return batch

    def pending(self) -> int:
        return len(self._turns)

    def _take(self) -> List[Dict[str, object]]:
        batch = [
            {"index": t.index, "user": t.user, "assistant": t.assistant}
            for t in self._turns
        ]
        # 保留尾部作为下一批的上下文；keep 为 0 就整批清空。
        # 注意 _next 不回退 —— 序号必须单调，否则两批之间会重号。
        self._turns = self._turns[-self.keep:] if self.keep > 0 else []
        return batch


#: 全局窗口表（后端一个进程一张表，按 session_id 分）。
#:
#: 为什么不放在 `Agent` 实例上：Agent 会随 Session 回收（TTL）被销毁，
#: 而"已经攒了几轮但还没满窗"这件事应该跨实例存活 —— 用户切走再切回来，
#: 之前那几轮不该白攒。
_WINDOWS: Dict[str, DigestWindow] = {}


def window_for(
    session_id: str, *, size: Optional[int] = None, keep: int = 2
) -> DigestWindow:
    """取（或创建）某个会话的窗口。

    `size` 不传就走配置项（`session_config.profile_digest_window`），
    这样"多久沉淀一次"是个能调的量，而不是散落在代码里的魔数。
    """
    if size is None:
        from core.session_config import profile_digest_window

        size = profile_digest_window()

    key = session_id or ""
    win = _WINDOWS.get(key)
    if win is None:
        win = DigestWindow(size=size, keep=keep)
        _WINDOWS[key] = win
    return win


def reset_windows() -> None:
    """清空窗口表（测试用）。"""
    _WINDOWS.clear()
