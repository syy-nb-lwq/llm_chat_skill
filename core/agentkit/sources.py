"""上下文来源装配 —— 把九类信息源拼成一个上下文包。

## 九类来源与它们的作用

| 来源 | 装什么 | 为什么必须有 |
|---|---|---|
| `brief` | 目标 + 验收标准 + 交付物 | **永不裁** —— 丢了就不知道做到什么程度算完成 |
| `role` | 岗位、职责、硬边界 | 让子智能体知道自己是谁、能干什么 |
| `profile` | 用户画像（≤400 字） | "结合使用者习惯"这条定位的落点 |
| `memory` | 与本任务相关的长期记忆（top-k） | 避免重复劳动、避免踩已知的坑 |
| `skill` | 相关技能的方法（召回 top-k） | 方法论要能被用上，而不是全量塞进去 |
| `tool` | 授权工具的签名 | **只给白名单内的** —— 少即是多 |
| `workspace` | 已有文件/产物清单 | 知道手上有什么，别重做 |
| `session` | 最近 2 轮对话切片 | 指代消解（"发"指的是刚才那个视频） |
| `upstream` | 上游写进黑板的槽位 | 协作的数据流 |

## 与"全量倾倒"的差别

改造前，子智能体拿到的是主对话的**全量快照** + **全量工具** + **全量技能**。
改造后：按角色裁剪、按需召回、有预算、有裁剪记录。

**每个块都带 `why`** —— 没有理由的上下文不允许注入。
这是"够用且清晰"唯一能被检验的形式。

## 注入日志

装配完写一条审计（`memory/audit/context_<exec>.jsonl`）：
注入了什么、裁了什么、为什么裁。子智能体做错时，**第一件事就是看这份日志**。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.agentkit.context import (
    SOURCE_BRIEF,
    SOURCE_MEMORY,
    SOURCE_PROFILE,
    SOURCE_ROLE,
    SOURCE_SESSION,
    SOURCE_SKILL,
    SOURCE_TOOL,
    SOURCE_UPSTREAM,
    SOURCE_WORKSPACE,
    ContextAssembler,
    ContextBlock,
    ContextBundle,
)
from core.agentkit.roles import RoleProfile

#: 各类来源的裁剪优先级（数字越大越先被裁，见 core/agentkit/context.py）
PRIORITY = {
    SOURCE_BRIEF: 1,
    SOURCE_UPSTREAM: 1,
    SOURCE_SKILL: 2,
    SOURCE_PROFILE: 3,
    SOURCE_MEMORY: 3,
    SOURCE_TOOL: 4,
    SOURCE_WORKSPACE: 5,
    SOURCE_SESSION: 6,
    SOURCE_ROLE: 7,
}


class ContextBuilder:
    """按角色与任务装配上下文包。"""

    def __init__(
        self,
        *,
        assembler: Optional[ContextAssembler] = None,
        memory_facade: Any = None,
        skill_store: Any = None,
    ):
        self.assembler = assembler or ContextAssembler()
        self._memory = memory_facade
        self._skill_store = skill_store

    # ===== 依赖（懒加载，避免循环 import 与测试时的副作用） =====

    @property
    def memory(self):
        if self._memory is None:
            from core.agentkit.memory import get_memory_facade

            self._memory = get_memory_facade()
        return self._memory

    @property
    def skill_store(self):
        if self._skill_store is None:
            from skills.manager import get_skill_store

            self._skill_store = get_skill_store()
        return self._skill_store

    # ===== 装配 =====

    def build(
        self,
        *,
        goal: str,
        acceptance_criteria: Optional[List[str]] = None,
        deliverable: Optional[Dict[str, Any]] = None,
        role: Optional[RoleProfile] = None,
        session_id: str = "",
        agent_id: str = "",
        allowed_tools: Optional[List[str]] = None,
        allowed_skills: Optional[List[str]] = None,
        profile_block: str = "",
        context: Any = None,
        blackboard: Any = None,
        inputs: Optional[List[str]] = None,
        session_turns: int = 2,
        memory_limit: int = 3,
    ) -> ContextBundle:
        """装配上下文包。返回带 `dropped` 记录的 `ContextBundle`。"""
        blocks: List[ContextBlock] = []

        # --- 1. brief（目标与验收）---
        brief_lines = [f"目标: {goal}"]
        if acceptance_criteria:
            brief_lines.append("验收标准（做到这些才算完成）:")
            brief_lines.extend(f"  - {c}" for c in acceptance_criteria)
        if deliverable:
            brief_lines.append(f"交付物: {deliverable}")
        blocks.append(ContextBlock(
            source=SOURCE_BRIEF,
            why="这是你这一轮要完成的事与完成标准",
            content="\n".join(brief_lines),
            priority=PRIORITY[SOURCE_BRIEF],
        ))

        # --- 2. role（岗位与硬边界）---
        if role is not None:
            blocks.append(ContextBlock(
                source=SOURCE_ROLE,
                why="你的岗位设定与不许越界的地方",
                content=role.as_prompt(),
                priority=PRIORITY[SOURCE_ROLE],
            ))

        # --- 3. profile（用户画像）---
        if not profile_block:
            profile_block = self._profile_block()
        if profile_block:
            blocks.append(ContextBlock(
                source=SOURCE_PROFILE,
                why="这位用户的长期习惯，做事要按他的方式来",
                content=profile_block,
                priority=PRIORITY[SOURCE_PROFILE],
            ))

        # --- 4. memory（相关记忆）---
        mem_text = self._memory_block(
            query=goal,
            agent_id=agent_id,
            limit=memory_limit,
            scopes=(role.memory_scopes if role else None),
        )
        if mem_text:
            blocks.append(ContextBlock(
                source=SOURCE_MEMORY,
                why="与本次任务相关的历史记忆（避免重复劳动与重踩坑）",
                content=mem_text,
                priority=PRIORITY[SOURCE_MEMORY],
            ))

        # --- 5. skill（相关方法论）---
        skill_text = self._skill_block(
            query=goal, allowed_skills=allowed_skills
        )
        if skill_text:
            blocks.append(ContextBlock(
                source=SOURCE_SKILL,
                why="与本次任务相关的方法论（按需召回，不是全量技能表）",
                content=skill_text,
                priority=PRIORITY[SOURCE_SKILL],
            ))

        # --- 6. tool（授权工具签名）---
        tool_text = self._tool_block(allowed_tools)
        if tool_text:
            blocks.append(ContextBlock(
                source=SOURCE_TOOL,
                why="你被授权使用的工具及参数（只能用这些）",
                content=tool_text,
                priority=PRIORITY[SOURCE_TOOL],
            ))

        # --- 7. workspace（已有文件）---
        ws_text = self._workspace_block()
        if ws_text:
            blocks.append(ContextBlock(
                source=SOURCE_WORKSPACE,
                why="工作区里已经有的东西，别重做",
                content=ws_text,
                priority=PRIORITY[SOURCE_WORKSPACE],
            ))

        # --- 8. session（指代消敏所需的最近几轮）---
        session_text = self._session_block(context, turns=session_turns)
        if session_text:
            blocks.append(ContextBlock(
                source=SOURCE_SESSION,
                why="最近的对话（用于理解指代：'那个''发'指的是什么）",
                content=session_text,
                priority=PRIORITY[SOURCE_SESSION],
            ))

        # --- 9. upstream（上游产出）---
        if blackboard is not None and inputs:
            upstream = self._upstream_block(blackboard, inputs)
            if upstream:
                blocks.append(ContextBlock(
                    source=SOURCE_UPSTREAM,
                    why="上游子任务的结果（按你声明的键拉取）",
                    content=upstream,
                    priority=PRIORITY[SOURCE_UPSTREAM],
                ))

        bundle = self.assembler.assemble(blocks)
        self._log_injection(bundle, goal=goal, agent_id=agent_id)
        return bundle

    # ===== 各来源的实现 =====

    def _profile_block(self) -> str:
        try:
            from core.semantic_memory import get_semantic_memory

            return get_semantic_memory().build_profile_block() or ""
        except Exception:
            return ""

    def _memory_block(
        self,
        *,
        query: str,
        agent_id: str,
        limit: int,
        scopes: Optional[List[str]] = None,
    ) -> str:
        if not query:
            return ""
        try:
            entries = self.memory.read(
                scopes=scopes or ["user", "agent"],
                query=query,
                limit=limit,
                agent_id=agent_id or None,
                types=["episode", "failure", "context", "preference", "constraint"],
            )
        except Exception:
            return ""
        if not entries:
            return ""
        lines = []
        for e in entries[:limit]:
            lines.append(f"- [{e.type}] {e.content}")
        return "\n".join(lines)

    def _skill_block(
        self, *, query: str, allowed_skills: Optional[List[str]] = None
    ) -> str:
        try:
            matched = self.skill_store.match(query, 3)
        except Exception:
            return ""
        if allowed_skills is not None:
            allowed = set(allowed_skills)
            matched = [
                s for s in matched
                if s.name in allowed or f"{s.name}@{s.version}" in allowed
            ]
        if not matched:
            return ""
        lines = []
        for s in matched:
            lines.append(f"### {s.name}")
            if s.capability:
                lines.append(f"能力: {s.capability}")
            if s.method:
                lines.append(f"方法: {s.method}")
        return "\n".join(lines)

    def _tool_block(self, allowed_tools: Optional[List[str]]) -> str:
        try:
            from tools.hub import get_tool_hub

            hub = get_tool_hub()
            schemas = hub.schemas()
        except Exception:
            return ""
        if allowed_tools is not None:
            allow = set(allowed_tools)
            schemas = [s for s in schemas if s.name in allow]
        if not schemas:
            return ""
        lines = []
        for sch in schemas:
            try:
                parts = []
                for pm in sch.params:
                    mark = "" if getattr(pm, "required", True) else "?"
                    ptype = (getattr(pm, "type", "") or "string").replace("string", "str")
                    parts.append(f"{pm.name}{mark}: {ptype}")
                desc = (sch.description or "").strip().splitlines()[0][:80]
                lines.append(f"- {sch.name}({', '.join(parts) or '无参数'}) — {desc}")
            except Exception:
                continue
        return "\n".join(lines)

    def _workspace_block(self) -> str:
        try:
            from core.workspace_view import build_workspace_block

            return build_workspace_block() or ""
        except Exception:
            return ""

    def _session_block(self, context: Any, *, turns: int = 2) -> str:
        if context is None:
            return ""
        try:
            history = context.to_llm_messages(max_tokens=600)
        except Exception:
            return ""
        picked = [
            m for m in history
            if m.get("role") in ("user", "assistant") and m.get("content")
        ][-turns:]
        if not picked:
            return ""
        lines = []
        for m in picked:
            role = "用户" if m.get("role") == "user" else "助手"
            content = (m.get("content") or "").strip()
            if len(content) > 400:
                content = content[:400] + "…"
            lines.append(f"- {role}: {content}")
        return "\n".join(lines)

    def _upstream_block(self, blackboard: Any, inputs: List[str]) -> str:
        try:
            values = blackboard.read_many(inputs)
        except Exception:
            return ""
        if not values:
            return ""
        lines = []
        for key, value in values.items():
            text = value if isinstance(value, str) else str(value)
            if len(text) > 1200:
                text = text[:1200] + "…"
            lines.append(f"### {key}\n{text}")
        return "\n".join(lines)

    # ===== 注入日志 =====

    def _log_injection(
        self,
        bundle: ContextBundle,
        *,
        goal: str,
        agent_id: str,
    ) -> None:
        try:
            from core.audit import append_jsonl, current_identity, event as mk_event

            payload = mk_event(
                "context_injected",
                **current_identity(),
                goal=(goal or "")[:120],
                total_chars=bundle.total_chars,
                blocks=[
                    {
                        "source": b.source,
                        "why": b.why,
                        "chars": b.chars,
                        "priority": b.priority,
                    }
                    for b in bundle.blocks
                ],
                dropped=list(bundle.dropped),
            )
            append_jsonl("context.jsonl", payload)
        except Exception:
            pass
