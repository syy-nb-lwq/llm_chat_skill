"""技能加载器 - 支持 YAML(新)和 Markdown(旧,兼容)"""
import re
from pathlib import Path
from typing import List, Optional

import yaml

from infra.logger import get_logger, LogType
from skills.models import Skill, SkillStep


class SkillLoadError(Exception):
    pass


class SkillLoader:
    """从目录加载技能。目录结构:
        skills/builtin/*.yaml
        skills/user/*.yaml
    兼容旧格式:skills/*.md
    """

    def __init__(self, base_path: Path):
        self.base_path = Path(base_path)
        self.logger = get_logger()

    def load_all(self) -> List[Skill]:
        skills: List[Skill] = []
        # 新格式
        for sub in ("builtin", "user"):
            d = self.base_path / sub
            if not d.exists():
                continue
            for f in d.glob("*.yaml"):
                try:
                    skills.append(self._load_yaml(f))
                except Exception as e:
                    self.logger.error(LogType.FLOW_STEP, "Skills",
                                      f"YAML 加载失败 {f}: {e}")
        # 旧格式兼容
        for f in self.base_path.glob("*.md"):
            try:
                s = self._load_md(f)
                if s:
                    skills.append(s)
            except Exception as e:
                self.logger.error(LogType.FLOW_STEP, "Skills",
                                  f"MD 加载失败 {f}: {e}")
        return skills

    # ----- YAML -----
    def _load_yaml(self, f: Path) -> Skill:
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SkillLoadError(f"{f}: 顶层必须是 dict")

        steps: List[SkillStep] = []
        for s in raw.get("steps", []) or []:
            steps.append(SkillStep(
                id=s["id"],
                name=s.get("name", s["id"]),
                description=s.get("description", ""),
                tool=s.get("tool"),
                input_schema=s.get("input_schema", {}),
                output_schema=s.get("output_schema", {}),
                depends_on=s.get("depends_on", []) or [],
                parallel_group=s.get("parallel_group"),
                template=s.get("template"),
                fallback=s.get("fallback"),
                retry=int(s.get("retry", 0)),
                timeout_s=int(s.get("timeout_s", 30)),
            ))

        return Skill(
            name=raw["name"],
            version=raw.get("version", "1.0.0"),
            capability=raw.get("capability", ""),
            method=raw.get("method", ""),
            patterns=raw.get("patterns", []) or [],
            tags=raw.get("tags", []) or [],
            steps=steps,
            examples=raw.get("examples", []) or [],
            source=raw.get("source", "builtin"),
            author=raw.get("author"),
        )

    # ----- Markdown(旧) -----
    def _load_md(self, f: Path) -> Optional[Skill]:
        content = f.read_text(encoding="utf-8")
        skill = Skill(name=f.stem, source="builtin")

        if m := re.search(r"# 技能：(.+)", content):
            skill.name = m.group(1).strip()
        if m := re.search(r"## 能力\n([\s\S]+?)(?=##)", content):
            skill.capability = m.group(1).strip()
        if m := re.search(r"## 匹配模式\n([\s\S]+?)(?=##)", content):
            skill.patterns = re.findall(r"- (.+)", m.group(1))
        if m := re.search(r"## 方法论\n([\s\S]+?)(?=##)", content):
            skill.method = m.group(1).strip()
        if m := re.search(r"## 步骤\n([\s\S]+?)(?=##)", content):
            steps_text: List[str] = []
            for line in m.group(1).strip().split("\n"):
                line = line.strip()
                if line and (line[0].isdigit() or line.startswith("-")):
                    line = re.sub(r"^[\d]+\.\s*", "", line)
                    line = re.sub(r"^-\s*", "", line)
                    if line:
                        steps_text.append(line)
            skill.legacy_steps_text = steps_text
            # 也存到 structured steps(无 tool,DAG 不会调度它们)
            for i, t in enumerate(steps_text):
                skill.steps.append(SkillStep(id=f"s{i}", name=t, description=t))

        if not skill.name:
            return None
        return skill