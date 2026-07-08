"""Learning Agent - 流转中枢:执行工具、获取数据"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from infra.logger import get_logger
from tools.base import ToolResult, get_tool_registry


@dataclass
class ToolTask:
    """工具任务"""
    id: str
    type: str
    params: Dict = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    parallel_group: Optional[str] = None


class LearningAgent:
    """Learning Agent - 流转中枢

    职责:
    1. 工具注册
    2. 工具调用
    3. 工具结果处理
    """

    def __init__(self):
        self.logger = get_logger()
        self.tools = get_tool_registry()

    async def execute_tool(
        self,
        tool_type: str,
        params: Dict,
        on_event: Optional[Callable] = None,
    ) -> ToolResult:
        """执行单个工具"""
        tool = self.tools.get(tool_type)
        if not tool:
            return ToolResult(success=False, error=f"工具不存在: {tool_type}")

        async def _run():
            self.logger.info(tool_type, f"执行工具: {params}")
            try:
                result = await tool.execute(**params)
                self.logger.info(tool_type, f"执行成功")
                return result
            except Exception as e:
                self.logger.error(tool_type, f"执行失败: {e}")
                return ToolResult(success=False, error=str(e))

        retry = params.pop("_retry", 1)
        last_err = None
        for attempt in range(retry):
            result = await _run()
            if result.success:
                if on_event:
                    await on_event("tool_result", {
                        "task_id": tool_type,
                        "tool": tool_type,
                        "data": result.data,
                        "meta": getattr(result, "meta", {}),
                    })
                return result
            last_err = result.error
            if attempt < retry - 1:
                self.logger.warning("Learning", f"{tool_type} 第 {attempt+1} 次失败: {last_err},重试")
        return ToolResult(success=False, error=last_err or "unknown")

    async def execute_dag(
        self,
        tasks: List[ToolTask],
        on_event: Optional[Callable] = None,
    ) -> Dict[str, ToolResult]:
        """DAG 执行"""
        if not tasks:
            return {}

        # 检测循环
        if _has_cycle(tasks):
            self.logger.error("Learning", "DAG 存在循环依赖")
            return {}

        # 构建依赖图
        pending = {t.id: t for t in tasks}
        results: Dict[str, ToolResult] = {}
        running: Dict[str, Awaitable] = {}

        async def emit(event: str, payload: dict):
            if on_event:
                await on_event(event, payload)

        while pending or running:
            # 启动所有可执行的任务
            for task_id, task in list(pending.items()):
                deps_done = all(r in results for r in task.depends_on)
                if deps_done:
                    del pending[task_id]
                    coro = self.execute_tool(task.type, task.params, emit)
                    running[task_id] = asyncio.create_task(coro)

            # 等待任意一个完成
            if running:
                done, pending_items = await asyncio.wait(
                    running.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for finished in done:
                    for task_id, task_obj in list(running.items()):
                        if task_obj is finished:
                            results[task_id] = await task_obj
                            del running[task_id]
                            break

            # 如果没有可启动的任务且没有运行中的任务，退出
            if not pending and not running:
                break

        return results


def _has_cycle(tasks: List[ToolTask]) -> bool:
    """检测循环依赖"""
    visited = set()
    rec_stack = set()

    def dfs(task_id: str) -> bool:
        visited.add(task_id)
        rec_stack.add(task_id)
        for task in tasks:
            if task.id == task_id:
                for dep in task.depends_on:
                    if dep not in visited:
                        if dfs(dep):
                            return True
                    elif dep in rec_stack:
                        return True
        rec_stack.remove(task_id)
        return False

    for task in tasks:
        if task.id not in visited:
            if dfs(task.id):
                return True
    return False
