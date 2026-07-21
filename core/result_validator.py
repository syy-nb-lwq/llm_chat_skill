"""M3-03 Result Validator。

职责:在执行完成后,判断最终输出是否真正满足用户目标,
而不只看工具是否成功返回。

解决的问题:
- 没有工具的任务(纯方法论技能)不再无条件得 100%
- 检查技能自带评测样例(method/examples)是否被输出覆盖
- 检查结构化约束(capability 中声明的栏目是否出现)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from skills.models import Skill


@dataclass
class CheckResult:
    """单项检查结果"""
    name: str
    passed: bool
    reason: str = ""


@dataclass
class ResultValidation:
    """整体结果验证"""
    passed: bool
    score: float  # 0.0 ~ 1.0
    issues: List[str] = field(default_factory=list)
    checks: List[CheckResult] = field(default_factory=list)


class ResultValidator:
    """对最终输出做语义/结构校验。"""

    def validate(
        self,
        skill: Optional[Skill],
        final_output: str,
        user_input: str = "",
    ) -> ResultValidation:
        checks: List[CheckResult] = []
        issues: List[str] = []

        output = (final_output or "").strip()
        if not output:
            checks.append(CheckResult("non_empty", False, "输出为空"))
            issues.append("输出为空,未满足用户目标")
            return ResultValidation(passed=False, score=0.0, issues=issues, checks=checks)

        checks.append(CheckResult("non_empty", True, "输出非空"))

        # 输出长度过短视为可疑
        if len(output) < 10:
            checks.append(CheckResult("min_length", False, f"输出过短({len(output)} 字符)"))
            issues.append("输出过短,可能未完整回答用户目标")
        else:
            checks.append(CheckResult("min_length", True, f"输出长度 {len(output)} 字符"))

        if skill is None:
            # 无技能匹配时,只要非空且有一定长度就算通过
            score = 0.6 if not issues else 0.3
            return ResultValidation(passed=not issues, score=score, issues=issues, checks=checks)

        # 1. capability 约束:提取关键词,检查输出是否覆盖
        capability = (skill.capability or "").strip()
        if capability:
            keywords = _extract_capability_keywords(capability)
            missing = [kw for kw in keywords if kw not in output]
            if missing and len(keywords) > len(missing):
                checks.append(CheckResult(
                    "capability_coverage",
                    False,
                    f"capability 关键词未全覆盖: {missing[:3]}",
                ))
                issues.append(f"输出未覆盖能力声明中的: {missing[:3]}")
            elif missing:
                checks.append(CheckResult(
                    "capability_coverage",
                    False,
                    f"capability 关键词全部缺失: {missing[:3]}",
                ))
                issues.append(f"输出未体现能力声明: {missing[:3]}")
            else:
                checks.append(CheckResult("capability_coverage", True, "capability 关键词已覆盖"))

        # 2. method 结构约束:检查 method 中的步骤标记是否在输出中出现
        method = (skill.method or "").strip()
        if method:
            steps = _extract_step_keywords(method)
            if steps:
                present = [s for s in steps if s in output]
                coverage = len(present) / len(steps) if steps else 0.0
                if coverage < 0.34:
                    checks.append(CheckResult(
                        "method_steps",
                        False,
                        f"method 步骤覆盖率过低({coverage:.0%}): {present}/{steps}",
                    ))
                    issues.append("输出未遵循 method 定义的关键步骤")
                else:
                    checks.append(CheckResult(
                        "method_steps",
                        True,
                        f"method 步骤覆盖率 {coverage:.0%}",
                    ))

        # 3. examples 风格:至少与一个 example 有关键词重叠
        examples = skill.examples or []
        if examples:
            matched = any(
                any(kw in output for kw in _extract_keywords(ex) if len(kw) >= 2)
                for ex in examples
            )
            if matched:
                checks.append(CheckResult("examples_style", True, "输出与样例风格匹配"))
            else:
                checks.append(CheckResult("examples_style", False, "输出与所有样例无关键词重叠"))
                issues.append("输出与技能样例风格不一致")

        score = sum(1 for c in checks if c.passed) / len(checks) if checks else 1.0
        passed = not issues
        return ResultValidation(passed=passed, score=score, issues=issues, checks=checks)


def _extract_capability_keywords(text: str) -> List[str]:
    """提取 capability 中显式声明的结构化约束。"""
    import re

    # 冒号前通常是能力概述，后面才是必须覆盖的栏目。
    if ":" in text or "：" in text:
        text = re.split(r"[:：]", text, maxsplit=1)[1]
    parts = [item.strip() for item in re.split(r"[、,，;；/\n]", text) if item.strip()]
    if len(parts) <= 1:
        return []
    return [item for item in parts if len(item) >= 2]



def _extract_keywords(text: str) -> List[str]:
    """从中文/英文文本中提取关键词(简单分词)。"""
    import re
    # 保留中文连续字符和英文单词
    tokens = re.findall(r"[\u4e00-\u9fa5]+|[A-Za-z]{2,}", text)
    # 过滤常见停用词
    stop = {"的", "了", "和", "与", "或", "及", "在", "为", "是", "按", "生成", "一个"}
    return [t for t in tokens if t not in stop]


def _extract_step_keywords(method: str) -> List[str]:
    """从 method 文本中提取步骤关键词。

    支持形如 "1) 完成 2) 问题 3) 计划" 或 "- 完成\\n- 问题" 的结构。
    """
    import re
    steps: List[str] = []
    # 匹配 "数字) 文字" 或 "数字. 文字"
    for m in re.finditer(r"(?:\d+[)\.．、]\s*)([\u4e00-\u9fa5A-Za-z]{2,})", method):
        steps.append(m.group(1))
    if steps:
        return steps
    # 回退:按行拆分,取每行前几个关键词
    for line in method.splitlines():
        line = line.strip().lstrip("-*•")
        if not line:
            continue
        kws = _extract_keywords(line)
        if kws:
            steps.append(kws[0])
    return steps
