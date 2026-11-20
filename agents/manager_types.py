"""ManagerAgent 的纯数据类型 —— 从 manager.py 抽出以便判别层独立引用。

这里只有 dataclass 和常量，没有任何依赖，避免 `agents/intents.py` 与
`agents/manager.py` 互相 import 形成环。

- `PlanResult`：L0/L1/L2 各层的统一产出
- `IntentType`：意图标签字符串（与 `core/intent_labels.py::IntentLabel` 一一对应）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from skills.manager import Skill


@dataclass
class PlanResult:
    """规划结果"""
    intent: str                    # 意图标签(6 闭集之一,见 IntentLabel)
    intent_detail: str = ""        # 意图详情描述
    selected_skill: Optional["Skill"] = None
    is_retry: bool = False         # 是否是重试意图
    tool_tasks: List[Dict] = field(default_factory=list)
    need_llm: bool = False         # 是否需要 LLM 处理
    # 意图判别过程的可观测信息
    intent_layer: str = ""         # 产出该意图的层: L0 / L1 / L2
    intent_confidence: float = 0.0  # 置信度
    intent_uncertain: bool = False  # 置信度不足,已升级到 L2 兜底
    intent_slots: Dict[str, Any] = field(default_factory=dict)
    #: 规划层认为「目标已经达成」了吗？
    #:
    #: **这是目标循环的开关**：True（默认）表示这一轮规划就够了，
    #: 走完工具直接生成回答；False 表示还有没做完的事，需要再规划一轮。
    #: 默认 True 保证简单问答的行为和以前完全一样（永远只跑一轮）。
    goal_done: bool = True
    #: 还没做完什么（goal_done=False 时给下一轮看的提示）
    remaining_note: str = ""


class IntentType:
    CHITCHAT = "chitchat"     # 闲聊:问候、感谢、道别等
    TASK = "task"             # 任务:需要执行工具/技能完成
    TEACH = "teach"           # 教导:用户教 Agent 新技能
    RETRY = "retry"           # 重试:重新执行上次的任务
    MANAGER = "manager"       # 技能管理:列出/查看/版本/回滚(M1-09)
    CLARIFY = "clarify"       # 意图不明:需要澄清

    # 兼容别名:旧代码里的 SKILL / UNKNOWN
    SKILL = "task"
    UNKNOWN = "clarify"
