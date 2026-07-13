"""ExecutionCritic - 执行批评器:在每次 DAG 执行后评估结果质量"""
import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.memory import FailureRecord, MemoryStore, SkillPatch, get_memory_store
from infra.logger import get_logger
from tools.base import ToolResult


# ---- Feature Flag ----
SELF_EVOLUTION_ENABLED = False  # 启动时由 infra.config 覆盖


def _load_flag():
    """延迟加载 feature flag"""
    try:
        from infra.config import config
        return bool(config.self_evolution_enabled)
    except Exception:
        return False


def get_self_evolution_enabled() -> bool:
    """获取 feature flag(延迟加载)"""
    return _load_flag()


@dataclass
class EvaluationResult:
    """评估结果"""
    trace_id: str
    scenario: str
    success_rate: float
    fallback_count: int
    latency_ms: float
    user_corrected: bool
    diagnosis: str
    suggestion: Optional[Dict] = None
    confidence: float = 0.5
    records_generated: List[str] = field(default_factory=list)  # 生成的处理类型列表


@dataclass
class TaskExecutionSummary:
    """单个任务执行摘要"""
    task_id: str
    tool: str
    success: bool
    used_fallback: bool = False
    retry_count: int = 0
    error: str = ""


@dataclass
class ExecutionContext:
    """执行上下文:包含一次完整执行的所有信息"""
    trace_id: str
    scenario: str
    intent: str
    selected_skill: Optional[str]
    tasks: List[TaskExecutionSummary]
    latency_ms: float
    user_feedback: Optional[str] = None  # 下一轮用户是否有纠正


