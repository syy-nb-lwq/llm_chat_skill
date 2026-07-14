"""技能管理器 - 兼容旧 API,内部用新版 models / loader / registry"""
from pathlib import Path
from typing import List, Optional

from infra.logger import get_logger
from skills.loader import SkillLoader
from skills.models import Skill, SkillStep
from skills.registry import SkillRegistry


class SkillStore:
    """技能存储(兼容旧 API)

    内部:
    - 使用 SkillLoader 加载 builtin/user/*.yaml 和 *.md
    - 使用 SkillRegistry 做 CRUD + 匹配
    """

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
        self._skills = self._registry._by_name  # 兼容旧 dict 访问

    # ----- 旧 API 兼容 -----
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
        # 从内存移除
        self._registry._by_name.pop(skill.name, None)
        self._registry._by_id.pop(skill_id, None)
        # 文件删除
        for sub in ("builtin", "user"):
            for f in (self.base_path / sub).glob(f"{skill.name}*.yaml"):
                f.unlink(missing_ok=True)
        return True

    def remove(self, skill_name: str) -> bool:
        """通过名称移除技能"""
        skill = self._registry.get(skill_name)
        if not skill:
            return False
        return self.delete(skill.id)

    # ----- 新 API -----
    def match(self, user_input: str, top_k: int = 3) -> List[Skill]:
        return self._registry.match(user_input, top_k)

    def validate(self, tool_names: List[str]) -> List[str]:
        return self._registry.validate(tool_names)


# 全局实例
_store: Optional[SkillStore] = None


def get_skill_store(path: str = None) -> SkillStore:
    """获取技能库管理器实例"""
    global _store
    if _store is None:
        _store = SkillStore(path)
    return _store


def reset_skill_store():
    global _store
    _store = None