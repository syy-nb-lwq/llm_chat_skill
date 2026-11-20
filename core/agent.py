"""Agent 核心 - 多智能体协作"""
import asyncio
import time
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from agents.manager import ManagerAgent, IntentType
from agents.orchestrator import OrchestratorAgent
from agents.learning import LearningAgent, ToolTask
from agents.skill_trainer import SkillTrainer
from core.context import Context, Message
from core.correction import CorrectionSignal, get_correction_detector
from core.critic import ExecutionCritic, build_execution_context
from core.dag import DAGExecutor
from core.identity import IdentityContext
from core.memory import get_memory_store
from core.memorykit import get_memory
from core.session_config import session_restore_limit
from core.turn_context import TurnContext
from infra.config import config
from core.events import (
    EV_CLARIFICATION_REQUEST,
    EV_ERROR,
    EV_MESSAGE_DELTA,
    EV_MESSAGE_FINAL,
    EV_PLAN,
    EV_SKILL_LEARNED,
    EV_TEACHING_INTERACTIVE,
    EV_THINKING,
)
from infra.logger import get_logger
from core.exception_policy import downgrade


_sync_loop: Optional[asyncio.AbstractEventLoop] = None

#: 上游返回空内容时的兜底文案(不给用户看空气泡)
EMPTY_ANSWER_TEXT = "抱歉,这次没能生成出有效回答。换个说法或补充一点信息,我再试一次。"


def _get_sync_loop() -> asyncio.AbstractEventLoop:
    """同步适配器专用的事件循环(整个进程复用一个)。

    复用而不是每次 ``asyncio.run`` 新建 loop:工具里的 httpx AsyncClient
    会绑定到首次使用的 loop,换 loop 后复用会报 "Event loop is closed"。
    """
    global _sync_loop
    if _sync_loop is None or _sync_loop.is_closed():
        _sync_loop = asyncio.new_event_loop()
    return _sync_loop


