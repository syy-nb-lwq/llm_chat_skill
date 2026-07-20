"""Learning Agent - 流转中枢:执行工具、获取数据"""
import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from infra.logger import get_logger
from tools.base import ToolResult
from tools.hub import get_tool_hub


# 参数值类型(支持 ${task.data.x} 占位符)
ParamValue = Union[str, int, float, bool, list, dict, None]


@dataclass
class ToolTask:
    """工具任务"""
    id: str
    type: str
    params: Dict = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    parallel_group: Optional[str] = None
    retry: int = 0               # 失败后重试次数(0 = 不重试)
    timeout_s: int = 30          # 单次执行超时秒
    fallback_to: Optional[str] = None  # 失败时跳到的 task id


_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_value(value: Any, results: Dict[str, ToolResult],
                   user_input: Optional[Dict] = None) -> Any:
    """递归解析占位符。

    支持:
    - ${task.data.foo} / ${task.error} / ${task.success}
    - ${user_input} / ${user_input.city} ...

    失败或缺值的上游会解析为空字符串(整体字符串场景)。
    """
    if isinstance(value, str):
        def repl(m):
            path = m.group(1).strip()
            parts = path.split(".")
            if not parts:
                return ""
            root = parts[0]
            # user_input 引用
            if root == "user_input":
                if user_input is None:
                    return ""
                cur: Any = user_input
                for p in parts[1:]:
                    if isinstance(cur, dict):
                        cur = cur.get(p)
                    else:
                        cur = getattr(cur, p, None)
                    if cur is None:
                        return ""
                return "" if cur is None else str(cur)
            # 任务结果引用
            r = results.get(root)
            if r is None:
                return ""
            cur = r
            for p in parts[1:]:
                if p == "data":
                    if not r.success:
                        return ""
                    cur = r.data
                    continue
                if p == "error":
                    cur = getattr(r, "error", "")
                    continue
                if p == "success":
                    cur = getattr(r, "success", False)
                    continue
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    cur = getattr(cur, p, None)
                if cur is None:
                    return ""
            return "" if cur is None else str(cur)

        if "$" in value:
            return _VAR_RE.sub(repl, value)
        return value
    if isinstance(value, list):
        return [_resolve_value(v, results, user_input) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, results, user_input) for k, v in value.items()}
    return value


def resolve_params(params: Any, results: Dict[str, ToolResult],
                   user_input: Optional[Dict] = None) -> Any:
    """公开 API:解析整个 params 树中的占位符。"""
    return _resolve_value(params, results, user_input)


