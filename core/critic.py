"""ExecutionCritic - 执行批评器:在每次 DAG 执行后评估结果质量"""
import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.memory import FailureRecord, MemoryStore, SkillPatch, get_memory_store
from infra.config import get_self_evolution_enabled
from infra.logger import get_logger
from tools.base import ToolResult


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
    # M0-02:补齐执行记录字段
    fallback_to: Optional[str] = None   # 声明的 fallback 目标任务 id
    latency_ms: float = 0.0            # 单任务耗时


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
    # M0-01:四级标识(每次 handle 唯一 execution_id,同 session 多轮不互相覆盖)
    execution_id: str = ""
    turn_id: str = ""
    user_id: str = "default"
    session_id: str = "default"


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

        # ===== 记录区:任何失败都只记日志,不影响本轮 =====
        #
        # **自演化是"锦上添花",它挂了不能影响用户拿答案。**
        # 实机踩过:目录被外部清理掉(log 清理/部署脚本)之后,
        # `record_failure` 抛 FileNotFoundError,整个 evaluate() 崩掉,
        # 错误直接冒到用户界面上 —— 用户看到的是"[WinError 3]",
        # 而不是他的回答。
        #
        # 存储层已经自愈(目录没了会自己建),这里再加一道:
        # 万一还有别的 IO 问题(磁盘满/权限),也只丢一条记录,不影响其他。
        def _safe_write(action: str, fn, *args, **kwargs) -> bool:
            try:
                fn(*args, **kwargs)
                return True
            except Exception as exc:
                self.logger.warning(
                    "ExecutionCritic",
                    f"{action} 落盘失败(已忽略,不影响本轮): {exc}",
                )
                return False

        # 1. 失败情况:记录失败(success_rate < 1.0 **或被用户纠正**)
        #
        # 被纠正是比"工具失败"更强的信号:工具全调通了但答案是错的,
        # 在旧逻辑里会被记成"成功",学习系统就永远学不到这件事。
        if success_rate < 1.0 or user_corrected:
            if _safe_write(
                "失败记录",
                self.memory.record_failure,
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
                # M0-01:用 execution_id 落盘,同 session 多轮失败不再互相覆盖
                execution_id=context.execution_id or context.trace_id,
                user_id=context.user_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
            ):
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

            # 高置信度(>0.9)才尝试自动发布,但必须过校验闸(M3-06)
            if confidence >= 0.9:
                applied, report, reason = await self._try_auto_publish(patch)
                if report is not None:
                    patch.validation = report.to_dict()
                    patch.risk_level = report.risk_level
                if applied:
                    patch.status = "auto_approved"
                    records_generated.append("patch_auto_approved")
                else:
                    # 没过闸 → 降级为人工审批,并说明原因(M3-05 风险展示)
                    patch.status = "pending"
                    patch.blocked_reason = reason
                    patch.risk_level = "high"
                    records_generated.append("patch_auto_publish_blocked")
                    self.logger.warning(
                        "ExecutionCritic",
                        f"自动发布被拦截({patch.id}): {reason}",
                    )
            else:
                records_generated.append("patch_pending")
            if _safe_write("待审补丁", self.memory.add_pending_patch, patch):
                records_generated.append("patch_recorded")

        # 3. 成功情况:记录成功路径
        #    被用户纠正的轮次**不算成功** —— 它已经在上面记成失败了
        if success_rate == 1.0 and fallback_count == 0 and not user_corrected:
            if _safe_write(
                "成功记录",
                self.memory.record_success,
                trace_id=context.trace_id,
                scenario=context.scenario,
                matched_skill=context.selected_skill or "",
                latency_ms=context.latency_ms,
                pattern=context.intent,
            ):
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
            if _safe_write("纠正补丁", self.memory.add_pending_patch, patch):
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

    @staticmethod
    def _extract_updates(suggestion: Dict) -> Dict[str, object]:
        """从 suggestion 中提取可应用的字段。"""
        updates: Dict[str, object] = {}
        if "method" in suggestion:
            updates["method"] = suggestion["method"]
        if "capability" in suggestion:
            updates["capability"] = suggestion["capability"]
        if "patterns" in suggestion:
            updates["patterns"] = suggestion["patterns"]
        return updates

    async def _try_auto_publish(self, patch: "SkillPatch"):
        """M3-06:高置信度补丁的自动发布,必须同时满足三个条件:

        1. 静态校验通过(结构 / 工具引用 / DAG 无环)
        2. 至少有一个回归样例,且**全部通过**(M3-03 结果校验)
        3. 写审计记录

        任一条不满足 → 不落盘,补丁降级为人工审批,并返回拦截原因。

        Returns:
            (applied: bool, report: Optional[ValidationReport], reason: str)
        """
        from core.result_validator import ResultValidator
        from skills.manager import get_skill_store
        from skills.validator import new_issues, split_issues, validate_skill
        from tools.hub import get_tool_hub

        updates = self._extract_updates(patch.suggestion or {})
        if not updates:
            return False, None, "补丁没有可应用的字段"

        if not patch.target_skill:
            return False, None, "补丁没有指定目标技能"

        store = get_skill_store()
        candidate = store.preview_update(patch.target_skill, updates)
        if candidate is None:
            return False, None, f"找不到技能 {patch.target_skill}"

        try:
            tool_names = list(get_tool_hub().names() or [])
        except Exception:
            tool_names = []

        # 基线已有的问题不阻断补丁,只拦新引入的问题
        baseline = store.get_by_name(patch.target_skill)
        baseline_errors, _ = (
            split_issues(validate_skill(baseline, tool_names)) if baseline else ([], [])
        )
        candidate_errors, warnings = split_issues(validate_skill(candidate, tool_names))
        errors = new_issues(baseline_errors, candidate_errors)

        report = await ResultValidator().validate(
            candidate, static_issues=errors, warnings=warnings
        )

        # 记进技能质量账本:验收样例通过率是"这个技能靠不靠谱"的第一手证据
        # (docs/14 §2.4)。记账失败不该影响发布流程。
        try:
            from core.skill_quality import get_skill_quality_store

            get_skill_quality_store().record_sample_run(
                patch.target_skill,
                version=candidate.version,
                total=report.total,
                passed=report.passed,
                failed_cases=[c.name for c in report.cases if not c.passed],
            )
        except Exception as exc:
            self.logger.warning("Critic", f"记录样例结果失败: {exc}")

        if errors:
            return False, report, "静态校验未通过: " + "; ".join(errors)
        if not report.has_regression:
            return False, report, "技能缺少回归样例,自动发布前需要先补齐"
        if not report.ok:
            failed = [c.name for c in report.cases if not c.passed]
            return False, report, "回归样例未通过: " + ", ".join(failed)

        written = store.update_skill(patch.target_skill, updates)
        if not written:
            return False, report, "写入技能文件失败"

        audit_path = self.memory.write_audit(
            "auto_publish",
            {
                "patch_id": patch.id,
                "target_skill": patch.target_skill,
                "confidence": patch.confidence,
                "diagnosis": patch.diagnosis,
                "updates": updates,
                "files": written,
                "validation": report.to_dict(),
                "reviewer": "auto",
            },
        )
        self.logger.info(
            "ExecutionCritic",
            f"自动发布通过校验: {patch.target_skill} -> {written} (审计: {audit_path})",
        )
        return True, report, ""

    async def _apply_patch(self, patch: "SkillPatch") -> bool:
        """尝试将 auto_approved patch 直接应用到 skill YAML"""
        target_skill = patch.target_skill
        suggestion = patch.suggestion or {}
        if not target_skill or not suggestion:
            return False

        # 构建 updates 字典:从 suggestion 中提取可应用字段
        updates: Dict[str, object] = {}
        if "method" in suggestion:
            updates["method"] = suggestion["method"]
        if "capability" in suggestion:
            updates["capability"] = suggestion["capability"]
        if "patterns" in suggestion:
            updates["patterns"] = suggestion["patterns"]

        if not updates:
            return False

        try:
            from skills.manager import get_skill_store
            store = get_skill_store()
            applied = store.update_skill(target_skill, updates)
            if applied:
                self.logger.info(
                    "ExecutionCritic",
                    f"auto_approved patch {patch.id} 已应用: {target_skill} -> {applied}"
                )
                return True
        except Exception as e:
            self.logger.warning("ExecutionCritic", f"auto_apply patch 失败: {e}")
        return False

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
    *,
    task_specs: Optional[Dict[str, Dict[str, Any]]] = None,
    execution_id: str = "",
    turn_id: str = "",
    user_id: str = "default",
    session_id: str = "default",
) -> ExecutionContext:
    """从工具执行结果构建 ExecutionContext。

    Args:
        task_specs: task_id → 任务规格(type / retry / timeout_s / fallback_to)。
            提供时用真实工具名与声明的 fallback,不再从 task_id 反推(M0-02)。
        execution_id / turn_id / user_id / session_id: 四级标识(M0-01)。
    """
    tasks: List[TaskExecutionSummary] = []
    specs = task_specs or {}

    for task_id, result in tool_results.items():
        spec = specs.get(task_id) or {}
        meta = getattr(result, "meta", {}) or {}

        # 真实工具名:优先 task_specs,其次 meta,最后才退回旧的推断逻辑
        tool_name = (
            spec.get("type")
            or meta.get("tool")
            or (task_id if task_id.startswith("tool:") else f"task_{task_id}")
        )

        # 重试次数:meta.attempts 是实际尝试次数,减 1 即重试次数
        attempts = meta.get("attempts")
        if attempts is not None:
            retry_count = max(0, int(attempts) - 1)
        elif meta.get("retry_count") is not None:
            retry_count = int(meta.get("retry_count") or 0)
        else:
            retry_count = int(spec.get("retry") or 0)

        # fallback:任务声明了 fallback_to,或运行时确实走了 fallback
        fallback_to = spec.get("fallback_to") or meta.get("fallback_to")
        used_fallback = bool(meta.get("used_fallback", False)) or bool(fallback_to)

        tasks.append(TaskExecutionSummary(
            task_id=task_id,
            tool=tool_name,
            success=result.success,
            used_fallback=used_fallback,
            retry_count=retry_count,
            error=result.error or "",
            fallback_to=fallback_to,
            latency_ms=float(meta.get("latency_ms") or 0.0),
        ))

    return ExecutionContext(
        trace_id=trace_id,
        scenario=scenario,
        intent=intent,
        selected_skill=selected_skill,
        tasks=tasks,
        latency_ms=latency_ms,
        user_feedback=user_feedback,
        execution_id=execution_id,
        turn_id=turn_id,
        user_id=user_id,
        session_id=session_id,
    )
