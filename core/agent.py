"""Agent 核心 - 多智能体协作"""
import time
from typing import Callable, Dict, Optional

from agents.manager import ManagerAgent
from agents.orchestrator import OrchestratorAgent
from agents.learning import LearningAgent, ToolTask
from agents.skill_trainer import SkillTrainer
from core.context import Context
from core.dag import DAGExecutor
from infra.config import config
from infra.logger import get_logger


class Agent:
    """多智能体协作入口"""

    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self.logger = get_logger()

        self.manager = ManagerAgent()
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.dag = DAGExecutor(self.learning)
        self.trainer = SkillTrainer()

        self.context = Context()
        self.dag_enabled = bool(config.skill_dag_enabled)

    async def handle(
        self,
        user_input: str,
        on_event: Optional[Callable] = None,
    ) -> str:
        start = time.time()
        
        self.context.add_user_message(user_input)
        self.logger.info("Agent", f"INPUT: {user_input[:50]}")

        def emit(event: str, payload: dict):
            if on_event:
                try:
                    import asyncio
                    if asyncio.iscoroutinefunction(on_event):
                        asyncio.create_task(on_event(event, payload))
                    else:
                        on_event(event, payload)
                except RuntimeError:
                    pass
                except Exception:
                    pass

        try:
            # 教导意图检测
            if self.trainer._heuristic_teaching(user_input):
                emit("thinking", {"stage": "teaching_detect"})
                ok, msg, skill = await self.trainer.teach(user_input)
                if ok and skill:
                    emit("skill_learned", {
                        "name": skill.name,
                        "version": skill.version,
                        "message": msg,
                    })
                    self.context.add_assistant_message(msg)
                    emit("message_final", {"content": msg})
                    return msg

            # 直接回答
            if self.manager.should_answer_directly(user_input):
                emit("thinking", {"stage": "direct_answer"})
                buf = []
                async for d in self.orchestrator.stream(user_input, {}, None):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
                ans = "".join(buf)
                self.context.add_assistant_message(ans)
                emit("message_final", {"content": ans})
                return ans

            # 规划
            emit("thinking", {"stage": "planning"})
            plan = await self.manager.plan(user_input)
            skill_name = plan.selected_skill.name if plan.selected_skill else None
            emit("plan", {
                "intent": plan.intent,
                "skill": skill_name,
                "tasks": plan.tool_tasks,
            })

            # 执行工具
            tool_results: Dict = {}
            if plan.tool_tasks:
                emit("thinking", {"stage": "tools_running", "count": len(plan.tool_tasks)})
                
                async def on_tool_event(event: str, payload: dict):
                    emit(event, payload)

                tasks = []
                for i, t in enumerate(plan.tool_tasks):
                    task_id = t.get("id") or f"t{i+1}"
                    tasks.append(ToolTask(
                        id=task_id,
                        type=t.get("type", ""),
                        params=t.get("params", {}),
                        depends_on=t.get("depends_on", []) or [],
                    ))
                tool_results = await self.learning.execute_dag(tasks, on_tool_event)

            # 生成回答
            emit("thinking", {"stage": "synthesizing"})
            buf = []
            try:
                async for d in self.orchestrator.stream(user_input, tool_results, plan.selected_skill):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
            except Exception:
                ans = await self.orchestrator.orchestrate(user_input, tool_results, plan.selected_skill)
                buf = [ans]
                emit("message_delta", {"delta": ans})

            ans = "".join(buf)
            self.context.add_assistant_message(ans)
            emit("message_final", {"content": ans})

            duration = (time.time() - start) * 1000
            self.logger.info("Agent", f"DONE: {duration:.0f}ms")
            return ans

        except Exception as e:
            self.logger.error("Agent", f"ERROR: {e}")
            emit("error", {"message": str(e)})
            raise

    def reset(self):
        self.context.clear()
        self.logger.info("Agent", "RESET")
