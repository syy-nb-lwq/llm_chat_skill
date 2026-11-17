"""子智能体工具 —— 主智能体用来「派活」的入口。

## 用法

规划层可以在 `tool_tasks` 里放：

```json
{"id": "t1", "type": "run_subagents", "params": {
  "tasks": [
    {"name": "查政策", "goal": "查 2026 年微短剧最新的监管要求，列出关键条款"},
    {"name": "查案例", "goal": "找两个近期被处罚的微短剧案例，写出处罚原因"}
  ]
}}
```

几个子任务会**并行**执行，每个在自己的上下文里干活，只回结论。

## 主智能体怎么"探测完成情况"

两个层次：

1. **本轮同步**：`run_subagents` 会等所有子任务结束（受超时保护），
   返回里带每个子任务的状态/耗时/用过的工具/结论。规划层下一轮就能看到。
2. **过程可见**：池子会通过 `core.event_bus` 推 `subagent_spawned` /
   `subagent_progress` / `subagent_done`，前端流转面板能实时看到。

## 边界（都在池子里实现，这里只传参）

- 并发上限、单个超时、深度上限
- 子智能体不能再派子智能体（深度到了就把本工具从它的工具表里摘掉）
- **失败会出现在返回文本里**，并要求主智能体如实汇报
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


class RunSubAgentsTool(Tool):
    """派一个或多个子智能体并行执行子任务。"""

    name = "run_subagents"
    description = (
        "把**互不依赖**的子任务派给子智能体并行执行，然后收回它们的结论。\n"
        "适合：一个任务能拆成几件可以同时做的事（查多个来源、分别造几个工具、"
        "各自处理一批数据）。\n"
        "不适合：有先后依赖的步骤（那种按顺序拆成多个 tool_task 更合适）。\n"
        "注意：子任务失败会在返回里明确列出，回答时必须如实说明。"
    )
    required_flag = "subagents_enabled"

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="tasks",
                    type="array",
                    required=True,
                    description=(
                        '子任务列表：[{"name": "短名", "goal": "具体要做什么、'
                        '要产出什么"}]。goal 要写成能独立执行的一句话。'
                    ),
                ),
            ],
            returns={
                "summary": "成功/失败/超时计数",
                "tasks": "[{name, status, result, error, duration_ms}]",
            },
        )

    async def execute(self, tasks: Optional[List[Dict[str, Any]]] = None) -> ToolResult:
        from core.event_bus import emit
        from core.subagents import get_subagent_pool

        specs = tasks or []
        if not isinstance(specs, list) or not specs:
            return ToolResult(
                success=False,
                error=(
                    "tasks 不能为空。给一个子任务列表："
                    '[{"name":"查政策","goal":"查 2026 年微短剧最新监管要求"}]'
                ),
            )

        pool = get_subagent_pool()

        async def on_event(event: str, payload: dict):
            # 转推给当前轮的事件流（前端能看到子智能体在干什么）
            await emit(event, payload)

        self.logger.info("SubAgents", f"派出 {len(specs)} 个子任务")
        # depth=1：这些是"子"智能体；到上限后它们自己不能再派
        report = await pool.run_many(specs, depth=1, on_event=on_event)

        data = report.to_dict()
        data["summary"] = report.summary_line()
        text = report.as_text()

        return ToolResult(
            success=report.done > 0,       # 至少一个成功就算这个工具"有产出"
            data={**data, "text": text},
            error="" if report.all_ok else report.summary_line(),
            meta={
                "total": len(report.tasks),
                "done": report.done,
                "failed": report.failed,
                "timeout": report.timeout,
            },
        )


class SubAgentStatusTool(Tool):
    """查子智能体的当前状态（运行中也能查）。"""

    name = "subagent_status"
    description = (
        "查看子智能体任务的状态（运行中/已完成/失败/超时）与进度。"
        "想知道「刚才派的活做到哪了」时用它。"
    )
    required_flag = "subagents_enabled"

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[],
            returns={"tasks": "[{id, name, status, progress, duration_ms}]"},
        )

    async def execute(self) -> ToolResult:
        from core.subagents import get_subagent_pool

        pool = get_subagent_pool()
        items = pool.status()
        return ToolResult(
            success=True,
            data={
                "count": len(items),
                "active": pool.active_count(),
                "tasks": items,
            },
        )
