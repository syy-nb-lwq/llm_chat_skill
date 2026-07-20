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
from core.identity import IdentityContext
from core.memory import get_memory_store
from infra.config import config
from infra.logger import get_logger


class Agent:
    """多智能体协作入口"""

    def __init__(self, session_id: str = "default", user_id: str = "default"):
        self.session_id = session_id
        self.user_id = user_id
        self.logger = get_logger()
        # 兼容字段:首次 handle 之前保持空字符串,handle 入口再生成
        self.trace_id = ""

        self.manager = ManagerAgent()
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.dag = DAGExecutor(self.learning)
        self.trainer = SkillTrainer()
        self.critic = ExecutionCritic(get_memory_store())
        from skills.manager import get_skill_store
        self.skill_store = get_skill_store()

        self.context = Context()
        self.dag_enabled = bool(config.skill_dag_enabled)

    async def handle(
        self,
        user_input: str,
        on_event: Optional[Callable] = None,
    ) -> str:
        start = time.time()

        # 每次 handle 都创建独立的身份 + execution 标识,避免失败记录互相覆盖
        identity = IdentityContext(
            user_id=self.user_id,
            session_id=self.session_id,
        )
        self.trace_id = identity.execution_id

        self.context.add_user_message(user_input)
        self.logger.info("Agent", f"INPUT({identity.execution_id}): {user_input[:50]}")

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
                # 闲聊不进行 ExecutionCritic 评估,避免污染记忆库
                return ans

            # 教导意图:进入技能学习流程(M1-01 + M1-07)
            if intent_type == IntentType.TEACH:
                emit("thinking", {"stage": "teaching_detect"})
                ts = await self.trainer.start_or_continue(
                    user_input,
                    user_id=identity.user_id,
                    session_id=identity.session_id,
                )

                if ts.status == "active":
                    # 用户在草稿阶段输入"确认" → 校验 + 发布
                    ok, msg, skill = self.trainer.confirm_and_publish(
                        identity.user_id, identity.session_id,
                    )
                    if ok and skill:
                        emit("skill_learned", {
                            "name": skill.name,
                            "version": skill.version,
                            "message": msg,
                            "teaching_session_id": ts.teaching_session_id,
                        })
                    self.context.add_assistant_message(msg)
                    emit("message_final", {"content": msg})
                    return msg

                # 其它状态(collecting / draft / duplicate)→ 返回问题或草稿
                emit("teaching_interactive", {
                    "teaching_session_id": ts.teaching_session_id,
                    "status": ts.status,
                    "missing_fields": ts.missing_fields,
                    "duplicate_of": ts.duplicate_of,
                })
                assistant_msg = ts.current_question or "请补充技能信息"
                self.context.add_assistant_message(assistant_msg)
                emit("message_final", {"content": assistant_msg})
                return assistant_msg

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
                    return ans

            # 技能管理意图(M1-09):列出/查看/版本/回滚
            if intent_type == IntentType.MANAGER:
                emit("thinking", {"stage": "skill_management"})
                skills = self.skill_store.list_all()
                versions = self.skill_store._registry.all_versions()
                active_map = self.skill_store._registry.active_versions()
                if not skills:
                    ans = "当前没有任何已发布技能。"
                else:
                    lines = []
                    for s in skills:
                        ver = s.version
                        active_ver = active_map.get(s.name, "")
                        marker = " (active)" if ver == active_ver else ""
                        lines.append(
                            f"- **{s.name}** v{ver}{marker}: {s.capability}"
                        )
                    ans = "当前已发布技能:\n" + "\n".join(lines)
                self.context.add_assistant_message(ans)
                emit("message_final", {"content": ans})
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
            task_specs_by_id: Dict[str, Dict] = {}
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
                        for st in tasks:
                            task_specs_by_id[st.id] = {
                                "type": st.type,
                                "params": st.params,
                                "depends_on": st.depends_on,
                                "retry": st.retry,
                                "timeout_s": st.timeout_s,
                                "fallback_to": st.fallback_to,
                            }

                if not used_dag:
                    tasks = []
                    for i, t in enumerate(plan.tool_tasks):
                        task_id = t.get("id") or f"t{i+1}"
                        task_specs_by_id[task_id] = t
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
                user_id=identity.user_id,
                session_id=identity.session_id,
                turn_id=identity.turn_id,
                execution_id=identity.execution_id,
                parent_execution_id=identity.parent_execution_id,
                task_specs=task_specs_by_id,
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

    def chat(
        self,
        user_input: str,
        on_event: Optional[Callable] = None,
    ) -> str:
        """Sync wrapper used by CLI and Streamlit entry points."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.handle(user_input, on_event))

        result: Dict[str, Any] = {}
        error: Dict[str, BaseException] = {}

        def runner():
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                result["value"] = loop.run_until_complete(self.handle(user_input, on_event))
            except BaseException as exc:  # pragma: no cover - defensive sync bridge
                error["value"] = exc
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        import threading

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        thread.join()
        if "value" in error:
            raise error["value"]
        return result.get("value", "")

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
