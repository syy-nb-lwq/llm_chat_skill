"""短期记忆（Short Term Memory）—— **这次会话我们说过什么**。

## 职责边界（唯一的一句话）

> 会话内容的**全量存档 + 压缩视图 + 会话内检索**。

## 与另外两层的分界

| 层 | 分界 |
|---|---|
| 工作记忆 | 短期存"说过的话"，不存"这一轮的工具返回"。工具结果进 `working`，不进这里 |
| 长期记忆 | 短期存的是**发生过什么**，不是**这个人是谁**。会话里的一句"我随便看看"不能被当画像 |

## 谁能写、谁能读（硬约束）

写：**只有会话层**（`Agent._persist_message` / `_maybe_rollup` / `reset`）。
　　工具、子智能体、critic —— 一律不许写。理由：写入口一多，消息顺序、`seq` 记账、
　　摘要覆盖范围就全乱了。

读：只有三个正当用途 ——
1. 会话恢复（`Agent._setup_session_persistence`，装回 `Context`）
2. 规划前的会话内检索（`ManagerAgent._search_session_history`）
3. 用户主动查看/删除（`/api/sessions*`）

## 存储

`memory/sessions.db`：`session_messages`（全量）+ `session_summaries`（滚动摘要）。
**不设过期、不删除**（用户主动删除外）；存储与注入分离，注入时才按预算裁剪。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


class ShortTermMemory:
    """会话内容层的唯一门面。

    这一层**没有自己的存储逻辑** —— 它只收口调用点，真正的 SQL 在
    `core/session_memory.py`（那里有 FTS / CJK 二元组 / reset 记账等一堆细节，
    不该在这里重写一遍）。
    """

    def __init__(self, store: Any = None):
        self._store = store
        self.logger = get_logger()

    @property
    def store(self):
        """底层存储（仅在需要 `maybe_rollup` 这类既有助手函数时使用）。

        **不缓存**：测试会 monkeypatch `core.session_memory.get_session_memory_store`
        来隔离到临时库，缓存住就会跨测试串库。
        """
        if self._store is not None:
            return self._store
        from core.session_memory import get_session_memory_store

        return get_session_memory_store()

    # ===== 写（只给会话层） =====

    def ensure_session(self, session_id: str) -> None:
        self.store.ensure_session(session_id)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        turn_id: str = "",
        tool_tasks: Optional[List[Dict]] = None,
    ) -> int:
        """追加一条消息，返回会话内绝对序号 `seq`。

        **只允许 `Context.sink`（即 `Agent._persist_message`）调用。**
        """
        return self.store.append_message(
            session_id=session_id,
            role=role,
            content=content,
            turn_id=turn_id,
            tool_tasks=tool_tasks,
        )

    async def rollup(self, context, *, session_id: str, logger=None) -> None:
        """会话过长时压缩早期内容（原文不删）。"""
        from core.session_rollup import maybe_rollup

        await maybe_rollup(
            context, self.store, session_id=session_id, logger=logger or self.logger
        )

    def reset(self, session_id: str) -> int:
        """标记一次 reset（此前的消息仍保留在库里，但不再装回上下文）。"""
        return self.store.mark_reset(session_id)

    # ===== 读（只给装配与用户查看） =====

    def session_exists(self, session_id: str) -> bool:
        return self.store.session_exists(session_id)

    def recent_messages(self, session_id: str, limit: int = 20) -> List[Any]:
        return self.store.get_recent_messages(session_id, limit=limit)

    def messages(self, session_id: str, limit: int = 0) -> List[Any]:
        """按时间顺序取消息（`limit=0` 表示全部）—— 给历史查看接口用。"""
        return self.store.get_messages(session_id, limit=limit)

    def latest_summary(self, session_id: str) -> Optional[Any]:
        return self.store.get_latest_summary(session_id)

    def reset_at_seq(self, session_id: str) -> int:
        return self.store.get_reset_at_seq(session_id)

    def search(self, session_id: str, query: str, limit: int = 5) -> List[Any]:
        """会话内检索（规划前用，让"刚才提到的那个"能接上）。"""
        try:
            return self.store.search(session_id, query, limit=limit)
        except Exception as exc:
            # 检索是增强，坏了不能拖垮规划
            try:
                self.logger.warning("ShortTermMemory", f"会话内检索失败: {exc}")
            except Exception:
                pass
            return []

    def count_messages(self, session_id: str) -> int:
        return self.store.count_messages(session_id)

    def list_sessions(self) -> List[Dict]:
        return self.store.list_sessions()

    def delete_session(self, session_id: str) -> bool:
        return self.store.delete_session(session_id)

    def stats(self) -> Dict[str, Any]:
        return self.store.get_stats()
