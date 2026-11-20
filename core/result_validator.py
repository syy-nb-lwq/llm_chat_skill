"""M3-03 结果校验器(Result Validator)。

在技能版本发布之前,用该版本的**回归样例**跑一遍真实执行路径,确认:
- 该调用的工具确实被调用了
- 调用没有失败
- 输出里包含预期内容

校验不通过 → 该版本不允许自动发布(M3-06),只能转人工审批并展示风险(M3-05)。

回归样例格式(skill YAML 的 `regression` 字段):
    regression:
      - name: 北京明天天气
        input:
          user_input: "北京明天天气"
        expect:
          tool: weather_query      # 必须调用到的工具
          success: true            # 所有工具都应成功
          contains: ["天气"]        # 输出中应出现的子串
          output_keys: ["code"]    # 工具结果 data 中应存在的键
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from infra.logger import get_logger
from skills.models import Skill


@dataclass
class CaseResult:
    """单个回归样例的结果。"""

    name: str
    passed: bool
    reason: str = ""
    tools_called: List[str] = field(default_factory=list)
    failed_tools: List[str] = field(default_factory=list)
    output: str = ""

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "reason": self.reason,
            "tools_called": self.tools_called,
            "failed_tools": self.failed_tools,
        }


@dataclass
class ValidationReport:
    """一次技能版本校验的完整报告。"""

    skill_name: str
    version: str
    total: int = 0
    passed: int = 0
    cases: List[CaseResult] = field(default_factory=list)
    static_issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        """校验通过:没有静态问题,且所有回归样例都通过。"""
        return not self.error and not self.static_issues and self.total > 0 and self.passed == self.total

    @property
    def has_regression(self) -> bool:
        return self.total > 0

    @property
    def risk_level(self) -> str:
        """风险等级(M3-05:给前端展示)。

        - low    : 有回归样例且全部通过
        - medium : 有回归样例但部分失败
        - high   : 没有回归样例 / 静态校验不通过 / 执行异常
        """
        if self.error or self.static_issues:
            return "high"
        if not self.has_regression:
            return "high"
        if self.passed == self.total:
            return "low"
        return "medium"

    def to_dict(self) -> Dict:
        return {
            "skill_name": self.skill_name,
            "version": self.version,
            "total": self.total,
            "passed": self.passed,
            "ok": self.ok,
            "risk_level": self.risk_level,
            "has_regression": self.has_regression,
            "static_issues": list(self.static_issues),
            "warnings": list(self.warnings),
            "error": self.error,
            "cases": [c.to_dict() for c in self.cases],
        }


class ResultValidator:
    """技能结果校验器。"""

    def __init__(self, learning_agent=None):
        self._learning = learning_agent
        self._executor = None
        self.logger = get_logger()

    # ===== 内部 =====

    def _get_executor(self):
        """拿到 DAG 执行器(懒加载,避免 import 期副作用)。"""
        if self._executor is None:
            from agents.learning import LearningAgent
            from core.dag import DAGExecutor

            if self._learning is None:
                self._learning = LearningAgent()
            self._executor = DAGExecutor(self._learning)
        return self._executor

    @staticmethod
    def _case_input(case: Dict) -> Dict:
        """把样例的 input 规整成 DAG 注入用的上下文。"""
        raw = case.get("input") or {}
        if isinstance(raw, str):
            return {"text": raw}
        ctx = dict(raw)
        # 允许写成 {"user_input": "..."} 这种扁平写法
        if "user_input" in ctx and "text" not in ctx:
            value = ctx.pop("user_input")
            ctx["text"] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        ctx.setdefault("text", "")
        return ctx

    @staticmethod
    def _tool_name(result) -> str:
        """工具名:优先取执行 meta 里的 tool,再退回到结果本身。"""
        meta = getattr(result, "meta", None) or {}
        return str(meta.get("tool") or getattr(result, "tool", "") or "")

    @staticmethod
    def _output_text(results: Dict) -> str:
        """把所有工具结果拼成一段可检索文本。"""
        chunks: List[str] = []
        for res in (results or {}).values():
            data = getattr(res, "data", None)
            if data is None:
                continue
            if isinstance(data, str):
                chunks.append(data)
            else:
                try:
                    chunks.append(json.dumps(data, ensure_ascii=False, default=str))
                except Exception:
                    chunks.append(str(data))
        return "\n".join(chunks)

    @staticmethod
    def _result_keys(results: Dict) -> List[str]:
        keys: List[str] = []
        for res in (results or {}).values():
            data = getattr(res, "data", None)
            if isinstance(data, dict):
                keys.extend(str(k) for k in data.keys())
        return keys

    # ===== 对外 =====

    async def validate(
        self,
        skill: Skill,
        *,
        static_issues: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
    ) -> ValidationReport:
        """校验一个技能版本。

        Args:
            skill: 待校验的技能(通常是打了补丁的候选版本)
            static_issues: 已知的静态校验**错误**;不给则自己跑一遍
            warnings: 静态校验的软性提示(不阻断)
        """
        report = ValidationReport(
            skill_name=skill.name,
            version=skill.version,
            static_issues=list(static_issues or []),
            warnings=list(warnings or []),
        )

        cases = list(skill.regression or [])
        report.total = len(cases)
        if not cases:
            report.static_issues = report.static_issues or ["技能没有回归样例,无法验证新版本是否退化"]
            return report

        # 没有可执行步骤的技能(纯方法论)只做静态校验
        if not skill.has_structured_steps():
            report.passed = report.total if not report.static_issues else 0
            report.cases = [
                CaseResult(
                    name=c.get("name", "case"),
                    passed=not report.static_issues,
                    reason="纯方法论技能,无工具步骤,只做静态校验",
                )
                for c in cases
            ]
            return report

        executor = self._get_executor()
        for case in cases:
            result = await self._run_case(executor, skill, case)
            report.cases.append(result)
            if result.passed:
                report.passed += 1

        return report

    async def _run_case(self, executor, skill: Skill, case: Dict) -> CaseResult:
        name = case.get("name", "case")
        expect = case.get("expect") or {}
        ctx = self._case_input(case)

        try:
            results = await executor.run_skill(skill, ctx)
        except Exception as exc:
            self.logger.warning("ResultValidator", f"样例 {name} 执行异常: {exc}")
            return CaseResult(name=name, passed=False, reason=f"执行异常: {exc}")

        tools_called = [self._tool_name(r) for r in (results or {}).values()]
        failed_tools = [
            self._tool_name(r)
            for r in (results or {}).values()
            if not getattr(r, "success", False)
        ]
        output = self._output_text(results)

        # 1) 期望的工具必须被调用
        want_tool = expect.get("tool")
        if want_tool and want_tool not in tools_called:
            return CaseResult(
                name=name, passed=False,
                reason=f"期望调用工具 {want_tool},实际调用了 {tools_called or '无'}",
                tools_called=tools_called, failed_tools=failed_tools, output=output,
            )

        # 2) 期望全部成功
        if expect.get("success") and failed_tools:
            return CaseResult(
                name=name, passed=False,
                reason=f"工具执行失败: {failed_tools}",
                tools_called=tools_called, failed_tools=failed_tools, output=output,
            )

        # 3) 期望输出包含指定内容
        contains = expect.get("contains") or []
        if isinstance(contains, str):
            contains = [contains]
        missing = [c for c in contains if str(c) not in output]
        if missing:
            return CaseResult(
                name=name, passed=False,
                reason=f"输出缺少预期内容: {missing}",
                tools_called=tools_called, failed_tools=failed_tools, output=output,
            )

        # 4) 期望结果数据含指定键
        want_keys = expect.get("output_keys") or []
        if isinstance(want_keys, str):
            want_keys = [want_keys]
        actual_keys = self._result_keys(results)
        missing_keys = [k for k in want_keys if k not in actual_keys]
        if missing_keys:
            return CaseResult(
                name=name, passed=False,
                reason=f"结果缺少预期字段: {missing_keys}",
                tools_called=tools_called, failed_tools=failed_tools, output=output,
            )

        return CaseResult(
            name=name, passed=True, reason="通过",
            tools_called=tools_called, failed_tools=failed_tools, output=output,
        )
