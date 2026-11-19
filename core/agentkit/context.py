"""上下文装配器 —— 给一个子智能体"够用且清晰"的信息包。

## 为什么不是一个字符串

现在子智能体拿到的是主对话的**全量快照**（`core/subagents.py`），
加上全量工具签名与全量技能列表。这有三个问题：
提示词膨胀、上下文污染（看到无关技能会误选）、出问题无法复盘。

所以上下文必须是一个**结构化对象**：

- 每块内容都带 `source`（从哪来）与 `why`（为什么给你）——
  **没有理由的上下文不允许注入**，这是"够用且清晰"的可检验形式；
- 有预算：每块上限 + 总量上限；
- 超限时按**优先级**裁，且**目标与验收标准永不裁**；
- 裁掉了什么要记下来 —— 复盘时先看这份记录。

## 裁剪优先级

`priority` 数字**越大越先被裁**：

| priority | 来源 | 为什么 |
|---|---|---|
| 1 | brief（目标与验收）、upstream（上游产出） | 丢了就没法干活 |
| 2 | skill（方法论） | 丢了会做不对 |
| 3 | memory（相关记忆） | 丢了会重复劳动 |
| 4 | tool（工具签名） | 丢了会调错参数 |
| 5 | workspace（工作区清单） | 丢了只是不知道有什么文件 |
| 6 | session（对话切片） | 丢了只是不知道指代 |
| 7 | role（角色与约束） | 丢了只是风格不对 |
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ===== 来源名 =====

SOURCE_ROLE = "role"
SOURCE_BRIEF = "brief"
SOURCE_PROFILE = "profile"
SOURCE_MEMORY = "memory"
SOURCE_SKILL = "skill"
SOURCE_TOOL = "tool"
SOURCE_WORKSPACE = "workspace"
SOURCE_SESSION = "session"
SOURCE_UPSTREAM = "upstream"

#: 每类来源的字符上限
BLOCK_LIMITS: Dict[str, int] = {
    SOURCE_ROLE: 800,
    SOURCE_BRIEF: 1500,
    SOURCE_PROFILE: 400,
    SOURCE_MEMORY: 2000,
    SOURCE_SKILL: 2500,
    SOURCE_TOOL: 1500,
    SOURCE_WORKSPACE: 1200,
    SOURCE_SESSION: 1500,
    SOURCE_UPSTREAM: 1200,
}

#: 默认单块上限（BLOCK_LIMITS 里没有的来源）
DEFAULT_BLOCK_LIMIT = 1200

#: 总量上限
DEFAULT_TOTAL = 12000

#: priority <= 这个值的块**永不裁**
NEVER_TRIM_PRIORITY = 1


@dataclass
class ContextBlock:
    """一块上下文。"""

    source: str
    #: 为什么给这块（必填 —— 没有理由的上下文不允许注入）
    why: str = ""
    content: str = ""
    chars: int = 0
    #: 数字越大越先被裁（见模块文档）
    priority: int = 5

    def __post_init__(self) -> None:
        if not self.chars:
            self.chars = len(self.content or "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "why": self.why,
            "content": self.content,
            "chars": self.chars,
            "priority": self.priority,
        }


@dataclass
class ContextBundle:
    """装配好的上下文包。"""

    blocks: List[ContextBlock] = field(default_factory=list)
    total_chars: int = 0
    #: 被裁掉的块及原因（复盘用）
    dropped: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, block: ContextBlock) -> "ContextBundle":
        self.blocks.append(block)
        self.total_chars = sum(b.chars for b in self.blocks)
        return self

    def by_source(self, source: str) -> List[ContextBlock]:
        return [b for b in self.blocks if b.source == source]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
            "total_chars": self.total_chars,
            "dropped": list(self.dropped),
        }

    def summary(self) -> str:
        """一行摘要（审计用）。"""
        return (
            f"上下文包：{len(self.blocks)} 块 / {self.total_chars} 字符"
            f"（裁掉 {len(self.dropped)} 块）"
        )


class ContextAssembler:
    """把若干来源的块装配成上下文包，带预算与裁剪记录。"""

    def __init__(
        self,
        *,
        block_limits: Optional[Dict[str, int]] = None,
        default_block_limit: int = DEFAULT_BLOCK_LIMIT,
        total_limit: int = DEFAULT_TOTAL,
    ):
        self.block_limits = dict(BLOCK_LIMITS)
        if block_limits:
            self.block_limits.update(block_limits)
        self.default_block_limit = default_block_limit
        self.total_limit = total_limit

    def assemble(
        self,
        blocks: List[ContextBlock],
        *,
        total_limit: Optional[int] = None,
    ) -> ContextBundle:
        """装配。返回带 `dropped` 记录的上下文包。"""
        limit = self.total_limit if total_limit is None else total_limit
        bundle = ContextBundle()
        kept: List[ContextBlock] = []

        # --- 第一步：单块上限 ---
        for block in blocks or []:
            max_len = self.block_limits.get(block.source, self.default_block_limit)
            if len(block.content or "") > max_len:
                trimmed = (block.content or "")[:max_len]
                bundle.dropped.append({
                    "source": block.source,
                    "why": block.why,
                    "chars": len(block.content or ""),
                    "reason": "block_limit",
                    "kept_chars": max_len,
                })
                block = ContextBlock(
                    source=block.source,
                    why=block.why,
                    content=trimmed,
                    chars=len(trimmed),
                    priority=block.priority,
                )
            kept.append(block)

        # --- 第二步：总量上限（priority 大的先裁）---
        total = sum(b.chars for b in kept)
        if total > limit:
            # 大 → 小 排序；priority 相同按块大小大 → 小（先裁大的）
            order = sorted(
                range(len(kept)),
                key=lambda i: (-kept[i].priority, -kept[i].chars),
            )
            for idx in order:
                if total <= limit:
                    break
                block = kept[idx]
                if block.priority <= NEVER_TRIM_PRIORITY:
                    continue    # 目标与验收永不裁
                bundle.dropped.append({
                    "source": block.source,
                    "why": block.why,
                    "chars": block.chars,
                    "reason": "total_limit",
                })
                total -= block.chars
                kept[idx] = None  # type: ignore[call-overload]

        bundle.blocks = [b for b in kept if b is not None]
        bundle.total_chars = sum(b.chars for b in bundle.blocks)
        return bundle
