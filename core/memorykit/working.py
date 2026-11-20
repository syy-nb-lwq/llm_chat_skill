"""工作记忆（Working Memory）—— **一轮任务的工作台**。

## 它回答的问题

> 这一轮现在做到哪了？中间结果是什么？

## 它不是什么

| 容易混淆的 | 为什么不是 |
|---|---|
| 短期记忆 | 短期是"用户和助手说过的话"，跨轮保留；工作记忆一轮结束就丢 |
| 长期记忆 | 长期是"这个人是谁 / 什么做法长期有效"，永久保留 |
| `Context` | Context 是**给 LLM 看的对话视图**（消息 + 摘要）；工作记忆是**给代码用的结构化中间结果**（计划、工具返回、结论） |

## 生命周期（这是它最硬的约束）

```
handle() 进入 ──> begin() 建一个 → 全网共享 → 轮末 clear()，失败才 dump()
```

- **一轮一个实例**，跨轮复用是 bug；
- **不落库**（唯一例外：本轮失败时 `dump()` 一份到 `memory/turns/` 供排障）；
- **值有上限**（单值 4k 字符、最多 200 个槽位），防止某个工具把 1MB 塞进上下文。

## 为什么用 contextvar 而不是一路传参

和 `core/exec_context.py` 同样的理由：工具签名是给 LLM 看的，不该为了传上下文变脏。
`asyncio.create_task()` 会复制 contextvar，所以 `gather` 出来的子智能体各自看得到同一个
工作台（共享上游结论），这正是编排需要的语义。

## 槽位命名约定

槽位名是**跨模块契约**，改名前先看这里：

| 槽位 | 写入方 | 读取方 |
|---|---|---|
| `plan` | Agent（规划后） | Agent（回答、重试） |
| `tool_results` | Agent（DAG 执行后） | Agent（回答生成）、Orchestrator |
| `findings` | Agent（每轮结束追加） | 重规划（`prior_findings`） |
| `subagent:<task_id>` | SubAgentPool | Agent（汇总） |
| `failure` | Agent（失败取证） | 排障脚本、`/api/traces` |
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from core.exception_policy import downgrade

#: 单个值 / 槽位总数的上限（工作记忆不能变成第二个数据库）
MAX_VALUE_CHARS = 4000
MAX_SLOTS = 200

#: 约定槽位名（见模块 docstring）
SLOT_PLAN = "plan"
SLOT_TOOL_RESULTS = "tool_results"
SLOT_FINDINGS = "findings"
SLOT_FAILURE = "failure"


@dataclass
class WorkingEntry:
    """工作台上的一个槽位。"""

    key: str
    value: Any = ""
    producer: str = ""
    note: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "producer": self.producer,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "truncated": self.truncated,
        }


class WorkingMemory:
    """一轮任务的工作台（进程内，不持久化）。

    用法（只在主链里出现，工具不该自己 new 一个）::

        wm = memory.working.begin(execution_id=..., session_id=...)
        wm.write(SLOT_TOOL_RESULTS, results, producer="agent")
        wm.read(SLOT_TOOL_RESULTS)
    """

    def __init__(
        self,
        *,
        execution_id: str = "",
        session_id: str = "",
        max_value_chars: int = MAX_VALUE_CHARS,
        max_slots: int = MAX_SLOTS,
        dump_dir: Optional[Path] = None,
    ):
        self.execution_id = execution_id
        self.session_id = session_id
        self.max_value_chars = max(1, int(max_value_chars))
        self.max_slots = max(1, int(max_slots))
        #: 失败取证目录（默认 `memory/turns/`；测试可指向 tmp_path）
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self.created_at = time.time()
        self._entries: Dict[str, WorkingEntry] = {}
        self.logger = get_logger()

    # ===== 读写 =====

    def write(
        self,
        key: str,
        value: Any,
        *,
        producer: str = "",
        note: str = "",
    ) -> WorkingEntry:
        """写一个槽位（同名覆盖）。值会被截断到上限并标注。"""
        key = (key or "").strip()
        if not key:
            raise ValueError("工作记忆槽位名不能为空")

        value, truncated = self._clip(value)

        entry = self._entries.get(key)
        if entry is None:
            entry = WorkingEntry(key=key, producer=producer, note=note)
            self._entries[key] = entry
            self._evict_if_needed()
        entry.value = value
        entry.producer = producer or entry.producer
        entry.note = note or entry.note
        entry.truncated = truncated
        entry.updated_at = time.time()
        return entry

    def append(self, key: str, item: Any, *, producer: str = "", note: str = "") -> WorkingEntry:
        """往一个列表槽位追加一条（`findings` 用）。"""
        current = self.read(key)
        if not isinstance(current, list):
            current = [] if current in (None, "") else [current]
        current.append(item)
        return self.write(key, current, producer=producer, note=note)

    def read(self, key: str, default: Any = None) -> Any:
        entry = self._entries.get(key)
        return entry.value if entry is not None else default

    def read_many(self, keys: List[str]) -> Dict[str, Any]:
        """批量读取（下游只拿自己声明要的，看不到别的）。"""
        return {
            k: self._entries[k].value
            for k in (keys or [])
            if k in self._entries
        }

    # ===== 约定槽位的便捷读写 =====

    def set_plan(self, plan: Any) -> None:
        self.write(SLOT_PLAN, plan, producer="agent", note="本轮计划")

    def get_plan(self, default: Any = None) -> Any:
        return self.read(SLOT_PLAN, default)

    def set_tool_results(self, results: Dict[str, Any]) -> None:
        self.write(SLOT_TOOL_RESULTS, results, producer="agent", note="工具执行结果")

    def get_tool_results(self) -> Dict[str, Any]:
        value = self.read(SLOT_TOOL_RESULTS)
        return value if isinstance(value, dict) else {}

    def add_finding(self, finding: str) -> None:
        """追加一条本轮结论（重规划时作为 `prior_findings` 回灌）。"""
        text = str(finding or "").strip()
        if text:
            self.append(SLOT_FINDINGS, text, producer="agent", note="本轮结论")

    def findings_text(self, *, limit: int = 3000) -> str:
        """把 `findings` 拼成给规划器看的文本。"""
        items = self.read(SLOT_FINDINGS) or []
        if not isinstance(items, list):
            items = [items]
        text = "\n".join(f"- {x}" for x in items if str(x).strip())
        return text[:limit]

    # ===== 观测与收尾 =====

    def keys(self) -> List[str]:
        return list(self._entries.keys())

    def snapshot(self) -> Dict[str, Any]:
        """结构化快照（给 trace / 前端流式面板用）。不含值，只有元信息 + 值预览。"""
        out: Dict[str, Any] = {}
        for key, entry in self._entries.items():
            preview = entry.value
            if not isinstance(preview, (str, int, float, bool, type(None))):
                try:
                    preview = json.dumps(preview, ensure_ascii=False)
                except Exception:
                    preview = str(preview)
            if isinstance(preview, str) and len(preview) > 200:
                preview = preview[:200] + "…"
            out[key] = {**entry.to_dict(), "preview": preview}
        return out

    def stats(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "slots": len(self._entries),
            "chars": sum(
                len(e.value) if isinstance(e.value, str) else 0
                for e in self._entries.values()
            ),
            "age_s": round(time.time() - self.created_at, 3),
        }

    def dump(self, directory: Optional[Path] = None, *, reason: str = "") -> Optional[Path]:
        """把整份工作台落盘（**只在本轮失败/排障时调用**）。

        返回落盘路径；写失败返回 None（取证不能影响业务）。
        """
        try:
            root = Path(directory) if directory else (self.dump_dir or (
                Path(__file__).resolve().parent.parent.parent / "memory" / "turns"
            ))
            root.mkdir(parents=True, exist_ok=True)
            name = self.execution_id or f"turn-{int(self.created_at * 1000)}"
            path = root / f"{name}.json"
            path.write_text(
                json.dumps(
                    {
                        "execution_id": self.execution_id,
                        "session_id": self.session_id,
                        "reason": reason,
                        "stats": self.stats(),
                        "slots": {
                            k: {**e.to_dict(), "value": e.value}
                            for k, e in self._entries.items()
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            return path
        except Exception as exc:
            try:
                self.logger.warning("WorkingMemory", f"工作记忆落盘失败: {exc}")
            except Exception as exc:
                downgrade(exc, where='core/memorykit/working.py')
            return None

    def clear(self) -> None:
        """轮末清空。**必须调用** —— 不清就是内存泄漏。"""
        self._entries.clear()

    # ===== 内部 =====

    def _clip(self, value: Any) -> tuple:
        if isinstance(value, str):
            if len(value) > self.max_value_chars:
                return value[: self.max_value_chars] + "…（已截断）", True
            return value, False
        if isinstance(value, (int, float, bool, type(None))):
            return value, False
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        if len(text) > self.max_value_chars:
            return text[: self.max_value_chars] + "…（已截断）", True
        return value, False

    def _evict_if_needed(self) -> None:
        """槽位超上限时丢掉**最早写入**的那个（保新不保旧）。"""
        if len(self._entries) <= self.max_slots:
            return
        oldest = min(self._entries.items(), key=lambda kv: kv[1].created_at)[0]
        self._entries.pop(oldest, None)
        try:
            self.logger.warning(
                "WorkingMemory", f"槽位超上限({self.max_slots})，已丢弃: {oldest}"
            )
        except Exception as exc:
            downgrade(exc, where='core/memorykit/working.py')
# ===== 当前轮的工作台（contextvar） =====

_current: ContextVar[Optional[WorkingMemory]] = ContextVar("dsh_working_memory", default=None)


def set_current_working(wm: Optional[WorkingMemory]) -> None:
    """主链在一轮开始时调用。"""
    _current.set(wm)


def get_current_working() -> Optional[WorkingMemory]:
    """取当前轮的工作台；没有就返回 None（调用方必须容忍）。"""
    try:
        return _current.get()
    except Exception:
        return None


def clear_current_working() -> None:
    _current.set(None)
