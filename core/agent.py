"""Agent 核心 - 多智能体协作"""
import asyncio
import time
from typing import Any, Callable, Dict, Optional

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

        # 收集异步回调任务,handle 末尾统一 await,确保事件按 emit 顺序完成
        pending_async: list = []

        def emit(event: str, payload: dict):
            """派发事件。同步回调直接调;异步回调收集到 pending_async 末尾 await。"""
            if on_event is None:
                return
            # 同步回调:直接调
            if not asyncio.iscoroutinefunction(on_event):
                try:
                    on_event(event, payload)
                except Exception:
                    pass
                return
            # 异步回调:挂到 list,handle() 末尾统一 await
            try:
                task = asyncio.ensure_future(on_event(event, payload))
                pending_async.append(task)
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
                async for d in self.orchestrator.stream(user_input, {}, None, self.context):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
                ans = "".join(buf)
                self.context.add_assistant_message(ans)
                emit("message_final", {"content": ans})
                return ans

            # 规划
            emit("thinking", {"stage": "planning"})
            plan = await self.manager.plan(user_input, self.context)
            skill_name = plan.selected_skill.name if plan.selected_skill else None
            emit("plan", {
                "intent": plan.intent,
                "skill": skill_name,
                "tasks": plan.tool_tasks,
            })

            # 提取实体,供 Skill DAG 的 params 用 (${user_input.city})
            entities = self._extract_entities(user_input)

            # 执行工具
            tool_results: Dict = {}
            if plan.tool_tasks:
                emit("thinking", {"stage": "tools_running", "count": len(plan.tool_tasks)})

                async def on_tool_event(event: str, payload: dict):
                    emit(event, payload)

                # 如果启用了 Skill DAG 且 plan 关联到了带结构化 steps 的 skill,
                # 优先用 DAGExecutor 把 skill.steps 翻成 ToolTask(忽略 Manager 输出的 task 列表)。
                used_dag = False
                if self.dag_enabled and plan.selected_skill and plan.selected_skill.has_structured_steps():
                    skill_tasks, issues = self.dag.skill_to_tasks(plan.selected_skill)
                    for i in issues:
                        emit("thinking", {"stage": "dag_skip", "issue": i})
                    if skill_tasks:
                        tasks = skill_tasks
                        used_dag = True
                        emit("thinking", {"stage": "skill_dag_used", "skill": plan.selected_skill.name, "count": len(tasks)})

                if not used_dag:
                    tasks = []
                    for i, t in enumerate(plan.tool_tasks):
                        task_id = t.get("id") or f"t{i+1}"
                        tasks.append(ToolTask(
                            id=task_id,
                            type=t.get("type", ""),
                            params=t.get("params", {}),
                            depends_on=t.get("depends_on", []) or [],
                            retry=int(t.get("retry", 0) or 0),
                            timeout_s=int(t.get("timeout_s", 30) or 30),
                            fallback_to=t.get("fallback_to"),
                        ))
                tool_results = await self.learning.execute_dag(
                    tasks, on_tool_event,
                    user_input={"text": user_input, **entities}
                )

            # 生成回答
            emit("thinking", {"stage": "synthesizing"})
            buf = []
            try:
                async for d in self.orchestrator.stream(user_input, tool_results, plan.selected_skill, self.context):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
            except Exception:
                ans = await self.orchestrator.orchestrate(user_input, tool_results, plan.selected_skill, self.context)
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
        finally:
            # 统一 await 所有异步 emit,保证事件按 emit 顺序完成
            if pending_async:
                results = await asyncio.gather(*pending_async, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        self.logger.warning("Agent", f"emit task failed: {r}")

    def reset(self):
        self.context.clear()
        self.logger.info("Agent", "RESET")

    # ===== 实体提取(给 Skill DAG 的 ${user_input.x} 用) =====

    _KNOWN_CITIES = {
        "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉",
        "西安", "重庆", "厦门", "南京", "天津", "苏州", "青岛",
        "长沙", "大连", "沈阳", "郑州", "哈尔滨", "长春", "南昌",
        "合肥", "昆明", "福州", "济南", "太原", "石家庄", "兰州",
        "乌鲁木齐", "呼和浩特", "南宁", "贵阳", "海口", "银川", "西宁",
        "拉萨", "香港", "澳门", "台北",
    }

    def _extract_entities(self, text: str) -> Dict[str, Any]:
        """轻量实体提取。

        当前支持:
        - city:  已知中国城市名(直接字典匹配)
        - date:  today / tomorrow / YYYY-MM-DD
        """
        out: Dict[str, Any] = {}
        for c in self._KNOWN_CITIES:
            if c in text:
                out["city"] = c
                break
        if "明天" in text:
            out["date"] = "tomorrow"
        elif "今天" in text or "今日" in text:
            out["date"] = "today"
        elif "后天" in text:
            out["date"] = "后天"  # wttr.in 不支持,让 weather 工具处理
        return out
