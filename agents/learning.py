"""Learning Agent - 流转中枢:工具调用、DAG 执行"""
import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from infra.logger import get_logger, LogType
from tools.base import ToolResult, get_tool_registry


_VAR_RE = re.compile(r"\$\{([\w\.]+)\}")


def resolve_params(template: Any, results: Dict[str, ToolResult]) -> Any:
    """递归替换 ${task_id.data.field} 变量。
    上游失败时,该变量替换为空字符串(不抛错,避免级联崩溃)。
    """
    if isinstance(template, str):
        def replace(m):
            path = m.group(1).split(".")
            obj: Any = results.get(path[0])
            if obj is None or not obj.success:
                return ""
            for k in path[1:]:
                if isinstance(obj, ToolResult):
                    obj = obj.data
                if isinstance(obj, dict):
                    obj = obj.get(k)
                else:
                    obj = getattr(obj, k, None)
                if obj is None:
                    return ""
            return str(obj)
        return _VAR_RE.sub(replace, template)

    if isinstance(template, dict):
        return {k: resolve_params(v, results) for k, v in template.items()}
    if isinstance(template, list):
        return [resolve_params(v, results) for v in template]
    return template


@dataclass
class ToolTask:
    """DAG 中的一个工具任务"""
    id: str
    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    parallel_group: Optional[str] = None
    retry: int = 0
    timeout_s: int = 30
    fallback_to: Optional[str] = None


