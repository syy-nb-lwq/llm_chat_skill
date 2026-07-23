"""Agent 核心 - 多智能体协作"""
import asyncio
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from agents.manager import ManagerAgent, IntentType
from agents.orchestrator import OrchestratorAgent
from agents.learning import LearningAgent, ToolTask
from agents.skill_manager_agent import get_skill_manager_agent
from agents.skill_trainer import SkillTrainer
from core.context import Context
from core.critic import ExecutionCritic, build_execution_context
from core.dag import DAGExecutor
from core.identity import IdentityContext, new_id
from core.memory import get_memory_store
from core.memory_repository import (
    EpisodeRecord,
    MemoryItem,
    MemoryScope,
    MemoryType,
    get_memory_repository,
)
from infra.config import config
from infra.logger import get_logger


class Agent:
    """多智能体协作入口"""

    def __init__(self, session_id: str = "default", user_id: str = "default"):
        self.session_id = session_id
        self.user_id = user_id
        self.logger = get_logger()

        self.manager = ManagerAgent()
        # M2-06:让 Manager 在召回时知道当前 user_id
        self.manager._current_user_id = user_id
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.dag = DAGExecutor(self.learning)
        self.trainer = SkillTrainer()
        # M1-09:把 SkillManagerAgent 接入主链
        self.skill_manager = get_skill_manager_agent()
        self.critic = ExecutionCritic(get_memory_store())

        self.context = Context()
        self.dag_enabled = bool(config.skill_dag_enabled)

        # M2-07:Session 摘要,超过窗口的早期消息折叠为摘要
        self._session_summary: str = ""
        self._window_size: int = 12  # 保留最近 N 条原始消息

        # 上一次的 execution_id(用于失败记录不互相覆盖)
        # 注意:这是为了兼容旧 API 访问 trace_id 的调用方;真正的本次
        # execution_id 在每次 handle() 入口创建 self._current_execution。
        self.trace_id = ""

    # ===== M0-01:每次 handle 独立 execution_id =====
    def _new_execution(self) -> IdentityContext:
        ctx = IdentityContext(
            user_id=self.user_id,
            session_id=self.session_id,
        )
        self.trace_id = ctx.execution_id
        return ctx

    def _new_child_execution(self, parent: IdentityContext) -> IdentityContext:
        ctx = parent.child()
        self.trace_id = ctx.execution_id
        return ctx

    # ===== M2-07:Session 摘要 =====
    def _maybe_summarize(self) -> None:
        """当消息数超过窗口时,生成摘要,折叠早段消息。"""
        msgs = self.context.messages
        if len(msgs) <= self._window_size:
            return
        # 选 1/3 旧的最近一次轮次
        old = msgs[:max(0, len(msgs) - self._window_size)]
        if not old:
            return
        text_blocks: List[str] = []
        for m in old:
            content = (m.content or "").strip()
            if not content:
                continue
            if len(content) > 200:
                content = content[:200] + "..."
            text_blocks.append(f"[{m.role}] {content}")
        summary_piece = "\n".join(text_blocks)
        if self._session_summary:
            self._session_summary = self._session_summary + "\n" + summary_piece
        else:
            self._session_summary = summary_piece
        # 保留 recent 部分
        self.context.messages = msgs[len(msgs) - self._window_size:]

    def get_session_summary(self) -> str:
        return self._session_summary

    async def handle(
        self,
        user_input: str,
        on_event: Optional[Callable] = None,
        *,
        identity: Optional[IdentityContext] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        start = time.time()

        if user_id:
            self.user_id = user_id
            self.manager._current_user_id = user_id
        if session_id:
            self.session_id = session_id

        # M0-01:每次 handle 创建新的 execution_id(非 session 级常量)
        identity_ctx = identity or self._new_execution()

        # M2-07:把 session 摘要注入 system prompt(经 context.to_llm_messages 之前)
        if self._session_summary:
            self.context.metadata["session_summary"] = self._session_summary

        self.context.add_user_message(user_input)
        self.logger.info(
            "Agent",
            f"INPUT[{identity_ctx.execution_id}]: {user_input[:50]}",
        )

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
            # 每个 payload 都加上 execution_id,前端能按 execution 串起来
            payload = {**payload, "execution_id": identity_ctx.execution_id}
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
            # M2-06:把 user_id 传给 manager,以便召回时按作用域过滤
            self.manager._current_user_id = self.user_id
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
                self._maybe_record_episode(
                    identity_ctx=identity_ctx,
                    intent=intent_type,
                    selected_skill=None,
                    skill_version=None,
                    tool_results={},
                    task_specs={},
                    duration=(time.time() - start) * 1000.0,
                    diagnosis="chitchat",
                    success_rate=1.0,
                )
                self._maybe_summarize()
                return ans

            # M1-09:Manager 意图(列出/查看/回滚/激活)走 SkillManagerAgent
            if intent_type == IntentType.MANAGER:
                emit("thinking", {"stage": "skill_manager"})
                mgr_result = await self.skill_manager.handle(user_input)
                ans = mgr_result.message
                self.context.add_assistant_message(ans)
                emit("skill_manager_result", {
                    "action": mgr_result.action,
                    "ok": mgr_result.ok,
                    "skill_name": mgr_result.skill_name,
                    "version": mgr_result.version,
                    "details": mgr_result.details or [],
                })
                emit("message_final", {"content": ans})
                await _drain_pending()
                self._maybe_summarize()
                return ans

            # 教导意图:进入技能学习流程
            if intent_type == IntentType.TEACH:
                emit("thinking", {"stage": "teaching_detect"})
                ok, result, skill = await self.trainer.teach(
                    user_input,
                    user_id=self.user_id,
                    session_id=self.session_id,
                )

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
                    # 教导成功 → 写入 episode,标记 taught
                    self._maybe_record_episode(
                        identity_ctx=identity_ctx,
                        intent=intent_type,
                        selected_skill=skill,
                        skill_version=skill.version if skill else None,
                        tool_results={},
                        task_specs={},
                        duration=(time.time() - start) * 1000.0,
                        diagnosis="teach_success",
                        success_rate=1.0,
                    )
                    self._maybe_summarize()
                    return result

                # 需要交互式教导
                if result and not ok:
                    emit("teaching_interactive", {"questions": result})
                    # 不调用不存在的 self.llm(M1-07):把 assistant 的中间追问
                    # 直接当作 assistant_message 留给下一轮,不污染 user 角色。
                    follow = (
                        "好的,我还想确认一些细节:\n" + result
                        if isinstance(result, str)
                        else "好的,请继续告诉我更多信息。"
                    )
                    self.context.add_assistant_message(follow)
                    emit("message_final", {"content": follow})
                    await _drain_pending()
                    self._maybe_summarize()
                    return follow

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
            skill_version = plan.selected_skill.version if plan.selected_skill else None
            emit("plan", {
                "intent": plan.intent,
                "skill": skill_name,
                "skill_version": skill_version,
                "skill_version_id": (
                    f"{skill_name}@{skill_version}" if skill_name and skill_version else None
                ),
                "tasks": plan.tool_tasks,
            })

            # 提取实体,供 Skill DAG 的 params 用 (${user_input.city})
            entities = self._extract_entities(user_input)

            # 执行工具
            tool_results: Dict = {}
            task_specs: Dict[str, Dict[str, Any]] = {}
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

                # 记录 task_specs 给 critic(M0-02)
                for t in tasks:
                    task_specs[t.id] = {
                        "type": t.type,
                        "retry": int(t.retry or 0),
                        "timeout_s": int(t.timeout_s or 30),
                        "fallback_to": t.fallback_to,
                    }

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
            self.logger.info("Agent", f"DONE[{identity_ctx.execution_id}]: {duration:.0f}ms")

            # ExecutionCritic 异步评估,不阻塞响应
            execution_context = build_execution_context(
                trace_id=identity_ctx.execution_id,
                execution_id=identity_ctx.execution_id,
                turn_id=identity_ctx.turn_id,
                user_id=identity_ctx.user_id,
                session_id=identity_ctx.session_id,
                parent_execution_id=identity_ctx.parent_execution_id,
                scenario=plan.selected_skill.name if plan.selected_skill else "",
                intent=plan.intent,
                selected_skill=plan.selected_skill.name if plan.selected_skill else None,
                selected_skill_version=skill_version,
                tool_results=tool_results,
                latency_ms=duration,
                task_specs=task_specs,
                final_output=ans,
                selected_skill_obj=plan.selected_skill,
            )

            # 计算 success_rate / fallback_count / retry_count(M0-02)
            success_rate, fallback_count, retry_count = self._summarize_results(
                tool_results, task_specs,
            )

            # M2-03:主链路写入 EpisodeRecord
            self._maybe_record_episode(
                identity_ctx=identity_ctx,
                intent=intent_type,
                selected_skill=plan.selected_skill,
                skill_version=skill_version,
                tool_results=tool_results,
                task_specs=task_specs,
                duration=duration,
                diagnosis="",
                success_rate=success_rate,
                fallback_count=fallback_count,
                retry_count=retry_count,
            )

            # 异步执行 critic,收集到 pending_async 末尾 await
            pending_async.append(asyncio.ensure_future(
                self.critic.evaluate(execution_context)
            ))

            self._maybe_summarize()
            return ans

        except Exception as e:
            self.logger.error("Agent", f"ERROR[{identity_ctx.execution_id}]: {e}")
            emit("error", {"message": str(e)})
            # 记录失败 episode
            try:
                self._maybe_record_episode(
                    identity_ctx=identity_ctx,
                    intent="error",
                    selected_skill=None,
                    skill_version=None,
                    tool_results={},
                    task_specs={},
                    duration=(time.time() - start) * 1000.0,
                    diagnosis=f"exception: {e}",
                    success_rate=0.0,
                )
            except Exception:
                pass
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
        self._session_summary = ""
        self.logger.info("Agent", "RESET")

    def chat(self, user_input: str) -> str:
        """同步包装:旧 API 兼容。

        返回 ``Agent.handle`` 的字符串结果。如果已在事件循环内,
        会返回 None 并提示使用 await;否则用 ``asyncio.run`` 执行。
        """
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            return _asyncio.run(self.handle(user_input))
        # 已在事件循环内:转为异步调用,警告调用方迁移
        self.logger.warning(
            "Agent", "chat() 在事件循环内被调用;请改用 await agent.handle(...)",
        )
        return None

    # ===== 摘要 helper =====

    @staticmethod
    def _summarize_results(
        tool_results: Dict[str, Any],
        task_specs: Dict[str, Dict[str, Any]],
    ):
        if not tool_results:
            return 1.0, 0, 0
        total = len(tool_results)
        success = 0
        fallback_count = 0
        retry_count = 0
        for tid, r in tool_results.items():
            if getattr(r, "success", False):
                success += 1
            meta = getattr(r, "meta", {}) or {}
            if meta.get("used_fallback"):
                fallback_count += 1
            retry_count += int(meta.get("retry_count", 0) or 0)
        return success / total, fallback_count, retry_count

    def _maybe_record_episode(
        self,
        *,
        identity_ctx: IdentityContext,
        intent: str,
        selected_skill: Optional[Any],
        skill_version: Optional[str],
        tool_results: Dict[str, Any],
        task_specs: Dict[str, Dict[str, Any]],
        duration: float,
        diagnosis: str,
        success_rate: float,
        fallback_count: int = 0,
        retry_count: int = 0,
    ) -> None:
        """M2-03:把一次执行写入统一存储。"""
        try:
            repo = get_memory_repository()
            skill_name = getattr(selected_skill, "name", "") if selected_skill else ""
            tool_attempts = {
                tid: {
                    "tool": (getattr(r, "meta", {}) or {}).get("tool", task_specs.get(tid, {}).get("type", tid)),
                    "success": getattr(r, "success", False),
                    "retry_count": int((getattr(r, "meta", {}) or {}).get("retry_count", 0) or 0),
                    "latency_ms": float((getattr(r, "meta", {}) or {}).get("latency_ms", 0.0) or 0.0),
                    "error": getattr(r, "error", "") or "",
                }
                for tid, r in tool_results.items()
            }
            ep = EpisodeRecord(
                execution_id=identity_ctx.execution_id,
                trace_id=identity_ctx.execution_id,
                user_id=self.user_id,
                session_id=self.session_id,
                turn_id=identity_ctx.turn_id,
                scenario=skill_name,
                intent=str(intent),
                selected_skill=skill_name,
                selected_skill_version=skill_version or "",
                success_rate=success_rate,
                fallback_count=fallback_count,
                retry_count=retry_count,
                latency_ms=duration,
                diagnosis=diagnosis,
                tool_attempts=tool_attempts,
            )
            repo.add_episode(ep)
        except Exception as e:
            self.logger.warning("Agent", f"episode 写入失败: {e}")

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
