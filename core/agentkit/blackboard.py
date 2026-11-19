"""黑板 —— 上游产出的共享区。

## 为什么是"按键拉取"而不是"广播"

子智能体之间要交换信息（上游查到的条款要给下游用），但**广播会失控**：
每个子智能体都看见所有东西 → 上下文污染、token 爆炸、无法复盘。

所以定成**黑板 + 键**：

- 上游把结论写进**具名槽位**（`outputs` 里声明写哪些键）；
- 下游按**自己的 `inputs`** 拉取需要的键 —— 看不到别人的。

这是"受控协作"（docs/18 §5.3）的底座：依赖关系由主智能体的编排图决定，
数据流由黑板的键决定，两者都不是"随便聊"。

## 值有上限

黑板上的值**必须是有界的** —— 一个 agent 塞 1MB 进来，下游就炸了。
超限截断并标注（下游能看出"这是截断的"）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: 单个值的最大字符数
MAX_VALUE_CHARS = 4000


@dataclass
class BlackboardEntry:
    """黑板上的一条内容。"""

    key: str
    value: Any = ""
    producer: str = ""
    schema_hint: str = ""
    created_at: float = field(default_factory=time.time)
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "producer": self.producer,
            "schema_hint": self.schema_hint,
            "created_at": self.created_at,
            "truncated": self.truncated,
        }


class Blackboard:
    """一轮任务内的共享区（进程内）。

    **不是数据库** —— 重启后不保留（跨轮次持久化属于 F6）。
    """

    def __init__(self, *, max_value_chars: int = MAX_VALUE_CHARS):
        self.max_value_chars = max_value_chars
        self._entries: Dict[str, BlackboardEntry] = {}

    def write(
        self,
        key: str,
        value: Any,
        *,
        producer: str = "",
        schema_hint: str = "",
    ) -> BlackboardEntry:
        """写入一个槽位（同名覆盖）。"""
        key = (key or "").strip()
        if not key:
            raise ValueError("黑板键不能为空")

        truncated = False
        if isinstance(value, str) and len(value) > self.max_value_chars:
            value = value[: self.max_value_chars] + "…（已截断）"
            truncated = True
        elif not isinstance(value, str):
            try:
                import json

                text = json.dumps(value, ensure_ascii=False)
            except Exception:
                text = str(value)
            if len(text) > self.max_value_chars:
                value = text[: self.max_value_chars] + "…（已截断）"
                truncated = True

        entry = BlackboardEntry(
            key=key,
            value=value,
            producer=producer,
            schema_hint=schema_hint,
            truncated=truncated,
        )
        self._entries[key] = entry
        return entry

    def read(self, key: str, default: Any = None) -> Any:
        entry = self._entries.get(key)
        return entry.value if entry is not None else default

    def read_many(self, keys: List[str]) -> Dict[str, Any]:
        """按键批量读取（下游只拿自己声明要的）。"""
        out: Dict[str, Any] = {}
        for key in keys or []:
            entry = self._entries.get(key)
            if entry is not None:
                out[key] = entry.value
        return out

    def get(self, key: str) -> Optional[BlackboardEntry]:
        return self._entries.get(key)

    def keys(self) -> List[str]:
        return list(self._entries.keys())

    def snapshot(self) -> Dict[str, Any]:
        return {k: v.to_dict() for k, v in self._entries.items()}

    def clear(self) -> None:
        self._entries.clear()