class LearningAgent:
    """Learning Agent - 流转中枢

    职责:
    1. 执行工具调用 - 按 DAG 调度(拓扑序)
    2. 获取数据 - 通过工具获取完成任务所需的数据
    """

    def __init__(self):
        self.logger = get_logger()
        self.registry = get_tool_registry()
        # 同步工具在线程池里跑,避免阻塞事件循环
        self._executor = ThreadPoolExecutor(max_workers=8)

    # ----- 旧 API 兼容 -----
    @property
    def tools(self) -> Dict[str, Any]:
        """兼容旧代码:返回 {tool_name: tool_instance}"""
        return {name: self.registry.get(name) for name in self.registry.names()}

    def execute_task(self, task_type: str, params: Dict[str, Any]) -> ToolResult:
        """单次执行(同步,向后兼容)"""
        return self._run_with_retry_sync(task_type, params, retry=0, timeout_s=30)

    def execute_tasks(self, tasks: List[Dict]) -> Dict[str, ToolResult]:
        """批量执行(同步,串行)。每项是 {type, params} dict,无 DAG。"""
        results: Dict[str, ToolResult] = {}
        for t in tasks:
            r = self.execute_task(t.get("type", ""), t.get("params", {}))
            results[t.get("type", "")] = r
        return results

    def list_tools(self) -> List[str]:
        return self.registry.names()

    # ----- 新 API -----
    async def execute_dag(
        self,
        tasks: List[ToolTask],
        on_event: Optional[Callable[[Dict], Awaitable[None]]] = None,
    ) -> Dict[str, ToolResult]:
        """异步 DAG 执行。
        1. 检测循环依赖
        2. 同 parallel_group 且依赖已满足的 task 并行触发
        3. 每完成一个 task,resolve 下游 params
        4. emit event 给上层
        """
        self.logger.log_flow("Learning", f"DAG 开始: {len(tasks)} 个任务")

        if not tasks:
            return {}

        # 检测循环
        if _has_cycle(tasks):
            self.logger.error(LogType.FLOW_STEP, "Learning", "DAG 存在循环依赖")
            return {}

        results: Dict[str, ToolResult] = {}
        pending = {t.id: t for t in tasks}
        completed: set = set()
        failed: set = set()

        async def emit(event: str, payload: Dict):
            if on_event:
                try:
                    await on_event({"event": event, "payload": payload})
                except Exception:
                    pass

        while pending:
            ready = [
                t for tid, t in pending.items()
                if all(d in completed or d in failed for d in t.depends_on)
            ]
            if not ready:
                self.logger.error(LogType.FLOW_STEP, "Learning",
                                  "DAG 死锁(无 ready 任务但仍有 pending)")
                break

            # 同 parallel_group 的 task 并行(各 group 内部并行,group 之间按依赖由入度决定)
            groups: Dict[Optional[str], List[ToolTask]] = {}
            for t in ready:
                groups.setdefault(t.parallel_group, []).append(t)

            for grp_id, grp_tasks in groups.items():
                await asyncio.gather(*[
                    self._run_task(t, results, completed, failed, pending, emit)
                    for t in grp_tasks
                ])

        self.logger.log_flow("Learning", f"DAG 完成: 成功 {len(completed)} / 失败 {len(failed)}")
        return results

    async def _run_task(
        self,
        task: ToolTask,
        results: Dict[str, ToolResult],
        completed: set,
        failed: set,
        pending: Dict[str, ToolTask],
        emit,
    ):
        resolved = resolve_params(task.params, results)
        await emit("tool_call", {
            "task_id": task.id,
            "tool": task.type,
            "params": resolved,
        })

        result = await self._run_with_retry_async(task.type, resolved, task.retry, task.timeout_s)
        results[task.id] = result

        if result.success:
            completed.add(task.id)
            await emit("tool_result", {
                "task_id": task.id,
                "tool": task.type,
                "data_preview": str(result.data)[:200] if result.data else "",
                "meta": result.meta,
            })
        else:
            failed.add(task.id)
            self.logger.error(LogType.TOOL_ERROR, "Learning",
                              f"task {task.id} 失败: {result.error}")
            await emit("tool_error", {
                "task_id": task.id,
                "tool": task.type,
                "error": result.error,
                "meta": result.meta,
            })

        pending.pop(task.id, None)

    # ----- 内部 -----
    def _run_with_retry_sync(self, tool_name: str, params: Dict, retry: int, timeout_s: int) -> ToolResult:
        last_err = None
        for attempt in range(retry + 1):
            try:
                tool = self.registry.get(tool_name)
                if not tool:
                    return ToolResult(success=False, error=f"未知工具: {tool_name}")
                future = self._executor.submit(self._safe_execute, tool, params)
                return future.result(timeout=timeout_s)
            except Exception as e:
                last_err = str(e)
                if attempt >= retry:
                    return ToolResult(success=False, error=last_err)
        return ToolResult(success=False, error=last_err or "unknown")

    async def _run_with_retry_async(self, tool_name: str, params: Dict, retry: int, timeout_s: int) -> ToolResult:
        loop = asyncio.get_running_loop()
        last_err = None
        for attempt in range(retry + 1):
            try:
                tool = self.registry.get(tool_name)
                if not tool:
                    return ToolResult(success=False, error=f"未知工具: {tool_name}")
                result = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, self._safe_execute, tool, params),
                    timeout=timeout_s,
                )
                return result
            except asyncio.TimeoutError:
                last_err = f"超时({timeout_s}s)"
            except Exception as e:
                last_err = str(e)
            if attempt < retry:
                self.logger.warning(LogType.TOOL_ERROR, "Learning",
                                    f"{tool_name} 第 {attempt+1} 次失败: {last_err},重试")
        return ToolResult(success=False, error=last_err or "unknown")

    @staticmethod
    def _safe_execute(tool, params: Dict) -> ToolResult:
        try:
            return tool.execute(**params)
        except Exception as e:
            return ToolResult(success=False, error=f"工具异常: {e}")


def _has_cycle(tasks: List[ToolTask]) -> bool:
    graph = {t.id: list(t.depends_on) for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def dfs(n):
        color[n] = GRAY
        for m in graph.get(n, []):
            if m not in color:
                continue
            if color[m] == GRAY:
                return True
            if color[m] == WHITE and dfs(m):
                return True
        color[n] = BLACK
        return False

    for n in graph:
        if color[n] == WHITE and dfs(n):
            return True
    return False