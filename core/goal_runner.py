"""目标循环 —— 把「一轮问答」变成「把目标做完」。

## 问题

`Agent.handle()` 原来是一条直线：规划一次 → 执行一次 → 生成回答 → 结束。
所以「造个工具、用它、再拿结果算东西」这种任务，只要第一次规划没把全部步骤
都想清楚，就没有第二次机会 —— **一轮就完了**。

## 这个模块做什么

```
round = 1
while round <= max_rounds:
    plan = 规划(目标, 已知结果)
    执行 plan 里的工具 → results
    findings += results
    if plan.goal_done:        # ← 规划层明确说"够了"
        stop("goal_done")
    if 没有任何新结果:          # ← 防原地打转
        stop("no_progress")
    if 超出预算:                # ← 防烧钱
        stop("budget")
    round += 1
```

## 三个关键设计

### 1. 循环由**规划层显式驱动**，不是自动无限跑

规划层每次输出 `goal_done`（布尔）。**默认 True** —— 所以
「你好」「算 1+1」这类请求行为和以前完全一样，永远只跑一轮，不会变慢。

只有当规划层说"还没完"（并给出 `remaining_note` 说清还差什么）时，
才会进入第二轮。

### 2. 无进展检测（最容易出事的地方）

模型可能连续几轮做**同一件无效的事**：同一个工具、同样的参数、同样的失败。
所以每轮记录「动作签名」（工具名 + 参数哈希），和上一轮完全相同就立刻停，
而不是把 6 轮预算全烧在重复动作上。

### 3. 停止原因必须可解释

停下来时要能说清为什么：`goal_done` / `max_rounds` / `budget` /
`no_progress` / `clarify` / `loop_disabled`。
这决定最终回答里那句"没做完，因为…"怎么写 —— 不能让用户以为全都做成了。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.events import (
    EV_GOAL_CONTINUE,
    EV_GOAL_PREP_ONLY,
    EV_GOAL_ROUND,
    EV_GOAL_STOP,
)
from infra.logger import get_logger
from core.exception_policy import downgrade


#: 停止原因
STOP_GOAL_DONE = "goal_done"
STOP_MAX_ROUNDS = "max_rounds"
STOP_BUDGET = "budget"
STOP_NO_PROGRESS = "no_progress"
STOP_CLARIFY = "clarify"
STOP_DISABLED = "loop_disabled"
STOP_ERROR = "error"

#: 停止原因 → 给用户看的说法
STOP_MESSAGES = {
    STOP_GOAL_DONE: "目标已达成",
    STOP_MAX_ROUNDS: "轮次预算用尽，还有没做完的部分",
    STOP_BUDGET: "总时长预算用尽，还有没做完的部分",
    STOP_NO_PROGRESS: "连续两轮做的是同一件事且没有新结果，判定这条路走不通",
    STOP_CLARIFY: "中途发现缺关键信息，需要你补充",
    STOP_DISABLED: "目标循环未启用",
    STOP_ERROR: "循环过程中出错",
}


@dataclass
class GoalResult:
    """一次目标循环的产出。"""

    tool_results: Dict[str, Any] = field(default_factory=dict)
    rounds: int = 0
    stop_reason: str = STOP_GOAL_DONE
    findings: str = ""
    elapsed_ms: float = 0.0
    #: 每轮的简要记录(可观测)
    round_log: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.stop_reason == STOP_GOAL_DONE

    @property
    def explanation(self) -> str:
        return STOP_MESSAGES.get(self.stop_reason, self.stop_reason)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rounds": self.rounds,
            "stop_reason": self.stop_reason,
            "completed": self.completed,
            "explanation": self.explanation,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "tool_count": len(self.tool_results),
            "round_log": self.round_log,
        }


#: 只有"准备"作用的工具 —— 它们跑完不等于任务完成
#:
#: 实机踩过：用户说「先造一个算 BMI 的工具，**然后用它**算一下 1.75/70」，
#: 规划层造完工具就判定 `goal_done=true`，于是只跑了一轮，
#: 用户永远拿不到计算结果。
#:
#: 模型对"完成"的判断在**准备型动作**上系统性偏乐观，所以这里不靠它，
#: 用一条确定性规则兜住：只是准备好了东西、还有轮次，就再跑一轮。
PREPARATORY_TOOLS = {
    "create_tool",
    "write_script",
    "list_scripts",
    "list_created_tools",
    "subagent_status",
}


def is_only_preparatory(tool_tasks: List[Dict[str, Any]]) -> bool:
    """这一轮是不是**只做了准备**（造工具/存脚本），没有任何实质动作。"""
    types = {
        str((t or {}).get("type") or "").strip()
        for t in (tool_tasks or [])
        if isinstance(t, dict)
    }
    types.discard("")
    if not types:
        return False
    return types.issubset(PREPARATORY_TOOLS)


def action_signature(tool_tasks: List[Dict[str, Any]]) -> str:
    """动作签名：工具名 + 参数（排序后）。

    用来判断"这一轮和上一轮是不是在做同一件事"。
    """
    parts = []
    for t in tool_tasks or []:
        if not isinstance(t, dict):
            continue
        try:
            params = json.dumps(t.get("params") or {}, sort_keys=True, ensure_ascii=False)
        except Exception:
            params = str(t.get("params"))
        parts.append(f"{t.get('type')}::{params}")
    return "|".join(sorted(parts))


def summarize_results(tool_results: Dict[str, Any], max_chars_per_tool: int = 600) -> str:
    """把一轮工具结果压成"已知信息"，喂给下一轮规划。

    太长会把提示词挤爆，所以每个工具截断；失败的工具也要写进去
    （"这个办法不通"本身就是重要信息）。
    """
    if not tool_results:
        return ""
    lines = []
    for tid, res in tool_results.items():
        ok = getattr(res, "success", False)
        data = getattr(res, "data", None)
        err = getattr(res, "error", "") or ""
        try:
            blob = json.dumps(data, ensure_ascii=False) if data is not None else ""
        except Exception:
            blob = str(data)
        if len(blob) > max_chars_per_tool:
            blob = blob[:max_chars_per_tool] + "…"
        if ok:
            lines.append(f"- [{tid}] 成功：{blob or '(无数据)'}")
        else:
            lines.append(f"- [{tid}] 失败：{err[:300] or '(未知原因)'}")
    return "\n".join(lines)


def merge_results(target: Dict[str, Any], new: Dict[str, Any]) -> None:
    """把新一轮结果并进总结果（同 id 覆盖，避免 id 冲突丢数据）。"""
    for k, v in (new or {}).items():
        key = k
        n = 2
        while key in target and target[key] is not v:
            key = f"{k}#{n}"
            n += 1
        target[key] = v


async def run_goal_loop(
    *,
    first_plan,
    plan_fn: Callable[[str], Awaitable[Any]],
    execute_fn: Callable[[Any], Awaitable[Dict[str, Any]]],
    emit: Optional[Callable[[str, dict], Awaitable[None]]] = None,
    enabled: bool = True,
    max_rounds: int = 6,
    budget_s: float = 180.0,
    is_clarify: Optional[Callable[[Any], bool]] = None,
) -> GoalResult:
    """跑目标循环。

    Args:
        first_plan: 已经拿到的第一轮计划（避免重复规划）
        plan_fn: `async (prior_findings) -> PlanResult`，后续轮重新规划
        execute_fn: `async (plan) -> Dict[str, ToolResult]`，执行一轮工具
        emit: 事件回调
        enabled: 关掉就只跑一轮（和旧行为完全一致）
        max_rounds: 轮次上限
        budget_s: 总时长预算
        is_clarify: 判断计划是不是"需要澄清"（命中就停）
    """
    logger = get_logger()
    started = time.perf_counter()
    result = GoalResult()

    async def _emit(event: str, payload: dict) -> None:
        if emit is None:
            return
        try:
            await emit(event, payload)
        except Exception as exc:
            downgrade(exc, where='core/goal_runner.py')
    if not enabled:
        result.stop_reason = STOP_DISABLED
        try:
            results = await execute_fn(first_plan)
        except Exception as exc:
            result.stop_reason = STOP_ERROR
            logger.warning("GoalLoop", f"执行失败: {exc}")
            results = {}
        merge_results(result.tool_results, results)
        result.rounds = 1
        result.findings = summarize_results(results)
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        result.round_log.append({"round": 1, "stop": STOP_DISABLED})
        return result

    plan = first_plan
    last_sig: Optional[str] = None
    prior_findings = ""

    for round_no in range(1, max(1, int(max_rounds)) + 1):
        result.rounds = round_no
        await _emit(EV_GOAL_ROUND, {
            "round": round_no,
            "max_rounds": max_rounds,
            "plan_detail": getattr(plan, "intent_detail", "") or "",
        })

        try:
            results = await execute_fn(plan)
        except Exception as exc:
            logger.warning("GoalLoop", f"第 {round_no} 轮执行失败: {exc}")
            result.stop_reason = STOP_ERROR
            break

        merge_results(result.tool_results, results)
        round_findings = summarize_results(results)

        # 无进展检测：动作相同 **且** 没有新结果 → 停下。
        # `and not round_findings` 是刻意的：同样的动作若每轮产生不同结果
        # （如同一查询返回不同数据、同一脚本产出不同文件），是合法的多轮进展，
        # 不能因为动作签名相同就提前停。只有动作相同、结果也为空才是真打转。
        # 见 tests/test_goal_and_subagents.py 里依赖多轮的同动作场景。
        sig = action_signature(getattr(plan, "tool_tasks", []) or [])
        if sig and sig == last_sig and not round_findings:
            result.stop_reason = STOP_NO_PROGRESS
            result.round_log.append({"round": round_no, "stop": STOP_NO_PROGRESS})
            logger.info("GoalLoop", "判定原地打转，停止")
            break
        last_sig = sig

        if round_findings:
            prior_findings = (prior_findings + "\n" + round_findings).strip()

        # --- 是否继续 ---
        goal_done = bool(getattr(plan, "goal_done", True))
        tasks = list(getattr(plan, "tool_tasks", []) or [])

        # 兜底：只做了"准备"（造工具/存脚本），却被判成完成。
        # 模型在这类动作上系统性偏乐观，不能只靠它的判断。
        if (
            goal_done
            and tasks
            and is_only_preparatory(tasks)
            and round_no < max_rounds
        ):
            goal_done = False
            await _emit(EV_GOAL_PREP_ONLY, {
                "round": round_no,
                "tools": [str((t or {}).get("type")) for t in tasks],
                "reason": "这一轮只是准备好了工具/脚本,还没用它完成任务",
            })

        if goal_done or not tasks:
            result.stop_reason = STOP_GOAL_DONE
            result.round_log.append({"round": round_no, "stop": STOP_GOAL_DONE})
            break
        if round_no >= max_rounds:
            result.stop_reason = STOP_MAX_ROUNDS
            result.round_log.append({"round": round_no, "stop": STOP_MAX_ROUNDS})
            break
        if (time.perf_counter() - started) > budget_s:
            result.stop_reason = STOP_BUDGET
            result.round_log.append({"round": round_no, "stop": STOP_BUDGET})
            break

        # 还没完 → 带着已知结果再规划一轮
        remaining = (getattr(plan, "remaining_note", "") or "").strip()
        await _emit(EV_GOAL_CONTINUE, {
            "round": round_no,
            "next_round": round_no + 1,
            "remaining": remaining,
        })
        result.round_log.append({
            "round": round_no,
            "stop": "",
            "remaining": remaining,
        })

        try:
            plan = await plan_fn(prior_findings)
        except Exception as exc:
            logger.warning("GoalLoop", f"第 {round_no + 1} 轮规划失败: {exc}")
            result.stop_reason = STOP_ERROR
            break

        if is_clarify is not None and is_clarify(plan):
            result.stop_reason = STOP_CLARIFY
            break

    result.findings = prior_findings
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    await _emit(EV_GOAL_STOP, result.to_dict())
    return result
