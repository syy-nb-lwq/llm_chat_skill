"""Skill 加载器 - 从 YAML/MD 文件加载技能"""
import re
from pathlib import Path
from typing import List

import yaml

from infra.logger import get_logger
from skills.models import Skill, SkillStep


class SkillLoader:
    """技能加载器

    默认递归查找 base_path 下所有子目录的 *.yaml 和 *.md。
    可通过 source_map 把特定子目录(如 backend/skills/)纳入。
    """

    def __init__(self, base_path: Path, extra_dirs: List[Path] = None):
        self.base_path = Path(base_path)
        self.extra_dirs = [Path(p) for p in (extra_dirs or []) if p]
        self.logger = get_logger()

    def load_all(self) -> List[Skill]:
        skills = []
        seen_paths = set()

        def _scan(root: Path):
            if not root.exists():
                return
            for f in root.rglob("*.yaml"):
                if f in seen_paths:
                    continue
                seen_paths.add(f)
                try:
                    skills.append(self._load_yaml(f))
                except Exception as e:
                    self.logger.error("Skills", f"YAML 加载失败 {f}: {e}")
            for f in root.rglob("*.md"):
                if f in seen_paths:
                    continue
                seen_paths.add(f)
                try:
                    skills.append(self._load_md(f))
                except Exception as e:
                    self.logger.error("Skills", f"MD 加载失败 {f}: {e}")

        _scan(self.base_path)
        for d in self.extra_dirs:
            _scan(d)
        return skills

    def _load_yaml(self, path: Path) -> Skill:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        skill = self._parse_skill(data)
        # 从目录推断 source
        if "source" not in data:
            parts = path.parts
            if "user" in parts:
                skill.source = "taught"
            elif "backend" in parts:
                skill.source = "imported"
        return skill

    def _load_md(self, path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        # 简单的 front-matter 解析
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                data = yaml.safe_load(parts[1]) or {}
                skill = self._parse_skill(data)
                if "source" not in data:
                    parts_path = path.parts
                    if "user" in parts_path:
                        skill.source = "taught"
                    elif "backend" in parts_path:
                        skill.source = "imported"
                return skill
        # 旧格式: name 在第一行,method 在后续
        lines = text.strip().split("\n")
        name = lines[0].strip("# ").strip()
        method = "\n".join(lines[1:]).strip()
        return Skill(
            name=name,
            version="1.0.0",
            capability="",
            method=method,
            patterns=[],
            tags=[],
            steps=[],
            examples=[],
            source="imported",
        )

    def _parse_skill(self, data: dict) -> Skill:
        steps = []
        for s in data.get("steps", []):
            steps.append(SkillStep(
                id=s.get("id", ""),
                name=s.get("name", ""),
                description=s.get("description", ""),
                tool=s.get("tool"),
                input_schema=s.get("input_schema", {}),
                params=s.get("params", {}) or {},
                output_schema=s.get("output_schema", {}),
                depends_on=s.get("depends_on", []),
                parallel_group=s.get("parallel_group"),
                template=s.get("template"),
                fallback=s.get("fallback"),
                retry=int(s.get("retry", 0) or 0),
                timeout_s=int(s.get("timeout_s", 30) or 30),
            ))

        return Skill(
            name=data.get("name", ""),
            version=data.get("version", "1.0.0"),
            capability=data.get("capability", ""),
            method=data.get("method", ""),
            patterns=data.get("patterns", []),
            tags=data.get("tags", []),
            steps=steps,
            examples=data.get("examples", []),
            source=data.get("source", "builtin"),
            author=data.get("author"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
