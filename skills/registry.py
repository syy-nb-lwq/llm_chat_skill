"""技能注册表 - 加权打分匹配 / 失效检测 / 版本管理(M1-03/M1-04)

设计:
- 内存中可同时存在同名多版本,key=``name@version`` → Skill
- ``active_versions`` 字典: ``{name: version_str}`` 显式记录当前激活版本
- 加载时:若 YAML 内含 ``active: true``,该 version 成为 active;
         若同 name 多个 active 版本同时存在,记录冲突;
         若无 active,默认激活 version 字符串最大的一个。
- match() 只返回 active 版本。
"""
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from infra.logger import get_logger
from skills.models import Skill, SkillStep


class SkillConflictError(Exception):
    pass


class SkillRegistry:
    """技能注册表(支持多版本 + active 指针)"""

    def __init__(self, skills: Optional[List[Skill]] = None):
        # name -> { version: Skill }  (允许同名多版本)
        self._by_version: Dict[str, Dict[str, Skill]] = defaultdict(dict)
        # name -> active version
        self._active_versions: Dict[str, str] = {}
        # 兼容旧 API
        self._by_name: Dict[str, Skill] = {}
        self._by_id: Dict[str, Skill] = {}
        self._pattern_index: Dict[str, List[str]] = defaultdict(list)
        self.logger = get_logger()
        if skills:
            for s in skills:
                self._index(s)

    # ===== 索引 =====

    def _index(self, skill: Skill):
        versions = self._by_version[skill.name]
        versions[skill.version] = skill
        # 兼容旧 by_name
        self._by_name[skill.name] = skill  # 最后写入的胜出(通常顺序加载)
        self._by_id[skill.id] = skill
        for p in skill.patterns:
            self._pattern_index[p.lower()].append(skill.name)
        # 加载时遇到 active=true 标记
        if getattr(skill, "active", False):
            self._active_versions[skill.name] = skill.version

    def add(self, skill: Skill, persist: bool = False, base_path: Optional[Path] = None) -> None:
        existing = self._by_version.get(skill.name, {}).get(skill.version)
        if existing:
            raise SkillConflictError(
                f"技能 {skill.name} v{skill.version} 已存在,请升级 version 或换 name"
            )
        self._index(skill)
        # 新加入的版本若声明 active,则切换指针
        if getattr(skill, "active", False):
            self._active_versions[skill.name] = skill.version
        self.logger.info("Skills", f"新增技能: {skill.name} v{skill.version}")
        if persist and base_path:
            self._save_yaml(skill, base_path)

    def get(self, name: str) -> Optional[Skill]:
        """获取 name 的 active 版本(兼容旧 API)。"""
        v = self._active_versions.get(name)
        if v is None:
            # 没有 active 指针时,返回该 name 下最大 version
            versions = list(self._by_version.get(name, {}).keys())
            if not versions:
                return None
            v = max(versions)
        return self._by_version.get(name, {}).get(v)

    def get_version(self, name: str, version: str) -> Optional[Skill]:
        return self._by_version.get(name, {}).get(version)

    def list_versions(self, name: str) -> List[str]:
        return list(self._by_version.get(name, {}).keys())

    def set_active(self, name: str, version: str) -> bool:
        if version not in self._by_version.get(name, {}):
            return False
        self._active_versions[name] = version
        return True

    def active_versions(self) -> Dict[str, str]:
        return dict(self._active_versions)

    def all(self) -> List[Skill]:
        """返回所有 active 版本。"""
        out: List[Skill] = []
        for name, versions in self._by_version.items():
            v = self._active_versions.get(name)
            if v is None:
                v = max(versions.keys())
            s = versions.get(v)
            if s is not None:
                out.append(s)
        return out

    def all_versions(self) -> List[Skill]:
        """返回所有版本(给管理 UI / 测试用)。"""
        out: List[Skill] = []
        for versions in self._by_version.values():
            out.extend(versions.values())
        return out

    def reload(self, skills: List[Skill]):
        self._by_version.clear()
        self._by_name.clear()
        self._by_id.clear()
        self._pattern_index.clear()
        self._active_versions.clear()
        for s in skills:
            self._index(s)
        # 自动选择每个 name 的最新 version 作 active(若 YAML 未声明)
        for name, versions in self._by_version.items():
            if name not in self._active_versions:
                self._active_versions[name] = max(versions.keys())

    def match(self, user_input: str, top_k: int = 3) -> List[Skill]:
        text = user_input.lower().strip()
        if not text:
            return []
        scores: List[Tuple[float, Skill]] = []
        # 只对 active 版本做匹配
        for skill in self.all():
            score = self._score(skill, text)
            if score > 0:
                scores.append((score, skill))
        scores.sort(key=lambda x: -x[0])
        return [s for _, s in scores[:top_k]]

    @staticmethod
    def _bigrams(text: str):
        """中文 2-gram 集合（去掉空白）。用于长短语的部分匹配。"""
        clean = re.sub(r"\s+", "", text)
        if len(clean) < 2:
            return {clean} if clean else set()
        return {clean[i:i + 2] for i in range(len(clean) - 1)}

    def _pattern_hit(self, pattern: str, text: str) -> float:
        """一个触发词的命中强度：完全包含 > 高覆盖的部分匹配 > 0。

        中文没有空格，patterns 常是"按格式输出报销文件"这类长短语，
        用户几乎不会一字不差地复述 —— 单纯 `if pattern in text` 会**永远匹配不上**
        （实机踩过：技能名都念对了，match 仍返回空，导致按技能裁剪工具永远走 fallback）。

        所以用 2-gram 覆盖率做部分匹配：重叠过半算命中，给 0.35 分（低于子串命中的 0.5，
        保证精确/子串匹配仍然优先）。
        """
        pl = pattern.lower().strip()
        if not pl:
            return 0.0
        if pl in text:
            return 1.0 if text == pl else 0.5
        # 只对够长的中文短语做 2-gram 部分匹配，短词仍走严格包含（避免泛化过度）
        if len(re.sub(r"\s+", "", pl)) < 4:
            return 0.0
        pb = self._bigrams(pl)
        tb = self._bigrams(text)
        if not pb or not tb:
            return 0.0
        overlap = len(pb & tb) / len(pb)
        return 0.35 if overlap >= 0.5 else 0.0

    def _score(self, skill: Skill, text: str) -> float:
        score = 0.0
        for p in skill.patterns:
            score += self._pattern_hit(str(p), text)
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
        for s in self.all():
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
