"""技能注册表 - 加权打分匹配 / 失效检测 / 版本管理"""
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from infra.logger import get_logger
from skills.models import Skill, SkillStep


class SkillConflictError(Exception):
    pass


class SkillRegistry:
    """技能注册表"""

    def __init__(self, skills: Optional[List[Skill]] = None):
        self._by_name: Dict[str, Skill] = {}
        self._by_id: Dict[str, Skill] = {}
        self._pattern_index: Dict[str, List[str]] = defaultdict(list)
        self.logger = get_logger()
        if skills:
            for s in skills:
                self._index(s)

    def _index(self, skill: Skill):
        self._by_name[skill.name] = skill
        self._by_id[skill.id] = skill
        for p in skill.patterns:
            self._pattern_index[p.lower()].append(skill.name)

    def add(self, skill: Skill, persist: bool = False, base_path: Optional[Path] = None) -> None:
        existing = self._by_name.get(skill.name)
        if existing and existing.version == skill.version:
            raise SkillConflictError(
                f"技能 {skill.name} v{skill.version} 已存在,请升级 version 或换 name"
            )
        self._index(skill)
        self.logger.info("Skills", f"新增技能: {skill.name} v{skill.version}")
        if persist and base_path:
            self._save_yaml(skill, base_path)

    def get(self, name: str) -> Optional[Skill]:
        return self._by_name.get(name)

    def all(self) -> List[Skill]:
        return list(self._by_name.values())

    def reload(self, skills: List[Skill]):
        self._by_name.clear()
        self._by_id.clear()
        self._pattern_index.clear()
        for s in skills:
            self._index(s)

    def match(self, user_input: str, top_k: int = 3) -> List[Skill]:
        text = user_input.lower().strip()
        if not text:
            return []
        scores: List[Tuple[float, Skill]] = []
        for skill in self._by_name.values():
            score = self._score(skill, text)
            if score > 0:
                scores.append((score, skill))
        scores.sort(key=lambda x: -x[0])
        return [s for _, s in scores[:top_k]]

    def _score(self, skill: Skill, text: str) -> float:
        score = 0.0
        for p in skill.patterns:
            pl = p.lower()
            if pl in text:
                score += 1.0 if text == pl else 0.5
        if skill.capability:
            for kw in skill.capability.split()[:5]:
                if kw.lower() in text:
                    score += 0.2
        if skill.method:
            for kw in skill.method.split()[:5]:
                if kw.lower() in text:
                    score += 0.1
        return score

    def validate(self, tool_names: List[str]) -> List[str]:
        issues: List[str] = []
        for s in self._by_name.values():
            issues.extend(self._validate_one(s, tool_names))
        return issues

    def _validate_one(self, skill: Skill, tool_names: List[str]) -> List[str]:
        out: List[str] = []
        step_ids = {st.id for st in skill.steps}
        for st in skill.steps:
            if st.tool and st.tool not in tool_names:
                out.append(f"技能 {skill.name}: step {st.id} 引用未知工具 {st.tool}")
            for dep in st.depends_on:
                if dep not in step_ids:
                    out.append(f"技能 {skill.name}: step {st.id} 依赖不存在的 step {dep}")
            if st.fallback and st.fallback not in step_ids:
                out.append(f"技能 {skill.name}: step {st.id} fallback 不存在 {st.fallback}")
        if _has_cycle(skill.steps):
            out.append(f"技能 {skill.name}: 存在循环依赖")
        return out

    def _save_yaml(self, skill: Skill, base_path: Path):
        try:
            import yaml
            target_dir = base_path / ("user" if skill.source == "taught" else "builtin")
            target_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{skill.name}@{skill.version}.yaml"
            path = target_dir / fname
            path.write_text(
                yaml.safe_dump(skill.to_dict(), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except Exception as e:
            self.logger.error("Skills", f"保存技能失败 {skill.name}: {e}")


def _has_cycle(steps: List[SkillStep]) -> bool:
    graph = {s.id: list(s.depends_on) for s in steps}
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
