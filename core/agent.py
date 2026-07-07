"""Agent 核心 - 多智能体协作(异步版 + DAG)"""
import time
from typing import Callable, Dict, Optional

from agents.learning import ToolTask
from agents.manager import ManagerAgent
from agents.orchestrator import OrchestratorAgent
from agents.learning import LearningAgent
from agents.skill_trainer import SkillTrainer
from core.context import Context
from core.dag import DAGExecutor
from infra.config import config
from infra.logger import get_logger, LogType


class Agent:
    """多智能体协作入口。

    流程:
    ┌──────────────────────────────────────────────────────┐
    │ 1. 直接回答? → Orchestrator 流式输出                  │
    │ 2. Manager.plan → {intent, skill, tool_tasks(DAG)}    │
    │ 3. Learning.execute_dag → 拓扑序/并行/重试/超时       │
    │ 4. Orchestrator.orchestrate(stream) → 流式输出       │
    └──────────────────────────────────────────────────────┘
    """

    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self.logger = get_logger()

        self.manager = ManagerAgent()
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.dag = DAGExecutor(self.learning)

        self.context = Context()
        self.dag_enabled = bool(config.skill_dag_enabled)

    async def handle(
        self,
        user_input: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> str:
        import time as _t
        start = _t.time()
        self.logger.start_trace(f"turn-{self.session_id}")

        self.context.add_user_message(user_input)
        self.logger.log_data("Agent", "in", "user_input", user_input)
        self.logger.log_flow("Agent", "开始处理用户请求")

        def emit(event: str, payload: dict):
            self.logger.info(LogType.FLOW_STEP, "Agent", f"event: {event}", payload)
            if on_event:
                try:
                    on_event({"event": event, "payload": payload})
                except Exception:
                    pass

        try:
            # ----- 0. 教导意图优先(P3 闭环) -----
            if self.trainer._heuristic_teaching(user_input):
                emit("thinking", {"stage": "teaching_detect"})
                ok, msg, skill = await self.trainer.teach(user_input)
                if ok and skill:
                    emit("skill_learned", {
                        "name": skill.name,
                        "version": skill.version,
                        "capability": skill.capability,
                        "patterns": skill.patterns,
                        "step_count": len(skill.steps),
                        "message": msg,
                    })
                    self.context.add_assistant_message(msg)
                    emit("message_final", {"content": msg})
                    return msg
                else:
                    # 启发式命中但 LLM 判定不是教导,降级走普通路径
                    self.logger.info(LogType.FLOW_STEP, "Agent",
                                     f"教导误判,降级: {msg}")

            # ----- 1. 直接回答 -----
            if self.manager.should_answer_directly(user_input):
                emit("thinking", {"stage": "direct_answer"})
                buf, ans = [], ""
                async for d in self.orchestrator.stream(user_input, {}, None):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
                ans = "".join(buf)
                self.context.add_assistant_message(ans)
                emit("message_final", {"content": ans})
                return ans

            # ----- 2. 规划 -----
            emit("thinking", {"stage": "planning"})
            plan = await self.manager.plan(user_input)
            emit("plan", {
                "intent": plan.intent,
                "skill": plan.selected_skill.name if plan.selected_skill else None,
                "tasks": [
                    {"id": t.get("id"), "type": t.get("type"), "params": t.get("params"),
                     "depends_on": t.get("depends_on", [])}
                    for t in plan.tool_tasks
                ],
            })

            # ----- 3. 工具执行(DAG) -----
            tool_results: Dict = {}
            if plan.tool_tasks:
                emit("thinking", {"stage": "tools_running", "count": len(plan.tool_tasks)})
                if self.dag_enabled and plan.selected_skill and plan.selected_skill.has_structured_steps():
                    # 走 Skill DAG
                    emit("thinking", {"stage": "dag_mode", "skill": plan.selected_skill.name})

                    async def dag_event(payload_with_event):
                        # DAG emit 的是 {event, payload} 格式,直接转发
                        emit(payload_with_event["event"], payload_with_event["payload"])

                    tool_results = await self.dag.run_skill(
                        plan.selected_skill, user_input, on_event=dag_event
                    )
                else:
                    # 走 Manager 输出的 DAG(dict → ToolTask)
                    tasks = [
                        ToolTask(
                            id=t.get("id") or f"t{i+1}",
                            type=t.get("type", ""),
                            params=t.get("params", {}),
                            depends_on=t.get("depends_on", []) or [],
                            parallel_group=t.get("parallel_group"),
                        )
                        for i, t in enumerate(plan.tool_tasks)
                    ]

                    async def on_dag_event(payload_with_event):
                        emit(payload_with_event["event"], payload_with_event["payload"])

                    tool_results = await self.learning.execute_dag(tasks, on_dag_event)

            # ----- 4. 整合回答(流式) -----
            emit("thinking", {"stage": "synthesizing"})
            buf = []
            try:
                async for d in self.orchestrator.stream(
                    user_input, tool_results, plan.selected_skill
                ):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
            except Exception:
                # 流式失败 → 整体
                ans = await self.orchestrator.orchestrate(
                    user_input, tool_results, plan.selected_skill
                )
                buf = [ans]
                emit("message_delta", {"delta": ans})

            ans = "".join(buf)
            self.context.add_assistant_message(ans)
            emit("message_final", {"content": ans})

            duration = (time.time() - start) * 1000
            self.logger.log_data("Agent", "out", "response",
                                 ans[:200] + "..." if len(ans) > 200 else ans)
            self.logger.log_flow("Agent", f"处理完成,耗时 {duration:.0f}ms")
            return ans

        except Exception as e:
            self.logger.error(LogType.FLOW_STEP, "Agent", f"处理失败: {e}")
            emit("error", {"message": str(e)})
            raise
        finally:
            self.logger.end_trace()

    def reset(self):
        self.context.clear()
        self.logger.log_flow("Agent", "对话上下文已重置")

    # ----- 同步兼容 -----
    def chat(self, user_input: str, callback=None) -> str:
        import asyncio
        async def _run():
            return await self.handle(user_input)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    return ex.submit(asyncio.run, _run()).result()
            return loop.run_until_complete(_run())
        except RuntimeError:
            return asyncio.run(_run())