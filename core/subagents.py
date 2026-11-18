"""子智能体 —— 让主智能体把互不依赖的子目标派出去并行做。

## 为什么需要子智能体（而不是主智能体自己排队做）

1. **并行**：三件互不依赖的事，串行 3×T，并行约为 T
2. **上下文隔离**：子任务的过程（试错、失败重来、一堆工具日志）
   不该污染主对话上下文 —— 主智能体只需要结论
3. **可组合**：一个子智能体失败不影响其他，主智能体可以只重做失败的那个

## 四个必须防住的东西（这是本模块的核心，不是优化）

一个能"复制自己"的系统，出错方式比单线程严重得多：

| 风险 | 防护 |
|---|---|
| 递归爆炸 | `max_depth`（默认 1）：子智能体不能再派子智能体 |
| 并发风暴 | `max_concurrent`（默认 3）：信号量限流 |
| 卡死 | 每个子任务 `timeout_s`（默认 90）：超时标记 `timeout` 并回收 |
| **失败被静默吞掉** | 结果里强制带 `done/failed/timeout` 计数，主智能体据此如实汇报 |

最后一条最要紧：不能出现"三个子任务两个失败，但最终回答说得像全成了"。

## 子智能体不做什么

- **不生成面向用户的回答** —— 只产出事实（findings），语言由主智能体统一组织
- **不写长期记忆** —— 记忆只由主智能体写，避免并发污染用户画像
- **不派更深的任务** —— 到深度上限后 `run_subagents` 从工具表里摘掉
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from contextvars import ContextVar

from infra.logger import get_logger


#: 当前这一轮是**哪个用户**在问 —— 子智能体要拿它去取用户画像。
#:
#: 为什么不一路传参：`run_subagents` 是工具，工具签名里塞 user_id 会很脏；
#: 而 `contextvars` 在 `asyncio.create_task()` 时会复制上下文，
#: 所以 gather 出来的子智能体任务能自动读到，正好合用。
_current_user: ContextVar[str] = ContextVar("dsh_subagent_user", default="")


def set_current_user(user_id: str) -> None:
    """一轮开始时由 Agent 调用。"""
    _current_user.set(user_id or "")


def get_current_user() -> str:
    return _current_user.get() or ""


#: 当前这一轮的**对话上下文** —— 子智能体必须能看到，否则它不知道在聊什么。
#:
#: 实机踩过：用户说"发"，主智能体知道指的是刚才那个视频任务；
#: 子智能体只拿到"提交视频生成任务"这一句，既不知道是哪个视频，
#: 也不知道前面已经建了哪些文件、试过哪些接口 —— 只能从头猜。
#:
#: 同样用 contextvars 传递（asyncio 任务创建时会复制上下文）。
_current_context: ContextVar[Any] = ContextVar("dsh_subagent_ctx", default=None)


def set_current_context(ctx: Any) -> None:
    """一轮开始时由 Agent 调用。"""
    _current_context.set(ctx)


def get_current_context() -> Any:
    return _current_context.get()


# ===== 状态 =====

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"


#: 子智能体默认限制
DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_TIMEOUT_S = 90
#: 子智能体自己的工具轮次（比主智能体少，够用即可）
DEFAULT_SUBAGENT_ROUNDS = 2


@dataclass
class SubAgentTask:
    """一个子任务的状态与结果。"""

    goal: str
    name: str = ""
    id: str = ""
    status: str = STATUS_PENDING
    depth: int = 0
    #: 结构化结论（给主智能体看的"事实"，不是给用户的回答）
    result: str = ""
    error: str = ""
    #: 调过哪些工具（可观测）
    tool_calls: List[str] = field(default_factory=list)
    #: 最近在做什么（进度，供主智能体探测）
    progress: str = ""
    rounds: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = f"sub_{uuid.uuid4().hex[:8]}"
        if not self.name:
            # 用目标前 12 字当名字，方便日志和前端辨认
            self.name = (self.goal or "子任务")[:12]

    @property
    def duration_ms(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return (end - self.started_at) * 1000

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "status": self.status,
            "depth": self.depth,
            "result": self.result,
            "error": self.error,
            "tool_calls": list(self.tool_calls),
            "progress": self.progress,
            "rounds": self.rounds,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class SubAgentReport:
    """一批子任务的整体结果(给主智能体汇总用)。"""

    tasks: List[SubAgentTask] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for t in self.tasks if t.status == STATUS_DONE)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tasks if t.status == STATUS_FAILED)

    @property
    def timeout(self) -> int:
        return sum(1 for t in self.tasks if t.status == STATUS_TIMEOUT)

    @property
    def all_ok(self) -> bool:
        return bool(self.tasks) and self.done == len(self.tasks)

    def summary_line(self) -> str:
        """一行统计 —— **失败必须出现在这一行里**,不许被吞掉。"""
        return (
            f"共 {len(self.tasks)} 个子任务："
            f"成功 {self.done}，失败 {self.failed}，超时 {self.timeout}"
        )

    def as_text(self) -> str:
        """给主智能体读的汇总文本。"""
        parts = [self.summary_line()]
        for t in self.tasks:
            head = f"\n### {t.name}（{t.status}"
            if t.duration_ms:
                head += f"，{t.duration_ms:.0f}ms"
            head += "）"
            parts.append(head)
            parts.append(f"目标：{t.goal}")
            if t.tool_calls:
                parts.append(f"用过的工具：{', '.join(t.tool_calls)}")
            if t.status == STATUS_DONE:
                parts.append(f"结论：{t.result or '（无内容）'}")
            else:
                parts.append(f"失败原因：{t.error or '未知'}")
        if not self.all_ok:
            parts.append(
                "\n注意：有子任务没有成功。回答时必须如实说明哪些没做成、"
                "以及原因，不要把失败说成成功。"
            )
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": len(self.tasks),
            "done": self.done,
            "failed": self.failed,
            "timeout": self.timeout,
            "all_ok": self.all_ok,
            "tasks": [t.to_dict() for t in self.tasks],
        }


class SubAgentPool:
    """子智能体池：负责并发、深度、超时，并提供状态查询。

    它**不关心子智能体内部怎么干活** —— 那是 `_run_one` 的事。
    这里只做调度和边界。
    """

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_depth: int = DEFAULT_MAX_DEPTH,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_rounds: int = DEFAULT_SUBAGENT_ROUNDS,
    ):
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_depth = max(0, int(max_depth))
        self.timeout_s = float(timeout_s)
        self.max_rounds = max(1, int(max_rounds))
        self.logger = get_logger()
        #: id -> 任务(运行中也能查状态)
        self._tasks: Dict[str, SubAgentTask] = {}

    # ===== 状态查询 =====

    def get(self, task_id: str) -> Optional[SubAgentTask]:
        return self._tasks.get(task_id)

    def status(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self._tasks.values()]

    def active_count(self) -> int:
        return sum(
            1 for t in self._tasks.values()
            if t.status in (STATUS_PENDING, STATUS_RUNNING)
        )

    # ===== 调度 =====

    async def run_many(
        self,
        specs: List[Dict[str, Any]],
        *,
        depth: int = 0,
        on_event: Optional[Callable] = None,
    ) -> SubAgentReport:
        """并行跑一批子任务。

        Args:
            specs: `[{"goal": "...", "name": "可选"}]`
            depth: 当前深度（子智能体调用时是 1，到上限就不再允许派）
            on_event: 进度回调 `await on_event(event, payload)`
        """
        if depth > self.max_depth:
            # 到深度上限：直接拒绝，而不是悄悄降级 —— 让主智能体知道原因
            report = SubAgentReport()
            for spec in specs:
                t = SubAgentTask(
                    goal=str((spec or {}).get("goal") or ""),
                    name=str((spec or {}).get("name") or ""),
                    depth=depth,
                )
                t.status = STATUS_FAILED
                t.error = f"已达到子智能体深度上限（max_depth={self.max_depth}），不能再派"
                report.tasks.append(t)
            return report

        specs = [s for s in (specs or []) if isinstance(s, dict) and str(s.get("goal") or "").strip()]
        if not specs:
            return SubAgentReport()

        sem = asyncio.Semaphore(self.max_concurrent)
        tasks: List[SubAgentTask] = []
        for spec in specs:
            t = SubAgentTask(
                goal=str(spec.get("goal") or "").strip(),
                name=str(spec.get("name") or "").strip(),
                depth=depth,
            )
            self._tasks[t.id] = t
            tasks.append(t)

        async def _worker(task: SubAgentTask):
            async with sem:
                await self._run_one(task, on_event=on_event)

        await asyncio.gather(*(_worker(t) for t in tasks), return_exceptions=True)
        return SubAgentReport(tasks=tasks)

    async def _run_one(self, task: SubAgentTask, *, on_event=None) -> None:
        """跑单个子智能体（带超时保护）。"""
        task.status = STATUS_RUNNING
        task.started_at = time.time()
        task.progress = "启动"
        await self._emit(on_event, "subagent_spawned", task)

        try:
            await asyncio.wait_for(
                self._execute(task, on_event=on_event),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            task.status = STATUS_TIMEOUT
            task.error = f"超过 {self.timeout_s:.0f}s 未完成"
            task.progress = "超时"
        except asyncio.CancelledError:
            task.status = STATUS_FAILED
            task.error = "被取消"
            raise
        except Exception as exc:
            task.status = STATUS_FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            task.progress = "异常退出"
            self.logger.warning("SubAgent", f"{task.name} 失败: {exc}")
        finally:
            task.finished_at = time.time()
            await self._emit(on_event, "subagent_done", task)

    async def _execute(self, task: SubAgentTask, *, on_event=None) -> None:
        """子智能体主循环：规划 → 执行工具 → 多轮，直到完成或用尽轮次。"""
        from agents.learning import LearningAgent
        from agents.manager import ManagerAgent

        manager = ManagerAgent()
        learning = LearningAgent()

        # 子智能体**不能**再派子智能体（深度已到上限时要把它从工具表里摘掉）
        blocked_tools = {"run_subagents", "subagent_status"} if task.depth >= self.max_depth else set()

        findings: List[str] = []
        last_action_sig = ""
        goal_text = task.goal

        for round_no in range(1, self.max_rounds + 1):
            task.rounds = round_no
            task.progress = f"第 {round_no} 轮规划"
            await self._emit(on_event, "subagent_progress", task)

            # 把已有发现拼进目标，让模型知道已经查到什么
            enriched = goal_text
            if findings:
                enriched = goal_text + "\n\n已完成的部分：\n" + "\n".join(findings)

            # **带用户画像** —— 用户的长期习惯必须影响子智能体怎么做事。
            # 不传的话，子智能体就是个"没人格的执行器"，
            # 和"结合使用者习惯"的定位不一致。
            profile_block = ""
            uid = get_current_user()
            if uid:
                try:
                    profile_block = manager.profile_block(uid)
                except Exception:
                    profile_block = ""

            try:
                # **带上主对话上下文** —— 用户说"发"，子智能体得知道指的是什么。
                # Context 对象由规划层自己按 token 预算截断，这里不用额外裁剪。
                plan = await manager.plan(
                    enriched,
                    get_current_context(),
                    user_id=uid,
                    profile_block=profile_block,
                )
            except Exception as exc:
                task.error = f"规划失败: {exc}"
                task.status = STATUS_FAILED
                return

            tasks = list(getattr(plan, "tool_tasks", []) or [])
            # 过滤越权/递归工具
            tasks = [t for t in tasks if (t or {}).get("type") not in blocked_tools]

            if not tasks:
                # 没有要调的工具 —— 说明它认为可以直接得出结论
                task.status = STATUS_DONE
                task.result = self._compose(findings) or (plan.intent_detail if hasattr(plan, "intent_detail") else "") or "（子任务未产生可用结论）"
                return

            # **无进展检测**：动作签名和上一轮完全相同 → 立刻停，别原地打转
            sig = self._action_signature(tasks)
            if sig == last_action_sig:
                task.status = STATUS_FAILED
                task.error = "连续两轮动作完全相同（原地打转），已停止"
                task.result = self._compose(findings)
                return
            last_action_sig = sig

            task.progress = f"第 {round_no} 轮执行 {len(tasks)} 个工具"
            await self._emit(on_event, "subagent_progress", task)

            # **必须转成 ToolTask**：`plan.tool_tasks` 是 dict，
            # 而 `execute_dag()` 要的是 ToolTask 对象。
            # 传错会报 `'dict' object has no attribute 'id'` —— 实机踩过。
            from agents.learning import ToolTask

            planned: List[ToolTask] = []
            for i, t in enumerate(tasks):
                if not isinstance(t, dict):
                    continue
                planned.append(ToolTask(
                    id=str(t.get("id") or f"t{i + 1}"),
                    type=str(t.get("type") or ""),
                    params=t.get("params") or {},
                    depends_on=list(t.get("depends_on") or []),
                    retry=int(t.get("retry", 0) or 0),
                    timeout_s=int(t.get("timeout_s", 30) or 30),
                    fallback_to=t.get("fallback_to"),
                ))
            if not planned:
                task.status = STATUS_DONE
                task.result = self._compose(findings)
                return

            try:
                results = await learning.execute_dag(planned)
            except Exception as exc:
                task.error = f"工具执行失败: {exc}"
                task.status = STATUS_FAILED
                return

            for tid, res in (results or {}).items():
                ttype = next(
                    (t.get("type") for t in tasks if (t or {}).get("id") == tid), tid
                )
                if ttype and ttype not in task.tool_calls:
                    task.tool_calls.append(ttype)
                if getattr(res, "success", False):
                    findings.append(self._summarize_result(ttype, res))

            # 有成功结果就先当作可以收尾；再跑一轮只有在还有没做完的事时才值
            if findings:
                task.status = STATUS_DONE
                task.result = self._compose(findings)
                return

        # 轮次用尽
        task.status = STATUS_DONE if findings else STATUS_FAILED
        task.result = self._compose(findings)
        if not findings and not task.error:
            task.error = f"{self.max_rounds} 轮内没拿到任何有效结果"

    # ===== 内部 =====

    async def _emit(self, on_event, event: str, task: SubAgentTask) -> None:
        if not on_event:
            return
        try:
            await on_event(event, task.to_dict())
        except Exception:
            pass

    @staticmethod
    def _action_signature(tasks: List[Dict[str, Any]]) -> str:
        """动作签名：工具名 + 参数（排序后），用来判断是不是在重复同一件事。"""
        import json

        parts = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            parts.append(
                f"{t.get('type')}::{json.dumps(t.get('params') or {}, sort_keys=True, ensure_ascii=False)}"
            )
        return "|".join(sorted(parts))

    @staticmethod
    def _summarize_result(tool: str, result) -> str:
        """把工具结果压成一行结论（子智能体只回结论，不回原始数据）。"""
        data = getattr(result, "data", None)
        try:
            import json

            blob = json.dumps(data, ensure_ascii=False)
        except Exception:
            blob = str(data)
        if len(blob) > 500:
            blob = blob[:500] + "…"
        return f"- {tool}: {blob}"

    @staticmethod
    def _compose(findings: List[str]) -> str:
        if not findings:
            return ""
        return "查到的事实：\n" + "\n".join(findings)


# ===== 全局单例（主智能体用） =====

_pool: Optional[SubAgentPool] = None


def get_subagent_pool() -> SubAgentPool:
    global _pool
    if _pool is None:
        _pool = SubAgentPool()
    return _pool


def reset_subagent_pool() -> None:
    global _pool
    _pool = None