class ExecutionCritic:
    """执行批评器

    职责:在每次 DAG 执行后,评估结果质量,决定是否生成改进建议。

    评估维度:
    - success_rate: 成功工具数 / 总工具数
    - fallback_count: 触发了 fallback 的次数
    - latency_ms: 总执行耗时
    - user_feedback: 用户是否追问/纠正

    评估策略:
    - success_rate < 0.5: 生成失败分析报告,存入 MemoryStore
    - fallback_count > 0 且结果仍差: 生成改进建议 SkillPatch
    - success_rate == 1.0 且快速: 记录成功路径,强化匹配权重
    - user_feedback == correction: 分析用户纠正,生成修正版 SkillPatch

    注意:Critic 完全异步执行,不影响 Agent.handle() 的返回时间。
    """

    def __init__(self, memory_store: Optional[MemoryStore] = None):
        self.memory = memory_store or get_memory_store()
        self.logger = get_logger()
        self._llm_client = None  # 延迟初始化

    def _get_llm_client(self):
        """延迟初始化 LLM 客户端"""
        if self._llm_client is None:
            try:
                from infra.llm import get_llm_client
                self._llm_client = get_llm_client()
            except Exception as e:
                self.logger.warning("ExecutionCritic", f"无法初始化 LLM: {e}")
        return self._llm_client

    async def evaluate(
        self,
        context: ExecutionContext,
    ) -> EvaluationResult:
        """评估一次执行的质量,生成诊断和建议。

        这是异步方法,但应通过 ensure_future 在后台调用,
        不阻塞主流程。
        """
        if not get_self_evolution_enabled():
            return EvaluationResult(
                trace_id=context.trace_id,
                scenario=context.scenario,
                success_rate=1.0,
                fallback_count=0,
                latency_ms=context.latency_ms,
                user_corrected=False,
                diagnosis="self_evolution_disabled",
            )

        self.logger.info("ExecutionCritic", f"评估执行: {context.trace_id}")

        # 计算评估维度
        total_tasks = len(context.tasks)
        if total_tasks == 0:
            success_rate = 1.0
        else:
            success_count = sum(1 for t in context.tasks if t.success)
            success_rate = success_count / total_tasks

        fallback_count = sum(1 for t in context.tasks if t.used_fallback)
        user_corrected = context.user_feedback is not None

        # 生成诊断和建议
        diagnosis, suggestion, confidence = await self._analyze(
            context, success_rate, fallback_count
        )

        # 根据评估结果决定记录什么
        records_generated: List[str] = []

        # 1. 失败情况:记录失败(success_rate < 1.0 都记录)
        if success_rate < 1.0:
            failure_record = self.memory.record_failure(
                trace_id=context.trace_id,
                scenario=context.scenario,
                intent=context.intent,
                selected_skill=context.selected_skill or "",
                success_rate=success_rate,
                fallback_count=fallback_count,
                latency_ms=context.latency_ms,
                diagnosis=diagnosis,
                suggestion=suggestion,
                user_corrected=user_corrected,
            )
            records_generated.append("failure_record")

        # 2. 有改进建议且置信度高:生成 SkillPatch
        if suggestion and confidence >= 0.7:
            patch = SkillPatch(
                id=f"patch_{context.trace_id}_{datetime.now().strftime('%H%M%S')}",
                trace_id=context.trace_id,
                timestamp=datetime.now().isoformat(),
                target_skill=context.selected_skill or "",
                patch_type="improve_skill",
                diagnosis=diagnosis,
                suggestion=suggestion,
                confidence=confidence,
                status="pending",
            )

            # 高置信度(>0.9)自动生效
            if confidence >= 0.9:
                patch.status = "auto_approved"
                records_generated.append("patch_auto_approved")
            else:
                self.memory.add_pending_patch(patch)
                records_generated.append("patch_pending")

        # 3. 成功情况:记录成功路径
        if success_rate == 1.0 and fallback_count == 0:
            self.memory.record_success(
                trace_id=context.trace_id,
                scenario=context.scenario,
                matched_skill=context.selected_skill or "",
                latency_ms=context.latency_ms,
                pattern=context.intent,
            )
            records_generated.append("success_record")

        # 4. 用户纠正:分析修正
        if user_corrected and context.user_feedback:
            patch = SkillPatch(
                id=f"patch_{context.trace_id}_correction_{datetime.now().strftime('%H%M%S')}",
                trace_id=context.trace_id,
                timestamp=datetime.now().isoformat(),
                target_skill=context.selected_skill or "",
                patch_type="fix_method",
                diagnosis=f"用户纠正: {context.user_feedback}",
                suggestion={"type": "user_correction", "content": context.user_feedback},
                confidence=0.85,
                status="pending",
            )
            self.memory.add_pending_patch(patch)
            records_generated.append("patch_correction")

        result = EvaluationResult(
            trace_id=context.trace_id,
            scenario=context.scenario,
            success_rate=success_rate,
            fallback_count=fallback_count,
            latency_ms=context.latency_ms,
            user_corrected=user_corrected,
            diagnosis=diagnosis,
            suggestion=suggestion,
            confidence=confidence,
            records_generated=records_generated,
        )

        self.logger.info(
            "ExecutionCritic",
            f"评估完成: rate={success_rate:.2f}, fallback={fallback_count}, "
            f"records={records_generated}"
        )
        return result

    async def _analyze(
        self,
        context: ExecutionContext,
        success_rate: float,
        fallback_count: int,
    ) -> tuple[str, Optional[Dict], float]:
        """分析执行情况,生成诊断和建议。

        策略:
        1. 优先用规则快速判断(无 LLM 调用)
        2. 仅在规则不足以判断时调用 LLM

        Returns: (diagnosis, suggestion, confidence)
        """
        # 规则1: 完全成功
        if success_rate == 1.0 and fallback_count == 0:
            return "执行完全成功,无需改进", None, 1.0

        # 规则2: 完全失败
        if success_rate == 0.0:
            failed_tools = [t.tool for t in context.tasks if not t.success]
            return (
                f"所有工具执行失败: {failed_tools}",
                None,  # 需要人工分析
                0.3,
            )

        # 规则3: 部分成功
        if success_rate < 1.0:
            failed_tasks = [t for t in context.tasks if not t.success]
            success_tasks = [t for t in context.tasks if t.success]

            diagnosis_parts = []
            suggestion_parts = []

            for t in failed_tasks:
                if t.error:
                    diagnosis_parts.append(f"{t.tool} 失败: {t.error}")
                    # 根据错误类型生成建议
                    if "超时" in t.error:
                        suggestion_parts.append(f"增加 {t.tool} 的 timeout_s")
                    elif "不存在" in t.error:
                        suggestion_parts.append(f"检查 {t.tool} 的参数是否正确")

            for t in success_tasks:
                if t.used_fallback:
                    diagnosis_parts.append(f"{t.tool} 使用了 fallback")
                    suggestion_parts.append(f"{t.tool} 的主路径不稳定,建议优化")

            diagnosis = "; ".join(diagnosis_parts) if diagnosis_parts else "部分工具执行失败"
            suggestion = None
            if suggestion_parts:
                suggestion = {
                    "type": "improve_skill",
                    "target_skill": context.selected_skill,
                    "recommendations": suggestion_parts,
                }

            # 置信度:基于成功率和是否有具体建议
            confidence = success_rate * 0.5 + (0.5 if suggestion else 0)

            return diagnosis, suggestion, confidence

        return "未知情况", None, 0.0

    async def suggest_improvements(
        self,
        context: ExecutionContext,
    ) -> Optional[Dict]:
        """可选:对复杂场景调用 LLM 生成改进建议(仅在 confidence < 0.7 时调用)"""
        llm = self._get_llm_client()
        if llm is None:
            return None

        # 构建 prompt
        prompt = self._build_improvement_prompt(context)
        if not prompt:
            return None

        try:
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": "你是一个技能优化专家。根据执行情况,给出改进建议。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            # 尝试解析 JSON
            import json, re
            m = re.search(r"\{[\s\S]*\}", response)
            if m:
                return json.loads(m.group())
        except Exception as e:
            self.logger.warning("ExecutionCritic", f"LLM 建议生成失败: {e}")

        return None

    def _build_improvement_prompt(self, context: ExecutionContext) -> str:
        """构建改进建议 prompt"""
        if context.selected_skill is None:
            return ""

        lines = [
            f"场景: {context.scenario}",
            f"意图: {context.intent}",
            f"技能: {context.selected_skill}",
            f"执行结果:",
        ]

        for t in context.tasks:
            status = "✓" if t.success else "✗"
            fallback = " (fallback)" if t.used_fallback else ""
            retry = f" (重试{t.retry_count}次)" if t.retry_count > 0 else ""
            error = f" - {t.error}" if t.error else ""
            lines.append(f"  {status} {t.tool}{fallback}{retry}{error}")

        lines.append(f"耗时: {context.latency_ms:.0f}ms")
        lines.append("")
        lines.append("请分析失败原因,给出改进建议。输出 JSON 格式:")
        lines.append('{"diagnosis": "...", "suggestion": {...}, "confidence": 0.0~1.0}')

        return "\n".join(lines)


def build_execution_context(
    trace_id: str,
    scenario: str,
    intent: str,
    selected_skill: Optional[str],
    tool_results: Dict[str, ToolResult],
    latency_ms: float,
    user_feedback: Optional[str] = None,
) -> ExecutionContext:
    """从工具执行结果构建 ExecutionContext"""
    tasks: List[TaskExecutionSummary] = []

    for task_id, result in tool_results.items():
        # 从 task_id 推断 tool 名(格式: t1, t2...)
        tool_name = task_id if task_id.startswith("tool:") else f"task_{task_id}"
        tasks.append(TaskExecutionSummary(
            task_id=task_id,
            tool=tool_name,
            success=result.success,
            used_fallback=False,  # TODO: 从 meta 中读取
            retry_count=0,  # TODO: 从 meta 中读取
            error=result.error or "",
        ))

    return ExecutionContext(
        trace_id=trace_id,
        scenario=scenario,
        intent=intent,
        selected_skill=selected_skill,
        tasks=tasks,
        latency_ms=latency_ms,
        user_feedback=user_feedback,
    )
