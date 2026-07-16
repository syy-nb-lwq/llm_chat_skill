"""Skill storage facade backed by loader + registry."""
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from infra.logger import get_logger
from skills.loader import SkillLoader
from skills.models import Skill
from skills.registry import SkillRegistry


class SkillStore:
    """Compatibility facade for skill CRUD and matching."""

    def __init__(self, path: str = None):
        if path is None:
            root = Path(__file__).parent.parent
            base = root / "skills"
            extras = [root / "backend" / "skills"]
        else:
            base = Path(path)
            extras = []

        self.path = base
        self.base_path = base
        self.path.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger()
        self._loader = SkillLoader(base, extra_dirs=extras)
        self._registry = SkillRegistry()
        self.reload()

    def reload(self):
        skills = self._loader.load_all()
        self._registry.reload(skills)
        self._skills = self._registry._by_name

    def add(self, skill: Skill) -> str:
        self._registry.add(skill)
        self._skills = self._registry._by_name
        return skill.id

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._registry._by_id.get(skill_id)

    def get_by_name(self, name: str) -> Optional[Skill]:
        return self._registry.get(name)

    def list_all(self) -> List[Skill]:
        return self._registry.all()

    def delete(self, skill_id: str) -> bool:
        skill = self._registry._by_id.get(skill_id)
        if not skill:
            return False
        removed = self.delete_by_name(skill.name)
        return bool(removed)

    def remove(self, skill_name: str) -> bool:
        return bool(self.delete_by_name(skill_name))

    def delete_by_name(self, skill_name: str) -> List[str]:
        removed = self._delete_files(skill_name=skill_name)
        if removed:
            self.reload()
        return removed

    def delete_version(self, skill_name: str, version: str) -> List[str]:
        removed = self._delete_files(skill_name=skill_name, version=version)
        if removed:
            self.reload()
        return removed

    def update_skill(
        self,
        skill_name: str,
        updates: Dict[str, object],
        *,
        version: Optional[str] = None,
    ) -> List[str]:
        updated_files: List[str] = []
        for path in self._candidate_files(skill_name, version):
            if path.suffix.lower() != ".yaml":
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                data.update(updates)
                path.write_text(
                    yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                updated_files.append(str(path))
            except Exception as exc:
                self.logger.error("Skills", f"update failed for {path}: {exc}")
        if updated_files:
            self.reload()
        return updated_files

    def match(self, user_input: str, top_k: int = 3) -> List[Skill]:
        return self._registry.match(user_input, top_k)

    def validate(self, tool_names: List[str]) -> List[str]:
        return self._registry.validate(tool_names)

    def _candidate_files(self, skill_name: str, version: Optional[str] = None) -> List[Path]:
        patterns = []
        if version:
            patterns.extend(
                [
                    f"{skill_name}@{version}.yaml",
                    f"{skill_name}@{version}.md",
                    f"{skill_name}*{version}*.yaml",
                    f"{skill_name}*{version}*.md",
                ]
            )
        else:
            patterns.extend([f"{skill_name}*.yaml", f"{skill_name}*.md"])

        candidates: List[Path] = []
        roots = [self.base_path, self.base_path / "builtin", self.base_path / "user"]
        root_parent = self.base_path.parent
        roots.append(root_parent / "backend" / "skills")

        seen = set()
        for root in roots:
            if not root.exists():
                continue
            for pattern in patterns:
                for path in root.rglob(pattern):
                    key = str(path.resolve())
                    if key not in seen:
                        seen.add(key)
                        candidates.append(path)
        return candidates

    def _delete_files(self, skill_name: str, version: Optional[str] = None) -> List[str]:
        removed: List[str] = []
        for path in self._candidate_files(skill_name, version):
            try:
                path.unlink()
                removed.append(str(path))
            except Exception as exc:
                self.logger.error("Skills", f"delete failed for {path}: {exc}")
        return removed


_store: Optional[SkillStore] = None


def get_skill_store(path: str = None) -> SkillStore:
    global _store
    if _store is None:
        _store = SkillStore(path)
    return _store


def reset_skill_store():
    global _store
    _store = None
