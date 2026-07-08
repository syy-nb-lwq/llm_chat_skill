"""Skill 加载器 - 从 YAML/MD 文件加载技能"""
import re
from pathlib import Path
from typing import List

import yaml

from infra.logger import get_logger
from skills.models import Skill, SkillStep


class SkillLoader:
    """技能加载器"""

    def __init__(self, base_path: Path):
        self.base_path = base_path
        self.logger = get_logger()

    def load_all(self) -> List[Skill]:
        skills = []
        # YAML 格式
        for f in self.base_path.glob("*.yaml"):
            try:
                skills.append(self._load_yaml(f))
            except Exception as e:
                self.logger.error("Skills", f"YAML 加载失败 {f}: {e}")
        # MD 格式(旧兼容)
        for f in self.base_path.glob("*.md"):
            try:
                skills.append(self._load_md(f))
            except Exception as e:
                self.logger.error("Skills", f"MD 加载失败 {f}: {e}")
        return skills

    def _load_yaml(self, path: Path) -> Skill:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return self._parse_skill(data)

    def _load_md(self, path: Path) -> Skill:
        text = path.read_text(encoding="utf-8")
        # 简单的 front-matter 解析
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                data = yaml.safe_load(parts[1])
                return self._parse_skill(data)
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
                output_schema=s.get("output_schema", {}),
                depends_on=s.get("depends_on", []),
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