def _run_sync(coro):
    """在同步上下文中执行协程。

    - 当前线程没有运行中的事件循环:在复用的 sync loop 上执行
    - 已经有运行中的事件循环(例如在 async 代码里误用同步入口):
      放到独立线程的新 loop 上执行,避免 "asyncio.run() cannot be called
      from a running event loop"。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _get_sync_loop().run_until_complete(coro)

    import concurrent.futures

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run_in_thread).result()


class Agent:
    """多智能体协作入口"""

    def __init__(
        self,
        session_id: str = "default",
        persist_session: bool = False,
    ):
        self.session_id = session_id
        self.logger = get_logger()
        self.trace_id = f"session-{session_id}-{int(time.time())}"

        # M0-01:每次 handle() 生成新的四级标识
        self.identity: Optional[IdentityContext] = None
        self.execution_id: str = ""
        self.turn_id: str = ""

        self.manager = ManagerAgent()
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.dag = DAGExecutor(self.learning)
        self.trainer = SkillTrainer()
        self.critic = ExecutionCritic(get_memory_store())

        self.context = Context()
        self.dag_enabled = bool(config.skill_dag_enabled)

        # 记忆三层门面：工作 / 短期 / 长期（见 core/memorykit/__init__.py 的职责表）
        # 主链只认这三张脸，不再直接摸底层 store。
        self.mem = get_memory()
        #: 本轮的工作记忆，在 handle() 里 begin_turn 创建
        self.working = None
        #: 执行上下文的 contextvar token（轮末 reset 用），由 _begin_turn 设置
        self._exec_tokens: tuple = ()

        # 会话持久化(短期记忆):仅在服务端会话里开启
        self.persist_session = bool(persist_session)
        self._session_store = None
        if self.persist_session:
            self._setup_session_persistence()

        # 用户画像写入(长期记忆):跟随 semantic_memory_enabled 开关
        self.persist_memory = bool(getattr(config, "semantic_memory_enabled", False))
        self._memory_tasks: set = set()
        self._profile_cache: Optional[str] = None
        #: 能力清单缓存(每轮清一次,见 _get_capability_block)
        self._capability_cache: Optional[str] = None

        # 用户纠正信号(自演化的质量信号):
        # 上一轮的执行上下文 —— 下一轮如果用户在纠正,就用它重评
        self._last_exec_ctx = None
        # 最近若干轮的执行上下文(供前端对历史消息打「不对」)
        self._exec_ctxs: "OrderedDict[str, Any]" = OrderedDict()
        # 后台反馈重评任务
        self._feedback_tasks: set = set()
        self.memory = self.critic.memory

    # ===== 用户纠正检测（自演化的质量信号） =====

    async def _evaluate_with_skill_quality(self, ctx):
        """跑 critic,并把这次真实使用记进技能质量账本(docs/14 §2.4)。

        **为什么"线上使用"比发布时的验收样例更可信**:
        验收样例只能证明"这一版没退化";线上信号才能证明"用户真的满意"。
        一个工具全调通、但用户每次都要纠正的技能,只有这一路能抓到。
        """
        result = await self.critic.evaluate(ctx)
        self._record_skill_usage(ctx, result)
        return result

    def _record_skill_usage(self, ctx, result) -> None:
        """把一次技能使用记进质量账本。"""
        skill_name = getattr(ctx, "selected_skill", None)
        if not skill_name:
            return
        try:
            corrected = bool(getattr(result, "user_corrected", False))
            rate = float(getattr(result, "success_rate", 1.0))
            ok = (not corrected) and rate >= 1.0
            from core.skill_quality import get_skill_quality_store

            get_skill_quality_store().record_usage(
                skill_name,
                ok=ok,
                execution_id=getattr(ctx, "execution_id", "") or "",
            )
        except Exception as exc:
            self.logger.warning("Agent", f"记录技能使用结果失败: {exc}")

    @staticmethod
    def _collect_artifacts(tool_results: dict) -> list:
        """从本轮工具结果里收集产物(文件/视频),给前端做下载链接。

        `python_exec` 会把生成的文件名放在 `data.artifacts` 里;
        其他工具如果也返回 artifacts 结构,同样能被收集到。

        这里只做**归一化 + 去重**,不做鉴权 —— 鉴权在下载接口上
        (`/api/artifacts/...` 要求会话令牌 + 路径必须在工作区内)。
        """
        out: list = []
        seen: set = set()
        try:
            for result in (tool_results or {}).values():
                data = getattr(result, "data", None)
                if not isinstance(data, dict):
                    continue
                for item in data.get("artifacts") or []:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("path") or item.get("name") or "").strip()
                    if not name:
                        continue
                    key = name.replace("\\", "/").lstrip("/")
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "name": str(item.get("name") or key),
                        "path": key,
                        "size": int(item.get("size") or 0),
                        "url": f"/api/artifacts/{key}",
                    })
        except Exception as exc:
            try:
                get_logger().warning("Agent", f"收集产物失败: {exc}")
            except Exception as exc:
                downgrade(exc, where='core/agent.py')
        return out

    @staticmethod
    def _ensure_answer(ans: str) -> str:
        """空回答兜底 —— 不能给用户一个空气泡。

        实测:上游返回空内容时,前端只会渲染出一个空气泡,没有任何提示,
        用户会以为系统卡死了。换成一句明确的失败说明。
        """
        if (ans or "").strip():
            return ans
        return EMPTY_ANSWER_TEXT

    def _detect_correction(self, user_input: str) -> Optional[CorrectionSignal]:
        """检测本轮输入是不是在纠正**上一条**回答。

        没有上一条回答就无从纠正 —— 这是最重要的前置条件。
        """
        try:
            prev = self._last_assistant_text()
            return get_correction_detector().detect(user_input, previous_answer=prev)
        except Exception as exc:
            self.logger.warning("Agent", f"纠正检测失败: {exc}")
            return None

    def _last_assistant_text(self) -> str:
        """上下文里最后一条助手回答"""
        try:
            for msg in reversed(self.context.messages):
                if msg.role == "assistant" and (msg.content or "").strip():
                    return msg.content
        except Exception as exc:
            downgrade(exc, where='core/agent.py')
        return ""

    def _apply_correction(self, signal: CorrectionSignal):
        """把纠正信号接到**上一轮**的执行上。

        上一轮 critic 已经跑完并记了"成功";现在用户说不对,
        就要**用纠正信息重评一次**,并把那条成功记录撤掉 ——
        否则统计里这条既算成功又算失败,采纳率就不准了。

        Returns:
            asyncio.Future 或 None(没有可重评的上下文时)
        """
        ctx = self._last_exec_ctx
        self._last_exec_ctx = None
        if ctx is None:
            self.logger.info("Agent", "检测到用户纠正,但没有对应的上一轮执行上下文")
            return None

        self.logger.info(
            "Agent",
            f"检测到用户纠正({signal.kind}, conf={signal.confidence:.2f}): "
            f"{signal.text[:60]}",
        )
        return self._reevaluate(ctx, signal.text)

    def _reevaluate(self, ctx, feedback: str):
        """用反馈重评一次执行,并撤销它之前的成功记录。"""
        retry_ctx = replace(ctx, user_feedback=feedback)
        try:
            self.memory.remove_success(ctx.trace_id)
        except Exception as exc:
            self.logger.warning("Agent", f"撤销上一轮成功记录失败: {exc}")
        # 重评用的是**同一个**技能 → 这次"被纠正"必须记到那个技能头上,
        # 否则技能质量账本里看不到真实的不满意(docs/14 §2.4)
        return asyncio.ensure_future(
            self._evaluate_with_skill_quality(retry_ctx)
        )

    def apply_explicit_feedback(
        self,
        execution_id: str,
        accepted: bool,
        comment: str = "",
    ) -> bool:
        """处理用户显式反馈(前端 👍 / 👎)。

        这是比隐式纠正**更可靠**的信号 —— 用户明确按了"不对"。
        `accepted=False` 时等价于一次纠正:重评那一轮并生成改进补丁。

        Returns:
            是否受理(找不到对应的执行上下文时返回 False)
        """
        ctx = self._exec_ctxs.get(execution_id)
        if ctx is None:
            self.logger.info("Agent", f"找不到执行上下文: {execution_id}")
            return False
        if accepted:
            self.logger.info("Agent", f"用户确认回答正确: {execution_id}")
            return True

        feedback = (comment or "").strip() or "用户标记该回答不正确"
        self.logger.info("Agent", f"用户标记回答不正确: {execution_id}: {feedback}")
        fut = self._reevaluate(ctx, feedback)
        if fut is not None:
            self._feedback_tasks.add(fut)
            fut.add_done_callback(self._feedback_tasks.discard)
        return True

    async def flush_feedback(self) -> None:
        """等后台的反馈重评跑完(WS 处理器 / 测试用)。"""
        while self._feedback_tasks:
            pending = list(self._feedback_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    # ===== 用户画像写入(长期记忆) =====

    def _get_profile_block(self, query: str = "") -> str:
        """本轮的用户画像块(**每轮只读一次**)。

        读取失败一律返回空串 —— 画像读不到不能影响回答。

        `query` 传本轮用户输入：**限定场景**的画像只在话题相关时才注入
        （"在 minimax 上用国内站"不该在聊电商时出现）。

        缓存在 `self._profile_cache` 里,`handle()` 开头清空:
        上一轮的画像写入是异步的,同一轮内重复读没有意义,
        但**下一轮**必须读到最新值。
        """
        if self._profile_cache is not None:
            return self._profile_cache
        block = ""
        if self.persist_memory:
            try:
                # 长期记忆的唯一读入口（画像 ≤400 字，常驻注入）
                block = self.mem.long_term.profile_block(query=query)
            except Exception as exc:
                self.logger.warning("Agent", f"读取用户画像失败: {exc}")
        self._profile_cache = block
        return block

    def _get_capability_block(self) -> str:
        """本轮的能力清单(**每轮只读一次**)。

        为什么要注入:回答层的 system prompt 如果只说"你是回答助手",
        模型被问"你能做什么"时会退回基础模型的通用自我认知
        ——"我只是个文本模型,不能执行代码"。哪怕系统里明明有执行环境。

        实机踩过:用户问"能不能执行代码/发网络请求/读写文件",
        agent 一条条回答"不能"。它不知道自己兜里有什么。
        """
        if self._capability_cache is not None:
            return self._capability_cache
        block = ""
        try:
            from core.capabilities import build_capability_block

            block = build_capability_block()
        except Exception as exc:
            self.logger.warning("Agent", f"生成能力清单失败: {exc}")
        self._capability_cache = block
        return block

    # ===== 目标循环配置 =====

    def _goal_loop_enabled(self) -> bool:
        """要不要跑多轮（关掉就退化成"只跑一轮"，和旧行为一致）。"""
        try:
            from infra.config import config

            return bool(getattr(config, "goal_loop_enabled", True))
        except Exception:
            return True

    def _goal_max_rounds(self) -> int:
        """轮次上限 —— 防烧钱的第一道闸。"""
        try:
            from infra.config import config

            return max(1, int(getattr(config, "goal_max_rounds", 6)))
        except Exception:
            return 6

    def _goal_budget_s(self) -> float:
        """总时长预算 —— 第二道闸。"""
        try:
            from infra.config import config

            return max(10.0, float(getattr(config, "goal_time_budget_s", 180)))
        except Exception:
            return 180.0

    # ===== 方法论自增长（后台，不阻塞回答） =====

    async def _harvest_skill(self, user_input: str, tool_results: Dict,
                             rounds: int, planned_tasks: List) -> None:
        """把一次成功的多步任务提炼成技能草稿。

        **永远不抛异常**：这是附加价值，失败不能影响主流程。
        """
        try:
            from infra.config import config

            if not getattr(config, "skill_harvest_enabled", True):
                return
            from core.skill_harvest import harvest

            specs = {}
            for t in (planned_tasks or []):
                try:
                    specs[t.id] = {"type": t.type, "params": dict(t.params or {})}
                except Exception:
                    continue
            outcome = await harvest(
                user_input,
                tool_results,
                rounds=rounds,
                task_specs=specs,
            )
            if outcome.skill:
                self.logger.info(
                    "Agent", f"沉淀出方法论草稿: {outcome.skill.name}"
                )
            elif outcome.reason:
                self.logger.info("Agent", f"未沉淀方法论: {outcome.reason}")
        except Exception as exc:
            self.logger.warning("Agent", f"方法论提炼失败(忽略): {exc}")

    async def _repair_tools(self) -> None:
        """检查有没有"自己造的、反复失败"的工具，有就试着修一版。

        **永远不抛异常** —— 自修复是附加价值，失败不能影响主流程。
        只修有 create_tool 提案的工具；内置工具源码不动。
        """
        try:
            from infra.config import config

            if not getattr(config, "tool_repair_enabled", True):
                return
            from core.tool_health import get_health_store
            from core.tool_repair import auto_repair

            bad = get_health_store().unhealthy()
            if not bad:
                return
            results = await auto_repair(max_repairs=1)
            for r in results:
                if r.published:
                    self.logger.info("Agent", f"自动修复工具: {r.tool} -> {r.reason}")
                elif r.attempted:
                    self.logger.info("Agent", f"尝试修复未成功: {r.tool} — {r.reason}")
        except Exception as exc:
            self.logger.warning("Agent", f"工具自修复失败(忽略): {exc}")

    # ===== 分阶段耗时记录 =====

    def _new_trace(self):
        """建一条本轮 trace(观测失败绝不能影响业务)"""
        try:
            from core.trace import TurnTrace

            scope = ""
            agent_id = "main"
            try:
                from core.exec_context import get_exec_context

                ctx = get_exec_context()
                scope = ctx.scope_key
                agent_id = ctx.agent_id or "main"
            except Exception as exc:
                downgrade(exc, where='core/agent.py')
            return TurnTrace(
                trace_id=self.trace_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                scope=scope,
                agent_id=agent_id,
            )
        except Exception:
            return None

    def _finish_trace(self, trace, turn_start: float, *, error: str = "") -> None:
        """收尾并落盘"""
        if trace is None:
            return
        try:
            from core.trace import STAGE_TOTAL, get_trace_recorder

            trace.since(STAGE_TOTAL, turn_start)
            if error:
                trace.error = error[:200]
            get_trace_recorder().record(trace)
        except Exception as exc:
            self.logger.warning("Agent", f"记录 trace 失败: {exc}")

    def _memory_context(self, current_input: str, limit: int = 6) -> str:
        """最近几轮对白（不含当前这句），给画像抽取消解指代用。

        为什么必须给：用户说「我以后都不要用国际站」时，主语（minimax）
        只在更早的轮次里。只把这一句丢给抽取模型，它只能猜 —— 实测猜成了
        "拒绝一切国际站"，于是几天后聊电商被这条记忆否掉。

        这对白**只用于判断指代**：证据（evidence）仍然必须来自当前这句，
        校验在 `core/profile_llm.validate_facts` 里，不从这里放宽。

        这里不包 try/except：`history_messages` 是纯切片，抛不出东西；
        真抛了也该让 `_schedule_memory_write` 那一层的日志看见。
        """
        msgs = self.context.history_messages(exclude_content=current_input or "")
        lines: List[str] = []
        for msg in msgs[-limit:]:
            content = (msg.content or "").strip().replace("\n", " ")
            if not content:
                continue
            role = "用户" if msg.role == "user" else "助手"
            lines.append(f"{role}：{content[:160]}")
        return "\n".join(lines)

    def _schedule_memory_write(self, user_input: str) -> None:
        """异步写用户画像,**不阻塞本轮响应**。

        docs/13 §2.4:写入必须异步且失败只记日志 —— 记忆是"锦上添花",
        不能因为它把回答拖慢或拖垮。
        """
        if not self.persist_memory or not user_input:
            return
        try:
            context = self._memory_context(user_input)
            task = asyncio.create_task(
                # 长期记忆的唯一写入口（LLM 抽取 + 证据校验，在后台跑）
                self.mem.long_term.record_turn(
                    user_input,
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    context=context,
                )
            )
        except Exception as exc:
            self.logger.warning("Agent", f"调度画像写入失败: {exc}")
            return

        # 持引用防止被 GC,并吞掉异常(内部已记录日志)
        self._memory_tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._memory_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self.logger.warning("Agent", f"画像写入异常: {exc}")

        task.add_done_callback(_done)

    # ===== 会话持久化(短期记忆) =====

    def _setup_session_persistence(self):
        """挂上持久化回调,并把该会话已有的历史恢复到 context。

        恢复时**直接**塞进 ``context.messages``,不走 ``add_message``,
        否则会把旧消息重复写回库里。
        """
        try:
            store = self.mem.short_term
            self._session_store = store
            store.ensure_session(self.session_id)
            self.context.sink = self._persist_message

            # reset 之前的内容不装回工作上下文(但仍保留在库里、可检索)
            reset_at = store.reset_at_seq(self.session_id)

            # 摘要只装回**覆盖到 reset 之后**的那一份。
            # 整份摘要都在 reset 之前 → 它讲的是"被用户主动清掉的旧话题",
            # 再塞回工作上下文等于 reset 没生效。
            cover = 0
            summary = store.latest_summary(self.session_id)
            if summary is not None:
                summary_cover = int(summary.cover_until_seq)
                if summary_cover > reset_at:
                    self.context.set_summary(summary.summary)
                    # 摘要覆盖到的绝对序号(滚动摘要的记账基准)
                    cover = summary_cover
                    self.context.rollup_cover_seq = cover
                else:
                    self.logger.info(
                        "Agent",
                        f"会话摘要已被 reset 覆盖(cover={summary_cover} <= reset={reset_at}),不装回",
                    )

            skip_before = max(cover, reset_at)

            # 只恢复"尚未被摘要覆盖"的消息,取最近若干条
            restored = store.recent_messages(
                self.session_id, limit=session_restore_limit()
            )
            for msg in restored:
                if msg.seq <= skip_before:
                    continue
                self.context.messages.append(
                    Message(
                        role=msg.role,
                        content=msg.content,
                        seq=msg.seq,
                        tool_tasks=msg.tool_tasks,
                    )
                )
            # 恢复"上一轮的工具计划" —— 重试要用。
            # 注意空列表也是有效状态(那一轮不需要工具),所以判空用 is not None。
            for msg in reversed(self.context.messages):
                if msg.tool_tasks is not None:
                    self.context.last_tool_tasks = msg.tool_tasks
                    break
            if restored:
                self.logger.info(
                    "Agent",
                    f"恢复会话历史: {self.session_id} "
                    f"(跳过前 {skip_before} 条,载入 {len(self.context.messages)} 条)",
                )
        except Exception as exc:
            # 持久化不可用时退回纯内存模式,不能影响主流程
            self.logger.warning("Agent", f"会话持久化初始化失败: {exc}")
            self._session_store = None
            self.context.sink = None

    def _persist_message(self, msg: Message):
        """把一条消息写入会话库(由 Context.sink 回调触发)。

        落库后把绝对序号回填到 ``msg.seq`` —— 滚动摘要按它记账。
        ``tool_tasks`` 一起落库 —— 会话恢复后才知道上一轮调了哪些工具。
        """
        if self._session_store is None:
            return
        try:
            seq = self._session_store.append_message(
                session_id=self.session_id,
                role=msg.role,
                content=msg.content,
                turn_id=self.turn_id,
                tool_tasks=msg.tool_tasks,
            )
            msg.seq = int(seq)
        except Exception as exc:
            self.logger.warning("Agent", f"会话消息落库失败: {exc}")

    async def _maybe_rollup(self):
        """会话过长时把早期内容压成滚动摘要。

        **原文不删除** —— 只是给 LLM 一个压缩视图;存储始终全量保留。
        """
        if self._session_store is None:
            return
        try:
            await self.mem.short_term.rollup(
                self.context,
                session_id=self.session_id,
                logger=self.logger,
            )
        except Exception as exc:
            self.logger.warning("Agent", f"会话摘要滚动失败: {exc}")

    async def _begin_turn(self, tc: TurnContext) -> None:
        """一轮的准备工作 —— 身份、执行上下文、工作记忆、画像、trace。

        这些都是 `handle` 的**前置副作用**（contextvar、Context、异步写画像），
        每轮只做一次。结果全部写进 `tc`，后面的 stage 只读 `tc`，
        不再靠局部变量在函数之间传递。
        """
        user_input = tc.user_input
        # M0-01:每次 handle 生成新的 execution_id,同 session 多轮不互相覆盖
        self.identity = IdentityContext(
            session_id=self.session_id,
        )
        self.execution_id = self.identity.execution_id
        self.turn_id = self.identity.turn_id
        self.trace_id = self.identity.execution_id

        # 执行上下文：这一轮"是谁在跑、跑在哪个隔离空间"。
        # 工具层（python_exec / write_script）靠它决定写入哪个目录 ——
        # 没有它就是全局单例目录,并发时会串味（docs/17 P0-2）。
        try:
            from core.exec_context import set_exec_context

            self._exec_tokens = set_exec_context(
                session_id=self.session_id,
                execution_id=self.execution_id,
                agent_id="main",
            )
        except Exception:
            self._exec_tokens = ()

        # 工作记忆：这一轮的工作台（计划 / 工具结果 / 结论）。
        # 一轮一个，轮末在 finally 里 end_turn —— 不清就是内存泄漏。
        self.working = self.mem.begin_turn(
            execution_id=self.execution_id,
            session_id=self.session_id,
        )

        # 会话过长时先滚动一次摘要(原文不删除,只是给 LLM 一个压缩视图)
        await self._maybe_rollup()

        self.context.add_user_message(user_input)
        self.logger.info("Agent", f"INPUT: {user_input[:50]}")

        # 用户画像与能力清单:每轮取一次,规划与回答生成共用同一份
        # (避免两处读不一致)
        self._profile_cache = None
        self._capability_cache = None
        # 上一轮的 emitter 可能还挂着（那一轮已经结束），先清掉
        try:
            from core.event_bus import clear_emitter

            clear_emitter()
        except Exception as exc:
            downgrade(exc, where='core/agent.py')
        profile_block = self._get_profile_block(user_input)
        capability_block = self._get_capability_block()

        # 分阶段耗时记录(产品化的最低观测要求,见 core/trace.py)
        trace = self._new_trace()
        trace.profile_chars = len(profile_block or "")

        # 用户画像写入(长期记忆):只依赖用户这句话,和本轮结果无关,
        # 所以在这里就异步起,让它和后面的规划/执行并行跑完。
        self._schedule_memory_write(user_input)

        # ===== 用户纠正检测 =====
        # 本轮输入可能是在纠正**上一条**回答 —— 那是自演化唯一的 ground truth,
        # 必须接进去,否则 critic 永远只知道"工具调没调通"。
        signal = self._detect_correction(user_input)
        if signal is not None:
            fut = self._apply_correction(signal)
            if fut is not None:
                tc.pending_async.append(fut)

        tc.profile_block = profile_block
        tc.capability_block = capability_block
        tc.trace = trace

    def _make_emitter(self, on_event, pending_async: list):
        """把一轮的事件派发器做成闭包。

        规则：
        - 同步 `on_event`：直接调
        - 异步 `on_event`：把协程挂到 `pending_async`，由 `_drain_pending` 按顺序 await
          （不能 ensure_future 直接调度，否则回调会并发执行、事件乱序）
        """
        def emit(event: str, payload: dict):
            """派发事件。
            - 同步 on_event: 直接调用
            - 异步 on_event: 创建 future + 串行 await(由 _drain_pending 完成)
              不能用 ensure_future 调度,否则 sleep 0.01s 的回调会并发跑、append 乱序。
            """
            # 最终回答带上 execution_id —— 前端要靠它把「👍/👎」关联到具体那条回答
            if event == "message_final" and self.execution_id:
                payload = {**payload, "execution_id": self.execution_id,
                           "turn_id": self.turn_id}
            if on_event is None:
                return
            if not asyncio.iscoroutinefunction(on_event):
                try:
                    on_event(event, payload)
                except Exception as exc:
                    downgrade(exc, where='core/agent.py')
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
            except Exception as exc:
                downgrade(exc, where='core/agent.py')
        return emit

    async def _drain_pending(self, pending_async: list) -> None:
        """按 emit 顺序串行 await 所有 pending 任务。"""
        while pending_async:
            batch = pending_async[:]
            pending_async.clear()
            for coro in batch:
                try:
                    await asyncio.ensure_future(coro)
                except Exception as exc:
                    downgrade(exc, where='core/agent.py')
    async def _stream_answer(
        self,
        user_input: str,
        tool_results: dict,
        skill,
        *,
        profile_block: str,
        capability_block: str,
        goal_note: str,
        trace,
        turn_start: float,
        emit,
    ) -> str:
        """流式生成回答，同时记录「首 token 延迟」与「生成长度」。"""
        from core.trace import STAGE_FIRST_TOKEN, STAGE_GENERATE

        buf = []
        gen_start = time.perf_counter()
        first_seen = False
        async for d in self.orchestrator.stream(
            user_input, tool_results, skill, self.context,
            profile_block=profile_block,
            capability_block=capability_block,
            goal_note=goal_note,
        ):
            if trace is not None and not first_seen:
                trace.since(STAGE_FIRST_TOKEN, turn_start)
                first_seen = True
            buf.append(d)
            emit(EV_MESSAGE_DELTA, {"delta": d})
        if trace is not None:
            trace.since(STAGE_GENERATE, gen_start)
        return self._ensure_answer("".join(buf))

    async def _handle_quick_intents(self, tc: TurnContext) -> Optional[str]:
        """处理不需要跑工具链的三种意图：闲聊 / 教导 / 重试。

        返回 `None` 表示"不属于快捷意图，继续走正常工具链"；
        返回字符串表示该轮已处理完毕，直接作为回答。

        这三个分支原本内联在 `handle` 里，占 60+ 行且各自有 `return`，
        是最容易独立测试的 stage —— 不需要跑 goal loop 就能验证行为。
        """
        from core.trace import STAGE_PLAN  # noqa: F401 - 保持与 handle 的导入时机一致

        user_input = tc.user_input
        plan = tc.plan
        profile_block = tc.profile_block
        capability_block = tc.capability_block
        goal_note = ""
        trace = tc.trace
        turn_start = tc.turn_start
        emit = tc.emit
        pending_async = tc.pending_async

        intent_type = plan.intent

        # 闲聊:直接回答,不记录到记忆
        if intent_type == IntentType.CHITCHAT:
            emit(EV_THINKING, {"stage": "chitchat", "detail": plan.intent_detail})
            ans = await self._stream_answer(
                user_input, {}, None,
                profile_block=profile_block,
                capability_block=capability_block,
                goal_note=goal_note,
                trace=trace,
                turn_start=turn_start,
                emit=emit,
            )
            self.context.add_assistant_message(ans)
            emit(EV_MESSAGE_FINAL, {"content": ans})
            await self._drain_pending(pending_async)
            # 闲聊不进行 ExecutionCritic 评估,避免污染记忆库
            return ans

        # 教导意图:进入技能学习流程
        if intent_type == IntentType.TEACH:
            emit(EV_THINKING, {"stage": "teaching_detect"})
            ok, result, skill = await self.trainer.teach(user_input)

            if ok and skill:
                emit(EV_SKILL_LEARNED, {
                    "name": skill.name,
                    "version": skill.version,
                    "message": result,
                })
                self.context.add_assistant_message(result)
                emit(EV_MESSAGE_FINAL, {"content": result})
                await self._drain_pending(pending_async)
                return result

            if result and not ok:
                emit(EV_TEACHING_INTERACTIVE, {"questions": result})
                complete_msg = await self._complete_teaching(user_input, result)
                self.context.add_assistant_message(complete_msg)
                emit(EV_MESSAGE_FINAL, {"content": complete_msg})
                await self._drain_pending(pending_async)
                return complete_msg
            # 教导失败,继续后续流程

        # 重试意图:重新执行上次的任务
        if intent_type == IntentType.RETRY:
            emit(EV_THINKING, {"stage": "retry"})
            last_tasks = self._get_last_tool_tasks()
            if last_tasks:
                self.logger.info("Agent", f"重试执行上次的 {len(last_tasks)} 个工具")
                plan.tool_tasks = last_tasks
            else:
                emit(EV_THINKING, {"stage": "no_retry_history"})
                ans = (
                    "上一轮没有需要执行的工具,无法重试"
                    if last_tasks is not None
                    else "没有找到上次的执行记录,无法重试"
                )
                self.context.add_assistant_message(ans)
                emit(EV_MESSAGE_FINAL, {"content": ans})
                await self._drain_pending(pending_async)
                return ans

        return None

    async def handle(
        self,
        user_input: str,
        on_event: Optional[Callable] = None,
    ) -> str:
        """跑一轮对话。

        这里只表达"这一轮怎么走"：准备 → 规划 → 分流 → 执行 → 生成 → 落库 → 收尾。
        每一步的实现都在对应的 stage 方法里，跨 stage 的状态全在 `TurnContext` 上 ——
        不再靠闭包捕获十几个局部变量（那正是它以前拆不动的原因）。
        """
        tc = TurnContext(
            user_input=user_input,
            start=time.time(),
            turn_start=time.perf_counter(),
        )
        await self._begin_turn(tc)
        tc.emit = self._make_emitter(on_event, tc.pending_async)
        self._install_global_emitter(tc)

        try:
            await self._plan_turn(tc)

            # 意图不明 → 进 L3 澄清层:问一句,而不是硬猜
            if tc.plan.intent == IntentType.CLARIFY:
                tc.answer = await self._handle_clarification(tc)
                return tc.answer

            # 本轮正常推进 → 澄清计数归零。
            # 限制的是**连续**澄清:用户答完能问出结果,就允许下一轮再问。
            self._reset_clarify_state()

            # 快捷意图：闲聊 / 教导 / 重试 各自是一个 stage
            quick = await self._handle_quick_intents(tc)
            if quick is not None:
                # finally 里的 _finish_trace 用 `ans` 记 reply_chars ——
                # 快捷意图的回答必须回填，否则 trace 显示"回复 0 字"。
                tc.answer = quick
                return tc.answer

            await self._execute_goal_loop(tc)
            tc.answer = await self._synthesize(tc)
            self._persist_turn(tc)
            return tc.answer

        except Exception as e:
            self.logger.error("Agent", f"ERROR: {e}")
            tc.emit(EV_ERROR, {"message": str(e)})
            tc.error = str(e)
            raise
        finally:
            await self._end_turn(tc)

    def _install_global_emitter(self, tc: TurnContext) -> None:
        """把 emitter 放进执行上下文。

        工具（如 run_subagents）和子智能体深处没有 `on_event` 参数可传，
        靠这个把进度事件推出来。

        注意 `tc.emit` 是**同步**函数（它把协程塞进 `pending_async` 再统一 await），
        而 event_bus 需要 async 接口，所以这里包一层。
        """
        try:
            from core.event_bus import set_emitter

            async def _async_emit(event: str, payload: dict):
                tc.emit(event, payload)

            set_emitter(_async_emit)
        except Exception as exc:
            downgrade(exc, where='core/agent.py')

    def _reset_clarify_state(self) -> None:
        """本轮正常推进 → 澄清计数归零。

        限制的是**连续**澄清：用户答完能问出结果，就允许下一轮再问。
        """
        self.context.metadata["clarify_round"] = 0
        self.context.metadata.pop("clarify_question", None)
        self.context.metadata.pop("clarify_options", None)

    async def _plan_turn(self, tc: TurnContext) -> None:
        """阶段 1：意图规划。结果写回 `tc.plan`。"""
        emit = tc.emit
        emit(EV_THINKING, {"stage": "planning"})
        plan_start = time.perf_counter()
        plan = await self.manager.plan(
            tc.user_input,
            self.context,
            session_id=self.session_id,
            profile_block=tc.profile_block,
        )
        tc.plan = plan

        if tc.trace is not None:
            from core.trace import STAGE_PLAN

            tc.trace.since(STAGE_PLAN, plan_start)
            tc.trace.intent_layer = plan.intent_layer or ""
            tc.trace.intent = str(getattr(plan.intent, "value", plan.intent))
            tc.trace.intent_confidence = float(plan.intent_confidence or 0.0)

        # 工作记忆：计划进工作台，供重规划 / 回答 / 重试读取
        if self.working is not None:
            self.working.set_plan(plan)

    async def _execute_goal_loop(self, tc: TurnContext) -> None:
        """阶段 3：执行工具链（含重规划）。

        一轮规划常常做不完:要造工具、要先查再算、要失败重来。
        循环由规划层的 `goal_done` 驱动 —— 它说"够了"就停,
        所以「你好」「算 1+1」这类请求行为和以前完全一样(只跑一轮)。
        """
        from core.goal_runner import run_goal_loop

        emit = tc.emit
        emit(EV_PLAN, {
            "intent": tc.plan.intent,
            "skill": tc.skill_name,
            "tasks": tc.plan.tool_tasks,
        })

        # 提取实体,供 Skill DAG 的 params 用 (${user_input.city})
        tc.entities = self._extract_entities(tc.user_input)

        # 让子智能体知道"这是谁在问"、"前面聊到哪了"
        try:
            from core.subagents import set_current_context

            # 共享主对话上下文 —— 否则子智能体不知道"发"指的是什么。
            # 给**快照**而不是本体:子智能体是并发跑的(gather),
            # 共享本体会看到互相穿插的消息列表。快照不带 sink,
            # 也不会把子智能体看到的东西再落一遍库。
            set_current_context(self.context.snapshot())
        except Exception as exc:
            downgrade(exc, where='core/agent.py')

        tools_start = time.perf_counter()
        goal = await run_goal_loop(
            first_plan=tc.plan,
            plan_fn=lambda prior: self._replan(tc, prior),
            execute_fn=lambda p: self._run_plan_tasks(tc, p),
            emit=emit,
            enabled=self._goal_loop_enabled(),
            max_rounds=self._goal_max_rounds(),
            budget_s=self._goal_budget_s(),
            is_clarify=lambda p: getattr(p, "intent", None) == IntentType.CLARIFY,
        )
        tc.goal = goal
        tc.tool_results = goal.tool_results

        # 工作记忆：本轮工具返回 + 最终计划（回答/重试都从这里取，不再靠局部变量传递）
        if self.working is not None:
            self.working.set_tool_results(tc.tool_results)
            self.working.set_plan(tc.plan)
        emit(EV_THINKING, {"stage": "goal_loop_done", **goal.to_dict()})

        # 存储本次 tool_tasks 用于重试
        # 独立字段,不挂在 messages[-1] 上 —— 挂消息会挂错
        # (写到这里时最后一条还是用户消息),而且空列表会被
        # 当成"没有记录",导致重试重跑更早一轮的旧工具。
        self.context.set_last_tool_tasks(tc.plan.tool_tasks)

        if tc.trace is not None:
            from core.trace import STAGE_TOOLS

            tc.trace.since(STAGE_TOOLS, tools_start)
            tc.trace.tool_count = len(tc.tool_results)
            tc.trace.tool_failed = sum(
                1 for r in tc.tool_results.values() if not getattr(r, "success", True)
            )

    async def _run_plan_tasks(self, tc: TurnContext, plan) -> Dict:
        """执行一份计划里的工具任务（`run_goal_loop` 每轮的 execute_fn）。

        DAG 优先：如果启用了 Skill DAG 且计划关联到了带结构化 steps 的技能，
        先用 `DAGExecutor` 把 `skill.steps` 翻成 ToolTask；翻不出来再退回
        规划层给的 `tool_tasks`。
        """
        emit = tc.emit
        results: Dict = {}
        if not plan.tool_tasks:
            return results
        emit(EV_THINKING, {"stage": "tools_running", "count": len(plan.tool_tasks)})

        async def on_tool_event(event: str, payload: dict):
            emit(event, payload)

        planned_tasks: List[ToolTask] = []
        used_dag = False
        if self.dag_enabled and plan.selected_skill and plan.selected_skill.has_structured_steps():
            skill_tasks, issues = self.dag.skill_to_tasks(plan.selected_skill)
            for i in issues:
                emit(EV_THINKING, {"stage": "dag_skip", "issue": i})
            if skill_tasks:
                planned_tasks = skill_tasks
                used_dag = True
                emit(EV_THINKING, {
                    "stage": "skill_dag_used",
                    "skill": plan.selected_skill.name,
                    "count": len(planned_tasks),
                })

        if not used_dag:
            for i, t in enumerate(plan.tool_tasks):
                task_id = t.get("id") or f"t{i+1}"
                planned_tasks.append(ToolTask(
                    id=task_id,
                    type=t.get("type", ""),
                    params=t.get("params", {}),
                    depends_on=t.get("depends_on", []) or [],
                    retry=int(t.get("retry", 0) or 0),
                    timeout_s=int(t.get("timeout_s", 30) or 30),
                    fallback_to=t.get("fallback_to"),
                ))
        tc.planned_tasks = planned_tasks
        return await self.learning.execute_dag(
            planned_tasks, on_tool_event,
            user_input={"text": tc.user_input, **tc.entities}
        )

    async def _replan(self, tc: TurnContext, prior_findings: str):
        """目标没做完 → 带着上一轮的结论重新规划，并更新 `tc.plan`。"""
        # 上一轮的结论进工作记忆，重规划时回灌（goal_runner 没给就用已记的）
        if self.working is not None:
            if prior_findings:
                self.working.add_finding(prior_findings)
            prior_findings = prior_findings or self.working.findings_text()
        new_plan = await self.manager.plan(
            tc.user_input,
            self.context,
            session_id=self.session_id,
            profile_block=tc.profile_block,
            prior_findings=prior_findings,
        )
        tc.plan = new_plan
        return new_plan

    async def _synthesize(self, tc: TurnContext) -> str:
        """阶段 4：生成回答、落库、把产物推给前端。"""
        emit = tc.emit
        goal_note = ""
        # 没真的做完时,要把原因交给回答层 —— 不能说得像全成了
        if tc.goal is not None and not tc.goal.completed:
            goal_note = (
                f"注意：这一轮没有完全完成目标（{tc.goal.explanation}，"
                f"共跑了 {tc.goal.rounds} 轮）。回答时必须如实说明"
                f"哪些部分没做成、卡在哪,不要把未完成说成已完成。"
            )
            emit(EV_THINKING, {"stage": "goal_incomplete", "detail": goal_note})
        emit(EV_THINKING, {"stage": "synthesizing"})
        try:
            ans = await self._stream_answer(
                tc.user_input, tc.tool_results, tc.skill,
                profile_block=tc.profile_block,
                capability_block=tc.capability_block,
                goal_note=goal_note,
                trace=tc.trace,
                turn_start=tc.turn_start,
                emit=emit,
            )
        except Exception:
            # 流式通道挂了就退到非流式一次性生成 —— 用户至少能拿到答案
            ans = await self.orchestrator.orchestrate(
                tc.user_input, tc.tool_results, tc.skill, self.context,
                profile_block=tc.profile_block,
                capability_block=tc.capability_block,
                goal_note=goal_note,
            )
            emit(EV_MESSAGE_DELTA, {"delta": ans})

        ans = self._ensure_answer(ans)
        # 本轮的工具计划跟着这条助手消息一起落库 —— 会话恢复后
        # 才能知道上一轮调了哪些工具(重试要用)。
        self.context.add_assistant_message(ans, tool_tasks=tc.plan.tool_tasks)
        # 把本轮产物一起给前端 —— 否则"生成的视频/报表"做出来了用户也看不到
        emit(EV_MESSAGE_FINAL, {
            "content": ans,
            "artifacts": self._collect_artifacts(tc.tool_results),
        })

        tc.duration_ms = (time.time() - tc.start) * 1000
        self.logger.info("Agent", f"DONE: {tc.duration_ms:.0f}ms")
        return ans

    def _persist_turn(self, tc: TurnContext) -> None:
        """阶段 5：把这一轮的轨迹交给后台（critic / 技能收割 / 工具自修复）。

        全部挂到 `pending_async`，由 `_end_turn` 统一 await —— 不阻塞回答。
        """
        execution_context = build_execution_context(
            trace_id=self.trace_id,
            scenario=tc.skill_name or "",
            intent=tc.plan.intent,
            selected_skill=tc.skill_name,
            tool_results=tc.tool_results,
            latency_ms=tc.duration_ms,
            # M0-02:传入任务规格,让 critic 拿到真实工具名/retry/fallback
            task_specs={
                t.id: {
                    "type": t.type,
                    "retry": t.retry,
                    "timeout_s": t.timeout_s,
                    "fallback_to": t.fallback_to,
                }
                for t in tc.planned_tasks
            },
            # M0-01:四级标识
            execution_id=self.execution_id,
            turn_id=self.turn_id,
            session_id=self.identity.session_id if self.identity else self.session_id,
        )
        # 工具自修复：自己造的工具反复失败 → 让它自己修一版
        # （后台跑，不阻塞回答；只动 agent 自己造的，内置源码不碰）
        tc.pending_async.append(asyncio.ensure_future(self._repair_tools()))

        # 方法论自增长：**只有真的做完了**才沉淀经验
        # （失败的轨迹没有复用价值；提炼本身在后台跑，不阻塞回答）
        if tc.goal is not None and tc.goal.completed:
            tc.pending_async.append(asyncio.ensure_future(
                self._harvest_skill(
                    tc.user_input, tc.tool_results, tc.goal.rounds,
                    tc.planned_tasks,
                )
            ))

        # 异步执行 critic,收集到 pending_async 末尾 await
        tc.pending_async.append(asyncio.ensure_future(
            self._evaluate_with_skill_quality(execution_context)
        ))
        # 存住这一轮的执行上下文 —— 下一轮用户如果纠正,
        # 就能拿它重评(docs/14 §2.3)
        self._last_exec_ctx = execution_context
        if execution_context.execution_id:
            self._exec_ctxs[execution_context.execution_id] = execution_context
            # 只留最近 20 轮,别无限涨
            while len(self._exec_ctxs) > 20:
                self._exec_ctxs.popitem(last=False)

    async def _end_turn(self, tc: TurnContext) -> None:
        """轮末收尾：还原执行上下文、drain 事件、收 trace、收工作记忆。

        **不管这一轮成功还是抛异常，都必须走这里** —— 它是生命周期边界。
        """
        # 还原执行上下文（子智能体各自设置自己的 agent_id）
        try:
            from core.exec_context import reset_exec_context

            reset_exec_context(self._exec_tokens)
        except Exception as exc:
            downgrade(exc, where='core/agent.py')

        # 统一 await 所有异步 emit,保证事件按 emit 顺序完成
        if tc.pending_async:
            results = await asyncio.gather(*tc.pending_async, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    self.logger.warning("Agent", f"emit task failed: {r}")

        if tc.trace is not None:
            tc.trace.reply_chars = len(tc.answer)
            self._finish_trace(tc.trace, tc.turn_start, error=tc.error)

        # 工作记忆收尾：失败先落盘取证，然后清空。
        # **必须在这里做** —— 它是一轮的生命周期边界，而不是 GC 的事。
        try:
            dumped = self.mem.end_turn(
                self.working,
                failed=bool(tc.error),
                reason=(tc.error or "")[:200],
            )
            if dumped:
                self.logger.info("Agent", f"本轮失败，工作记忆已落盘: {dumped}")
        except Exception as exc:
            self.logger.warning("Agent", f"工作记忆收尾失败: {exc}")
        finally:
            self.working = None

    async def _handle_clarification(self, tc: TurnContext) -> str:
        """L3 澄清:问一句带候选项的问题。

        约束(docs/12 §2.6):
        - 只问一个问题
        - 澄清轮次上限 1 轮;超了就直接答,不能把人卡住
        - 澄清轮**不进 critic、不写长期记忆** —— 它不是一次任务执行
        """
        from core.clarifier import detect_missing_slot, get_clarifier

        user_input = tc.user_input
        plan = tc.plan
        emit = tc.emit

        async def drain() -> None:
            """把澄清事件推完之后再返回 —— 与其它 stage 的事件顺序语义一致。"""
            await self._drain_pending(tc.pending_async)

        clarifier = get_clarifier()
        round_index = int(self.context.metadata.get("clarify_round", 0)) + 1
        slots = dict(plan.intent_slots or {})
        slot = detect_missing_slot(slots)

        emit(EV_THINKING, {"stage": "clarifying", "round": round_index})

        request = await clarifier.clarify(
            user_input,
            reason=plan.intent_detail,
            slot=slot,
            slots=slots,
            recent_turns=self.manager._recent_turns(self.context, limit=2),
            skill_names=[s.name for s in self.manager.skill_store.list_all()],
            round_index=round_index,
        )

        if request is None:
            # 澄清轮次已用尽 / 造不出有意义的问题 → 直接答,别把人卡住
            self.logger.info("Agent", "澄清不可用,改为直接回答")
            ans = await self.orchestrator.orchestrate(
                user_input, {}, None, self.context,
                profile_block=self._get_profile_block(),
            )
            ans = self._ensure_answer(ans)
            self.context.add_assistant_message(ans)
            emit(EV_MESSAGE_FINAL, {"content": ans})
            await drain()
            return ans

        self.logger.info(
            "Agent",
            f"澄清第 {request.round} 轮: {request.question}"
            f"(选项 {request.options},来源 {request.source})",
        )

        # 记录待澄清状态:下一轮 L1/L2 能看到"我问过什么、用户答了什么"
        self.context.metadata["clarify_round"] = request.round
        self.context.metadata["clarify_question"] = request.question
        self.context.metadata["clarify_options"] = list(request.options)

        text = request.to_text()
        self.context.add_assistant_message(text)
        # 顺序:先 delta 让前端建出气泡,再挂澄清选项,最后定稿
        emit(EV_MESSAGE_DELTA, {"delta": text})
        emit(EV_CLARIFICATION_REQUEST, request.to_dict())
        emit(EV_MESSAGE_FINAL, {"content": text})
        await drain()
        # 澄清轮不进 critic:这里直接返回,不追加 critic 任务
        return text

    def reset(self):
        """清空工作上下文。

        **不删除已落库的消息** —— 会话内容始终保留、可检索;
        只是把"工作上下文"的起点挪到当前位置,恢复会话时不再装回旧内容。
        """
        self.context.clear()
        # 工作记忆跟着一起清 —— reset 的语义是"忘掉这一轮在做什么"
        if self.working is not None:
            try:
                self.mem.end_turn(self.working)
            except Exception as exc:
                downgrade(exc, where='core/agent.py')
            self.working = None
        if self._session_store is not None:
            try:
                self.mem.short_term.reset(self.session_id)
            except Exception as exc:
                self.logger.warning("Agent", f"标记会话重置失败: {exc}")
        self.logger.info("Agent", "RESET")

    def chat(
        self,
        user_input: str,
        on_event: Optional[Callable] = None,
    ) -> str:
        """同步适配入口 —— 供 CLI / Streamlit 等同步调用方使用。

        内部走与 WebSocket 完全相同的异步主链路 ``handle()``,
        避免出现两套行为不一致的实现。
        """
        return _run_sync(self.handle(user_input, on_event))

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
            # Agent 本身不是 BaseAgent,借用 orchestrator 的 LLM 客户端
            response = await self.orchestrator.llm.chat([
                {"role": "user", "content": prompt},
            ])
            # M1-07:不要把助手的追问当作用户消息回写 context。
            # 追问由调用方以 assistant 身份写入;用户消息在 handle() 入口已写入。
            return response
        except Exception as e:
            self.logger.error("Agent", f"交互式教导失败: {e}")
            return "好的，请继续告诉我更多信息。"

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