class LearningAgent:
    """Learning Agent - 流转中枢

    职责:
    1. 工具注册
    2. 工具调用
    3. 工具结果处理(含 DAG + 变量替换 + 重试/超时)
    """

    def __init__(self):
        self.logger = get_logger()
        self.hub = get_tool_hub()
        self.tools = self.hub  # 兼容旧字段名
        self.registry = self.hub  # 兼容旧字段名

    async def execute_tool(
        self,
        tool_type: str,
        params: Dict,
        on_event: Optional[Callable] = None,
        *,
        task_id: Optional[str] = None,
        retry: int = 0,
        timeout_s: int = 30,
        fallback_to: Optional[str] = None,
    ) -> ToolResult:
        """执行单个工具(支持重试 + 超时)

        会把实际执行信息(attempts / latency_ms / timeout_s / fallback_to)写入
        ``result.meta``,给 ExecutionCritic / 可观测前端使用。
        """
        import time
        tool_info = self.hub.get_tool(tool_type)
        if not tool_info:
            return ToolResult(success=False, error=f"工具不存在: {tool_type}")

        # 参数拷贝,避免污染调用方
        local_params = dict(params) if params else {}

        async def _run_once() -> ToolResult:
            self.logger.info(tool_type, f"执行工具: {local_params}")
            t0 = time.time()
            try:
                result = await asyncio.wait_for(
                    self.hub.call_tool(tool_type, local_params),
                    timeout=timeout_s,
                )
                result.meta = dict(result.meta or {})
                result.meta.setdefault("latency_ms", int((time.time() - t0) * 1000))
                result.meta.setdefault("timeout_s", timeout_s)
                if result.success:
                    self.logger.info(tool_type, "执行成功")
                else:
                    self.logger.warning(tool_type, f"执行失败: {result.error}")
                return result
            except asyncio.TimeoutError:
                return ToolResult(
                    success=False,
                    error=f"超时({timeout_s}s)",
                    meta={"timeout_s": timeout_s, "timed_out": True,
                          "latency_ms": int((time.time() - t0) * 1000)},
                )
            except Exception as e:
                return ToolResult(
                    success=False,
                    error=str(e),
                    meta={"timeout_s": timeout_s, "latency_ms": int((time.time() - t0) * 1000)},
                )

        attempts = max(1, retry + 1)
        last_err = "unknown"
        last_result: Optional[ToolResult] = None
        for attempt in range(1, attempts + 1):
            last_result = await _run_once()
            if last_result.success:
                # 回填 attempts / fallback_to / retry
                last_result.meta = dict(last_result.meta or {})
                last_result.meta["attempts"] = attempt
                last_result.meta["retry"] = retry
                if fallback_to:
                    last_result.meta["fallback_to"] = fallback_to
                if on_event:
                    await on_event("tool_result", {
                        "task_id": task_id or tool_type,
                        "tool": tool_type,
                        "data": last_result.data,
                        "meta": last_result.meta,
                    })
                return last_result
            last_err = last_result.error
            if attempt < attempts:
                self.logger.warning("Learning", f"{tool_type} 第 {attempt} 次失败: {last_err},重试")

        # 全部失败,记录 attempts / fallback_to
        if last_result is not None:
            last_result.meta = dict(last_result.meta or {})
            last_result.meta["attempts"] = attempts
            last_result.meta["retry"] = retry
            if fallback_to:
                last_result.meta["fallback_to"] = fallback_to
        if on_event:
            await on_event("tool_error", {
                "task_id": task_id or tool_type,
                "tool": tool_type,
                "error": last_err,
            })
        return last_result or ToolResult(success=False, error=last_err)

    async def execute_dag(
        self,
        tasks: List[ToolTask],
        on_event: Optional[Callable] = None,
        user_input: Optional[Dict] = None,
    ) -> Dict[str, ToolResult]:
        """DAG 执行

        - 自动拓扑调度,依赖完成才启动下游
        - 同 parallel_group 的任务同步启动
        - 支持变量替换(${task.data.x})
        - 失败时按 fallback 决定是否跳过下游
        """
        if not tasks:
            return {}

        if _has_cycle(tasks):
            self.logger.error("Learning", "DAG 存在循环依赖")
            if on_event:
                await on_event("error", {"message": "DAG 存在循环依赖"})
            return {}

        # 校验依赖完整性:找不到的依赖视为错误,不进入 pending
        all_ids = {t.id for t in tasks}
        for t in tasks:
            missing = [d for d in t.depends_on if d not in all_ids]
            if missing:
                self.logger.error("Learning", f"任务 {t.id} 缺少依赖: {missing}")

        # 只保留依赖完整的任务
        pending: Dict[str, ToolTask] = {
            t.id: t for t in tasks
            if all(d in all_ids for d in t.depends_on)
        }
        results: Dict[str, ToolResult] = {}
        running: Dict[str, asyncio.Task] = {}

        async def emit(event: str, payload: dict):
            if on_event:
                await on_event(event, payload)

        # 计算每个任务"上游失败时仍可启动"的依赖集合。
# 默认严格: 上游失败 → 下游被跳过。
# 例外: 上游 task.fallback_to == 本任务 id → 即使上游失败也允许启动(接管)。
        fallback_allow: Dict[str, set] = {}
        for t in pending.values():
            allowed: set = set()
            for d in t.depends_on:
                src = next((x for x in pending.values() if x.id == d), None)
                if src is not None and getattr(src, "fallback_to", None) == t.id:
                    allowed.add(d)
            fallback_allow[t.id] = allowed

        while pending or running:
            # 找出可启动的任务:依赖都完成 + 失败依赖在 allowed 集合里
            for task_id, task in list(pending.items()):
                deps_ok = True
                for d in task.depends_on:
                    r = results.get(d)
                    if r is None:
                        deps_ok = False
                        break
                    if not r.success and d not in fallback_allow[task_id]:
                        deps_ok = False
                        break
                if deps_ok:
                    # 解析参数变量
                    resolved = resolve_params(task.params, results)
                    del pending[task_id]
                    if on_event:
                        await on_event("tool_call", {
                            "task_id": task.id,
                            "tool": task.type,
                            "params": resolved,
                            "depends_on": task.depends_on,
                            "parallel_group": task.parallel_group,
                        })
                    coro = self.execute_tool(
                        task.type,
                        resolved,
                        emit,
                        task_id=task.id,
                        retry=task.retry,
                        timeout_s=task.timeout_s,
                        fallback_to=task.fallback_to,
                    )
                    running[task_id] = asyncio.create_task(coro)

            if not running and pending:
                # 剩余任务因依赖失败而卡住 → 标记为 skipped
                for task_id in list(pending.keys()):
                    task = pending[task_id]
                    skipped = ToolResult(
                        success=False,
                        error="skipped due to upstream failure",
                    )
                    results[task_id] = skipped
                    if on_event:
                        await on_event("tool_error", {
                            "task_id": task_id,
                            "tool": task.type,
                            "error": "skipped due to upstream failure",
                        })
                    del pending[task_id]
                break

            if running:
                done, _ = await asyncio.wait(
                    running.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for finished in done:
                    for task_id, task_obj in list(running.items()):
                        if task_obj is finished:
                            try:
                                results[task_id] = await task_obj
                            except Exception as e:
                                results[task_id] = ToolResult(success=False, error=str(e))
                            del running[task_id]
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
