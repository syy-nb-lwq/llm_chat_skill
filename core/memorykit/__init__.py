"""记忆三层 —— 一份契约，三个门面。

```
core/memorykit/
├── working.py     工作记忆：这一轮做到哪了（进程内，轮末即弃）
├── short_term.py  短期记忆：这次会话说过什么（sessions.db，永久）
└── long_term.py   长期记忆：这个人是谁 / 什么做法长期有效（画像+经验+台账）
```

## 为什么要有这个包

以前"记忆"是 5 个平级模块（`memory.py` / `memory_db.py` / `semantic_memory.py` /
`session_memory.py` / `skill_quality.py`），调用方得自己判断该用哪个；同一个概念
（比如"失败记录"）在两处存储里各有一份。加一个功能就要先考古。

现在收成三层：**同一个问题只在一层里回答**。调用方只认这三张脸，不认底层 store。

## 职责 / 谁来调用（改这里之前先改这张表）

| 层 | 回答的问题 | 生命周期 | 载体 | 谁写 | 谁读 |
|---|---|---|---|---|---|
| 工作记忆 | 这一轮做到哪了？ | 单轮，结束即弃 | 进程内对象 | Agent 主链、SubAgentPool | 规划（replan）、回答生成、goal loop |
| 短期记忆 | 这次会话说过什么？ | 会话，跨重启 | `sessions.db` | **只有**会话层（`Context.sink`） | 会话恢复、会话内检索、`/api/sessions` |
| 长期记忆 | 这个人是谁？ | 永久 | `semantic_memory.db` + `memory/*` | **只有**后台写入器（MemoryWriter / Critic / SkillQuality） | 轮首装配一次（画像）、规划前 hints |

## 唯一允许的流向（单向沉淀）

```
工作记忆 ──轮末──> 短期记忆（消息原文）──抽取──> 长期记忆（画像 / 经验）
```

反向是**禁止**的：

- 长期记忆不得写回短期记忆（画像不是"说过的话"）；
- 工作记忆不得直接写长期记忆（一次工具输出不能改画像）；
- 短期记忆不得被当作长期知识注入（会话里的一句话不是这个人的属性）。

## 用法

```python
from core.memorykit import get_memory

mem = get_memory()
wm = mem.begin_turn(execution_id=self.execution_id, session_id=self.session_id)
wm.set_tool_results(results)          # 工作记忆
mem.short_term.append_message(...)    # 短期记忆
mem.long_term.profile_block()         # 长期记忆
mem.end_turn(wm, failed=...)          # 清空（失败时先落盘取证）
```
"""
from __future__ import annotations

from typing import Optional

from core.memorykit.long_term import LongTermMemory
from core.memorykit.short_term import ShortTermMemory
from core.memorykit.working import (
    MAX_SLOTS,
    MAX_VALUE_CHARS,
    SLOT_FAILURE,
    SLOT_FINDINGS,
    SLOT_PLAN,
    SLOT_TOOL_RESULTS,
    WorkingEntry,
    WorkingMemory,
    clear_current_working,
    get_current_working,
    set_current_working,
)

__all__ = [
    "MemoryHub",
    "get_memory",
    "reset_memory",
    "WorkingMemory",
    "WorkingEntry",
    "ShortTermMemory",
    "LongTermMemory",
    "SLOT_PLAN",
    "SLOT_TOOL_RESULTS",
    "SLOT_FINDINGS",
    "SLOT_FAILURE",
    "MAX_VALUE_CHARS",
    "MAX_SLOTS",
    "get_current_working",
    "set_current_working",
    "clear_current_working",
]


class MemoryHub:
    """三层的统一入口。"""

    def __init__(self, turns_dir=None):
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory()
        #: 失败取证目录（默认 `memory/turns/`）
        self.turns_dir = turns_dir

    # ===== 工作记忆：一轮一个，必须成对调用 =====

    def begin_turn(self, *, execution_id: str = "", session_id: str = "") -> WorkingMemory:
        """开一轮工作记忆，并注册为"当前轮"（工具/子智能体可经 contextvar 取到）。"""
        wm = WorkingMemory(
            execution_id=execution_id,
            session_id=session_id,
            dump_dir=self.turns_dir,
        )
        set_current_working(wm)
        return wm

    def end_turn(
        self,
        wm: Optional[WorkingMemory],
        *,
        failed: bool = False,
        reason: str = "",
    ) -> Optional[str]:
        """收尾：失败先取证落盘，然后清空。

        返回落盘路径（只在失败且落盘成功时非空）。
        """
        dumped: Optional[str] = None
        if wm is not None and failed:
            path = wm.dump(reason=reason or "turn failed")
            if path is not None:
                dumped = str(path)
        if wm is not None:
            wm.clear()
        clear_current_working()
        return dumped


_hub: Optional[MemoryHub] = None


def get_memory() -> MemoryHub:
    """全局单例（与项目其它 store 保持一致的单例风格）。"""
    global _hub
    if _hub is None:
        _hub = MemoryHub()
    return _hub


def reset_memory() -> None:
    """重置（测试用）。"""
    global _hub
    _hub = None
    clear_current_working()
