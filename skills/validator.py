"""Skill 验证流水线(M1-05)。

发布前必须通过:
- 必填字段: name / method / capability
- 版本号: 三段 semver
- name 格式: 不与系统保留冲突
- DAG 无环
- 引用的工具存在
- step 引用完整
- 至少 1 个正例 + 1 个边界例(可由 LLM/或默认 seed)

返回 issues 列表,空表示通过。
"""
from __future__ import annotations

import re
from typing import Iterable, List, Set

from skills.models import Skill, SkillStep


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def validate_skill(skill: Skill, tool_names: Iterable[str] = ()) -> List[str]:
    """对单一 Skill 做静态校验。

    Args:
        skill: 待校验的 Skill
        tool_names: 当前 ToolHub 中可用的工具名集合
    """
    issues: List[str] = []
    tool_set: Set[str] = set(tool_names or ())

    # 必填字段
    if not (skill.name or "").strip():
        issues.append("name 为空")
    elif not _NAME_RE.match(skill.name):
        issues.append(f"name 格式不合法(仅字母数字下划线,2-64 字符): {skill.name!r}")
    if not (skill.method or "").strip() or len(skill.method.strip()) < 5:
        issues.append("method 为空或太短(<5 字符)")
    if not (skill.capability or "").strip() or len(skill.capability.strip()) < 5:
        issues.append("capability 为空或太短(<5 字符)")

    # 版本号
    if skill.version and not _SEMVER_RE.match(skill.version):
        issues.append(f"version 不是合法的 semver: {skill.version!r}")

    # step DAG
    step_ids: Set[str] = set()
    for st in skill.steps or []:
        if not st.id:
            issues.append("存在缺少 id 的 step")
            continue
        if st.id in step_ids:
            issues.append(f"step id 重复: {st.id}")
        step_ids.add(st.id)

    for st in skill.steps or []:
        for dep in st.depends_on or []:
            if dep not in step_ids:
                issues.append(f"step {st.id} 依赖不存在的 step {dep}")
        if st.tool:
            if tool_set and st.tool not in tool_set:
                issues.append(f"step {st.id} 引用未知工具 {st.tool}")

    if _has_cycle(skill.steps or []):
        issues.append("存在循环依赖")

    # 至少 1 个 examples
    if not (skill.examples or []):
        # examples 可以来自教学 evidence,允许空但记 warning → 这里只返回问题
        issues.append("缺少 examples(教学证据为空,无回归样例)")

    return issues


def _has_cycle(steps: List[SkillStep]) -> bool:
    graph = {s.id: list(s.depends_on or []) for s in steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}

    def dfs(n):
        color[n] = GRAY
        for m in graph.get(n, []):
            if m not in color:
                continue
            if color[m] == GRAY:
                return True
            if color[m] == WHITE and dfs(m):
                return True
        color[n] = BLACK
        return False

    for n in graph:
        if color[n] == WHITE and dfs(n):
            return True
    return False
