"""SkillManagerAgent - 技能管理 Agent(M1-09)。

定位:
- 把 ``list / show / rollback / activate`` 等技能管理意图
  从原来的"旁路"搬入主链(``Agent.handle()``)。
- 不依赖 LLM,只对 ``skills/registry`` 做确定性操作。
- 返回结构化 ``ManagerResult``,由 ``Agent.handle()`` 渲染成自然语言。

设计依据:
- ``docs/10-目标架构评审与演进方案.md §4.7`` 与 ``11-开发任务清单.md M1-09``。
- 与 ``agents/skill_manager.py``(基于 LLM 的版本)并存,后者仍可由
  显式 API 调用;此处只承担对话路由职责。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from skills.manager import get_skill_store
from skills.registry import SkillRegistry


@dataclass
class ManagerResult:
    """技能管理操作的结果(M1-09)。"""
    ok: bool
    action: str                       # list / show / versions / rollback / activate / unknown
    message: str                      # 给用户展示的自然语言
    details: List[dict] = None        # 结构化数据(给前端 / 测试用)
    skill_name: str = ""
    version: str = ""

    def __post_init__(self):
        if self.details is None:
            self.details = []


class SkillManagerAgent:
    """主链上的技能管理 Agent(M1-09)。

    接收自然语言指令(如"列出所有技能"、"回滚日报到 1.0.0"),
    解析为 ``action + skill_name + version``,调用 ``SkillStore`` 执行。
    """

    # 顺序很重要:先匹配更具体的(rollback/activate),再匹配泛化的(list/show)。
    # 支持两种词序:
    #   1. 回滚 demo 到 1.0.0  (中文 skill 在 version 前,中间"到")
    #   2. rollback demo to 1.0.0 / activate demo 1.0.1  (skill 后跟 version)
    # 每条都是 (action, pattern)。
    _ACTION_PATTERNS = [
        # 中文词序:回滚 demo 到 1.0.0
        ("rollback", r"(?:回滚|rollback|revert)\s+(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)\s+(?:到|to)\s+(?P<ver>\d+\.\d+\.\d+)"),
        # 英文/数字词序:rollback demo 1.0.0 / activate demo 1.0.1
        ("rollback", r"(?:回滚|rollback|revert)\s+(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)\s+(?P<ver>\d+\.\d+\.\d+)"),
        ("activate", r"(?:激活|启用|activate|switch\s*to)\s+(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)\s+(?P<ver>\d+\.\d+\.\d+)"),
        # 仅指定 skill,无 version(由 _set_active 选默认/前一个版本)
        ("rollback", r"(?:回滚|rollback|revert)\s+(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)\s*$"),
        ("activate", r"(?:激活|启用|activate|switch\s*to)\s+(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)\s*$"),
        # 中文: <skill> 的版本(先于泛化的 versions 模式)
        ("versions", r"(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)\s*(?:的)?\s*(?:版本|versions?)"),
        # 泛化的 versions(只在前面的模式没匹配时才用)
        ("versions", r"(?:^|\s)(?:版本|versions?|list\s*versions?)\s*(?:of|for)?\s*(?P<skill>[A-Za-z][A-Za-z0-9_\-]*)?\s*$"),
        ("show", r"(?:查看|显示|show|describe|detail)\s*(?:技能\s*)?(?P<skill>[A-Za-z][A-Za-z0-9_\-]+)"),
        ("list", r"(?:列出|所有|有哪些|list|all|show\s*me)\s*(?:技能|skills?)?$"),
    ]

    def __init__(self):
        self.store = get_skill_store()

    # ===== 对外 API =====

    async def handle(self, user_input: str) -> ManagerResult:
        """解析并执行技能管理指令。"""
        text = (user_input or "").strip()
        if not text:
            return ManagerResult(False, "unknown", "没有提供操作指令")

        action, skill, ver = self._parse(text)
        if action == "list":
            return self._list_skills()
        if action == "show":
            return self._show_skill(skill)
        if action == "versions":
            return self._list_versions(skill)
        if action == "rollback" or action == "activate":
            return self._set_active(skill, ver, action=action)
        return ManagerResult(False, "unknown", f"无法识别的技能管理指令: {text}")

    # ===== 解析 =====

    def _parse(self, text: str) -> Tuple[str, str, str]:
        """返回 (action, skill_name, version)。"""
        low = text.lower().strip()
        for action, pat in self._ACTION_PATTERNS:
            m = re.search(pat, low, re.IGNORECASE)
            if not m:
                continue
            skill = (m.groupdict().get("skill") or "").strip().rstrip("，。,.")
            ver = (m.groupdict().get("ver") or "").strip()
            return action, skill, ver
        # 默认:没匹配到任何模式,返回 unknown
        return "unknown", "", ""

    # ===== 操作 =====

    def _list_skills(self) -> ManagerResult:
        skills = self.store.list_all()
        if not skills:
            return ManagerResult(
                True, "list",
                "当前还没有任何技能。",
                details=[],
            )
        lines = [f"现有 {len(skills)} 个技能(active 版本):"]
        for s in skills:
            cap = (s.capability or "").strip().splitlines()[0][:60] if s.capability else ""
            lines.append(f"- {s.name} @ {s.version} — {cap}")
        details = [
            {"name": s.name, "version": s.version, "capability": s.capability}
            for s in skills
        ]
        return ManagerResult(True, "list", "\n".join(lines), details=details)

    def _show_skill(self, name: str) -> ManagerResult:
        if not name:
            return ManagerResult(False, "show", "请告诉我需要查看哪个技能。")
        skill = self.store.get_by_name(name)
        if not skill:
            return ManagerResult(False, "show", f"找不到技能: {name}")
        body = (
            f"技能: {skill.name} @ {skill.version}\n"
            f"能力: {skill.capability or '(无)'}\n"
            f"方法: {skill.method or '(无)'}\n"
            f"匹配关键词: {', '.join(skill.patterns or []) or '(无)'}\n"
            f"标签: {', '.join(skill.tags or []) or '(无)'}\n"
            f"来源: {skill.source or '(未知)'}"
        )
        return ManagerResult(
            True, "show", body,
            details=[skill.to_dict() if hasattr(skill, "to_dict") else {}],
            skill_name=skill.name, version=skill.version,
        )

    def _list_versions(self, name: str) -> ManagerResult:
        if not name:
            return ManagerResult(False, "versions", "请告诉我需要查看哪个技能的版本。")
        versions = self.store._registry.list_versions(name)
        if not versions:
            return ManagerResult(False, "versions", f"找不到技能: {name}")
        active = self.store._registry._active_versions.get(name)
        lines = [f"技能 {name} 有 {len(versions)} 个版本:"]
        for v in sorted(versions):
            tag = " (active)" if v == active else ""
            lines.append(f"- {v}{tag}")
        return ManagerResult(
            True, "versions", "\n".join(lines),
            details=[{"version": v, "active": v == active} for v in sorted(versions)],
            skill_name=name,
        )

    def _set_active(
        self, name: str, version: str, *, action: str,
    ) -> ManagerResult:
        if not name:
            return ManagerResult(
                False, action,
                f"请告诉我需要{'回滚' if action == 'rollback' else '激活'}哪个技能。",
            )
        registry: SkillRegistry = self.store._registry
        versions = registry.list_versions(name)
        if not versions:
            return ManagerResult(False, action, f"找不到技能: {name}")

        # rollback: 若 version 为空,默认回到上一版本(列表中次新的)
        if not version:
            sorted_v = sorted(versions)
            if len(sorted_v) < 2:
                return ManagerResult(
                    False, action,
                    f"{name} 只有一个版本,无法回滚。",
                )
            version = sorted_v[-2]

        if version not in versions:
            return ManagerResult(
                False, action,
                f"{name} 不存在版本 {version};可选: {', '.join(sorted(versions))}",
            )

        if registry.set_active(name, version):
            verb = "回滚到" if action == "rollback" else "激活"
            return ManagerResult(
                True, action,
                f"已将技能 {name} {verb} {version}。",
                details=[{"skill": name, "version": version, "action": action}],
                skill_name=name, version=version,
            )
        return ManagerResult(False, action, f"{name} 切换到 {version} 失败")


_singleton: Optional[SkillManagerAgent] = None


def get_skill_manager_agent() -> SkillManagerAgent:
    """获取主链上的 SkillManagerAgent 单例。"""
    global _singleton
    if _singleton is None:
        _singleton = SkillManagerAgent()
    return _singleton