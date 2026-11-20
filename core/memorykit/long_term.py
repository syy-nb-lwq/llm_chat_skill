"""长期记忆（Long Term Memory）—— **这个人是谁 / 什么做法长期有效**。

## 职责边界

> 跨会话、跨天仍然成立的东西。一句话概括：**画像 + 经验 + 台账**。

它由三个子库组成，但**对外只有一个入口**（这个模块）。之所以收口，是因为
以前调用方要自己判断"该找 `semantic_memory` 还是 `memory` 还是 `skill_quality`"，
同一份语义散在三个存储里，谁也不知道哪个才是权威。

| 子库 | 存什么 | 载体 | 写入方 | 读取方 |
|---|---|---|---|---|
| `profile`（画像） | 偏好/习惯/方法/雷区/事实 | `memory/semantic_memory.db` | `MemoryWriter`（**异步**） | 轮首注入（≤400 字常驻） |
| `experience`（经验） | 失败诊断、成功模式、待审补丁 | `memory/failures`、`memory/successes`、`memory/skill_patches` | `ExecutionCritic`、`Reflect` | 规划前 hints、自演化面板 |
| `skill_ledger`（台账） | 技能通过率（验收 + 线上被纠正） | `memory/skills/quality.jsonl` | `SkillQualityStore` | planner 过滤降级技能 |

## 硬约束

1. **写必须异步、必须与回答解耦**：画像抽取是一次 LLM 调用，绝不能挡在
   `handle()` 的返回路径上（现在由 `_schedule_memory_write` 起后台 task）。
2. **工具内部禁止写长期记忆**。一次工具调用就把"用户偏好"改了，等于让
   随机输出污染画像；画像只能来自用户自己说的话。
3. **读必须每轮只读一次**（`Agent._get_profile_block` 有 `_profile_cache`）。
   画像每轮都在 system prompt 里，读两次会拿到不一致的两份。
4. **不做全量注入**：画像 ≤400 字常驻；经验只按 query 相关度取 top-k。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from core.exception_policy import downgrade


class LongTermMemory:
    """长期记忆的唯一门面（画像 / 经验 / 台账）。"""

    def __init__(self):
        self.logger = get_logger()

    # ===== 子库：懒加载，避免 import 期建库 =====

    @property
    def profile_store(self):
        from core.semantic_memory import get_semantic_memory

        return get_semantic_memory()

    @property
    def experience(self):
        """自演化用的 JSON 经验库（`core/memory.py::MemoryStore`）。"""
        from core.memory import get_memory_store

        return get_memory_store()

    @property
    def skill_ledger(self):
        from core.skill_quality import get_skill_quality_store

        return get_skill_quality_store()

    @property
    def writer(self):
        from core.memory_writer import get_memory_writer

        return get_memory_writer()

    # ===== 画像：读 =====

    def profile_block(
        self, limit: int = 20, max_chars: int = 400, query: str = ""
    ) -> str:
        """常驻注入的画像文本块（每轮只取一次，由 Agent 缓存）。

        `query` 是本轮用户输入：**限定场景**的画像（"在 minimax 上用国内站"）
        只在 query 提到该场景时才注入，否则整条不出现 —— 不然聊电商时
        助手会拿它去否掉跨境电商。
        """
        try:
            return self.profile_store.build_profile_block(
                limit=limit, max_chars=max_chars, query=query
            )
        except Exception as exc:
            try:
                self.logger.warning("LongTermMemory", f"画像读取失败: {exc}")
            except Exception as exc:
                downgrade(exc, where='core/memorykit/long_term.py')
            return ""

    def profile_entries(self, limit: int = 50) -> List[Any]:
        """「你记住了我什么」—— 给记忆面板用（全量画像类型，不是注入用的 top-N）。"""
        from core.memory_db import PROFILE_MEMORY_TYPES

        return self.profile_store.get_recent(
            types=list(PROFILE_MEMORY_TYPES), limit=limit
        )

    def get_profile_entry(self, memory_id: str) -> Optional[Any]:
        return self.profile_store.memory_db.get(memory_id)

    def forget(self, memory_id: str) -> bool:
        return self.profile_store.forget(memory_id)

    # ===== 画像：写（只允许后台写入器） =====

    async def record_turn(
        self,
        user_input: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        context: str = "",
    ) -> Any:
        """从一轮用户输入里抽取画像并入库（LLM + 证据校验）。

        `context` = 最近几轮对白，只用来让抽取器听懂"国际站"指的是哪个平台。
        """
        return await self.writer.record_turn(
            user_input,
            session_id=session_id,
            turn_id=turn_id,
            context=context,
        )

    # ===== 经验：读 =====

    def hints(self, user_input: str) -> List[str]:
        """与当前输入相关的历史教训 / 成功模式（按需，不全量）。"""
        try:
            return self.experience.get_skill_hints(user_input)
        except Exception as exc:
            try:
                self.logger.warning("LongTermMemory", f"经验 hints 读取失败: {exc}")
            except Exception as exc:
                downgrade(exc, where='core/memorykit/long_term.py')
            return []

    def recent_failures(self, top_k: int = 10) -> List[Any]:
        return self.experience.get_recent_failures(top_k=top_k)

    # ===== 经验：写（critic / reflect） =====

    def pending_patches(self) -> List[Any]:
        return self.experience.get_pending_patches()

    def approve_patch(self, patch_id: str, reviewer: str = "human") -> bool:
        return self.experience.approve_patch(patch_id, reviewer=reviewer)

    def reject_patch(self, patch_id: str, reviewer: str = "human") -> bool:
        return self.experience.reject_patch(patch_id, reviewer=reviewer)

    def stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, getter in (
            ("profile", lambda: self.profile_store.get_stats()),
            ("experience", lambda: self.experience.get_stats()),
        ):
            try:
                out[name] = getter()
            except Exception as exc:
                out[name] = {"error": str(exc)}
        return out

    # ===== 台账：技能质量 =====

    def skill_quality(self, name: str):
        return self.skill_ledger.get(name)

    def demoted_skills(self) -> set:
        try:
            return self.skill_ledger.demoted_skills()
        except Exception:
            return set()

    def quality_of(self, name: str) -> Optional[Dict[str, Any]]:
        try:
            q = self.skill_ledger.get(name)
            return q.to_dict() if q is not None else None
        except Exception:
            return None
