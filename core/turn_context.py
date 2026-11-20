"""一轮对话的状态容器（`TurnContext`）。

## 为什么要有它

`Agent.handle()` 原来是 494 行的单函数，"这一轮的状态"全是局部变量：
`plan` / `tool_results` / `goal` / `ans` / `goal_note` / `entities` /
`planned_holder` / `active` / `profile_block` / `capability_block` / `emit` /
`pending_async` / `trace`……

想把 handle 拆成 stage 就卡在这里：每个 stage 都要读其中大半，
只能靠闭包捕获 —— 于是"拆出来的方法"其实和原函数共享一坨隐式状态，
既不能单独测，改一个变量也看不出影响面（`docs/19` §05）。

`TurnContext` 把这坨隐式状态变成**显式的、每轮一个**的对象：

- 生命周期：`handle` 开头建，`handle` 的 `finally` 里随工作记忆一起收，
  不跨轮复用（跨轮的东西属于 `self`，不属于这里）；
- 所有 stage 方法都收 `tc: TurnContext`，读写哪些字段一眼可见；
- 单测可以只造一个 `tc` 直接调某个 stage，不必跑完整个 handle。

## 它不是"会话状态"

会话级（`self.context` / `self.identity` / 滚动摘要）不在这里。
这里只有**这一轮**的东西 —— 轮末就该消失，留下就是内存泄漏。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class TurnContext:
    """一轮对话（一次 `handle` 调用）的可变状态。"""

    #: 用户这一轮的输入（原样保留，不清洗 —— stage 之间看到的是同一份）
    user_input: str
    #: wall clock 起点，只用于日志里的总耗时
    start: float
    #: `perf_counter` 起点，用于 trace 的分阶段耗时（不能用 wall clock 混算）
    turn_start: float

    #: 事件派发器（同步闭包，内部把异步回调塞进 `pending_async` 串行执行）
    emit: Callable[[str, dict], None] = field(default=lambda event, payload: None)
    #: 待 drain 的异步回调队列。顺序即事件顺序，不允许并发执行
    pending_async: List[Any] = field(default_factory=list)

    #: 本轮 trace（`core/trace.py`），失败时为 None 也不影响主链
    trace: Any = None
    #: 本轮画像 / 能力清单文本，规划与回答共用同一份，避免两处读到不一致
    profile_block: str = ""
    capability_block: str = ""

    #: 当前计划。目标循环里会被重规划覆盖，回答阶段用的是最后一次
    plan: Any = None
    #: 从用户输入里抽出的实体（供 Skill DAG 的 `${user_input.city}` 之类占位）
    entities: Dict[str, Any] = field(default_factory=dict)

    #: **最后一轮**实际执行的 ToolTask 列表 —— critic 要靠它拿真实工具名 /
    #: retry / fallback，所以不能只看首轮计划
    planned_tasks: List[Any] = field(default_factory=list)
    #: 工具返回（key 是 task_id）
    tool_results: Dict[str, Any] = field(default_factory=dict)
    #: 目标循环的结果（`core/goal_runner.GoalRun`），含 completed / rounds / explanation
    goal: Any = None

    #: 本轮最终回答。`finally` 里靠它记 reply_chars —— 所有提前 return
    #: 的路径都必须先回填，否则 trace 会显示"回复 0 字"
    answer: str = ""
    #: 本轮耗时（毫秒）。日志与 critic 用同一个数，避免两处各算一遍
    duration_ms: float = 0.0
    #: 本轮异常（字符串形式，喂给 trace）
    error: str = ""

    @property
    def skill(self) -> Any:
        """当前计划选中的技能（没有则为 None）—— 高频访问，给个短名字。"""
        return getattr(self.plan, "selected_skill", None)

    @property
    def skill_name(self) -> Optional[str]:
        skill = self.skill
        return getattr(skill, "name", None) if skill else None
