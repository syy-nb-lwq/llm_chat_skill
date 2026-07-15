"""Agent 核心 - 多智能体协作"""
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional

from agents.manager import ManagerAgent, IntentType
from agents.orchestrator import OrchestratorAgent
from agents.learning import LearningAgent, ToolTask
from agents.skill_trainer import SkillTrainer
from core.context import Context
from core.critic import ExecutionCritic, build_execution_context
from core.dag import DAGExecutor
from core.memory import get_memory_store
from infra.config import config
from infra.logger import get_logger


class Agent:
    """多智能体协作入口"""

    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        self.logger = get_logger()
        self.trace_id = f"session-{session_id}-{int(time.time())}"

        self.manager = ManagerAgent()
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.dag = DAGExecutor(self.learning)
        self.trainer = SkillTrainer()
        self.critic = ExecutionCritic(get_memory_store())

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

        # 异步回调串行队列:保证按 emit 顺序执行,而不依赖 task 调度
        pending_async: list = []

        def emit(event: str, payload: dict):
            """派发事件。
            - 同步 on_event: 直接调用
            - 异步 on_event: 创建 future + 串行 await(由 _drain_pending 完成)
              不能用 ensure_future 调度,否则 sleep 0.01s 的回调会并发跑、append 乱序。
            """
            if on_event is None:
                return
            if not asyncio.iscoroutinefunction(on_event):
                try:
                    on_event(event, payload)
                except Exception:
                    pass
                return
            try:
                # 把"调用 on_event 并返回结果"包装成 future
                fut: asyncio.Future = asyncio.Future()

                async def _wrap():
                    try:
                        await on_event(event, payload)
                        if not fut.done():
                            fut.set_result(None)
                    except Exception as e:
                        if not fut.done():
                            fut.set_exception(e)

                # 关键:不立即 schedule _wrap,挂到 pending,等 drain 时串行 await。
                # 用一个 _deferred 协程持有 _wrap 引用,让 event loop 在 await 时才启动。
                async def _deferred():
                    await _wrap()

                # 保存 _deferred 协程本身,drain 时用 ensure_future 启动并 await
                pending_async.append(_deferred())
            except Exception:
                pass

        async def _drain_pending():
            """按 emit 顺序串行 await 所有 pending 任务。"""
            while pending_async:
                batch = pending_async[:]
                pending_async.clear()
                for coro in batch:
                    try:
                        await asyncio.ensure_future(coro)
                    except Exception:
                        pass

        try:
            # ===== 意图规划(统一入口) =====
            emit("thinking", {"stage": "planning"})
            plan = await self.manager.plan(user_input, self.context)

            # 根据意图类型决定后续流程
            intent_type = plan.intent

            # 闲聊意图:直接回答,不记录到记忆
            if intent_type == IntentType.CHITCHAT:
                emit("thinking", {"stage": "chitchat", "detail": plan.intent_detail})
                buf = []
                async for d in self.orchestrator.stream(user_input, {}, None, self.context):
                    buf.append(d)
                    emit("message_delta", {"delta": d})
                ans = "".join(buf)
                self.context.add_assistant_message(ans)
                emit("message_final", {"content": ans})
                # 等异步 on_event 完成,保证事件按 emit 顺序排干
                await _drain_pending()
                # 闲聊不进行 ExecutionCritic 评估,避免污染记忆库
                return ans

            # 教导意图:进入技能学习流程
            if intent_type == IntentType.TEACH:
                emit("thinking", {"stage": "teaching_detect"})
                ok, result, skill = await self.trainer.teach(user_input)
                
                if ok and skill:
                    # 技能创建成功
                    emit("skill_learned", {
                        "name": skill.name,
                        "version": skill.version,
                        "message": result,
                    })
                    self.context.add_assistant_message(result)
                    emit("message_final", {"content": result})
                    await _drain_pending()
                    return result

                # 需要交互式教导
                if result and not ok:
                    emit("teaching_interactive", {"questions": result})
                    # 使用 LLM 补充信息并完成教导
                    complete_msg = await self._complete_teaching(user_input, result)
                    self.context.add_assistant_message(complete_msg)
                    emit("message_final", {"content": complete_msg})
                    await _drain_pending()
                    return complete_msg
                
                # 教导失败,继续后续流程

            # 重试意图:重新执行上次的任务
            if intent_type == IntentType.RETRY:
                emit("thinking", {"stage": "retry"})
                # 从 context 中获取上一次的 tool_tasks
                last_tasks = self._get_last_tool_tasks()
                if last_tasks:
                    self.logger.info("Agent", f"重试执行上次的 {len(last_tasks)} 个工具")
                    # 使用上一次的 tool_tasks 继续执行
                    plan.tool_tasks = last_tasks
                else:
                    emit("thinking", {"stage": "no_retry_history"})
                    ans = "没有找到上次的执行记录,无法重试"
                    self.context.add_assistant_message(ans)
                    emit("message_final", {"content": ans})
                    await _drain_pending()
                    return ans

            # 技能执行(正常流程)
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

            # 存储本次 tool_tasks 用于重试
            self.context.set_last_tool_tasks(plan.tool_tasks)

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

            # ExecutionCritic 异步评估,不阻塞响应
            execution_context = build_execution_context(
                trace_id=self.trace_id,
                scenario=plan.selected_skill.name if plan.selected_skill else "",
                intent=plan.intent,
                selected_skill=plan.selected_skill.name if plan.selected_skill else None,
                tool_results=tool_results,
                latency_ms=duration,
            )
            # 异步执行 critic,收集到 pending_async 末尾 await
            pending_async.append(asyncio.ensure_future(
                self.critic.evaluate(execution_context)
            ))

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

    def _get_last_tool_tasks(self) -> Optional[List[Dict]]:
        """获取上一次的 tool_tasks"""
        return self.context.get_last_tool_tasks()

    async def _complete_teaching(self, user_input: str, questions: str) -> str:
        """交互式教导: 使用 LLM 补充信息并完成教导"""
        self.logger.info("Agent", "进入交互式教导流程")
        
        # 先询问用户需要补充的信息
        prompt = f"""用户想要教一个技能,但提供的信息不完整。

原始输入: {user_input}

{questions}

请用友好的方式询问用户补充信息:"""

        try:
            response = await self.llm.chat([
                {"role": "user", "content": prompt},
            ])
            # 将用户的回复添加到 context 供下一轮使用
            self.context.add_user_message(f"{user_input}\n\n{response}")
            return response
        except Exception as e:
            self.logger.error("Agent", f"交互式教导失败: {e}")
            return f"好的，请继续告诉我更多信息。"

    async def _finish_teaching(self, user_input: str) -> str:
        """完成教导: 根据收集的信息创建技能"""
        self.logger.info("Agent", "尝试完成教导")
        
        # 使用完整上下文创建技能
        ok, msg, skill = await self.trainer.teach(user_input)
        if ok and skill:
            return msg
        return msg  # 返回询问或其他消息

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
