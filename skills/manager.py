"""Skill storage facade backed by loader + registry."""
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from infra.logger import get_logger
from skills.loader import SkillLoader
from skills.models import Skill
from skills.registry import SkillConflictError, SkillRegistry


def _bump_version(version: str) -> str:
    """把版本号的补丁位 +1(1.0.0 → 1.0.1)。解析失败时退回 1.0.1。"""
    try:
        parts = str(version).split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return f"{major}.{minor}.{patch + 1}"
    except (ValueError, IndexError, TypeError):
        return "1.0.1"


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

    def add(self, skill: Skill, *, set_active: bool = False) -> str:
        """新增一个技能版本。

        Args:
            set_active: 若 True,把这个版本切到 active 指针(M1-04)。
        """
        self._registry.add(skill)
        if set_active:
            self._registry.set_active(skill.name, skill.version)
        return skill.id

    def persist(self, skill: Skill) -> Optional[Path]:
        """把技能写到磁盘并切 active —— **落盘的唯一入口**。

        为什么单独抽出来：`add()` 只动内存索引，进程一重启技能就没了。
        `approve_draft()` 原先直接调 `add()`，于是出现「批准了、界面也有了、
        重启之后又空了」——提炼链路明明跑通，方法论却一条都没沉淀下来。

        - `taught` / `harvested` 写 ``<base>/user/``（人教的和自己攒的）
        - 其余写 ``<base>/builtin/``

        Returns:
            写成功的路径；失败返回 None（已记日志）。
        """
        folder = "user" if str(getattr(skill, "source", "")) in ("taught", "harvested") else "builtin"
        target_dir = Path(self.base_path) / folder
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            data = skill.to_dict()
            data["active"] = True
            path = target_dir / f"{skill.name}@{skill.version}.yaml"
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.error("Skills", f"技能落盘失败 {skill.name}: {exc}")
            return None

        try:
            self.add(skill, set_active=True)
        except SkillConflictError:
            # 磁盘已经是真相，下面 reload 会把内存索引对齐
            pass
        self.reload()
        return path

    def get(self, skill_id: str) -> Optional[Skill]:
        return self._registry._by_id.get(skill_id)

    def get_by_name(self, name: str) -> Optional[Skill]:
        return self._registry.get(name)

    def list_all(self) -> List[Skill]:
        """当前所有 active 版本的技能。

        默认**排除被自动降级的技能** —— 降级的技能不该再被 planner 选中
        (docs/14 §2.4)。管理界面要看到降级状态,用 `include_demoted=True`。
        """
        return self.list_all_skills(include_demoted=False)

    def list_all_skills(self, include_demoted: bool = False) -> List[Skill]:
        """列出技能。`include_demoted=True` 时连被降级的也一起返回。"""
        skills = self._registry.all()
        if include_demoted:
            return skills
        demoted = self.demoted_skill_names()
        if not demoted:
            return skills
        return [s for s in skills if s.name not in demoted]

    def demoted_skill_names(self) -> set:
        """被质量账本判定为"不靠谱"的技能名(docs/14 §2.4)。

        降级是**推导**出来的:账本里的数据变好,降级自动解除。
        记账失败时返回空集合 —— 绝不能因为账本挂了就把所有技能都禁用掉。
        """
        try:
            from core.skill_quality import get_skill_quality_store

            return set(get_skill_quality_store().demoted_skills())
        except Exception as exc:
            self.logger.warning("Skills", f"读取技能质量账本失败(不降级任何技能): {exc}")
            return set()

    def list_tool_names(self) -> List[str]:
        """当前 ToolHub 中可用的工具名(给 Skill 校验流水线用)。"""
        try:
            from tools.hub import get_tool_hub

            return list(get_tool_hub().names() or [])
        except Exception:
            return []

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

    def preview_update(
        self,
        skill_name: str,
        updates: Dict[str, object],
        *,
        version: Optional[str] = None,
    ) -> Optional[Skill]:
        """构造应用 updates 之后的候选技能(**不落盘**)。

        M3-03/M3-06 用它做"发布前校验":先在内存里算出新版本,
        跑静态校验 + 回归样例,通过了再真正写文件。
        """
        source_path = self._find_source_file(skill_name, version)
        if source_path is None:
            return None

        try:
            data = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return None

        current_version = str(data.get("version") or "1.0.0")
        new_version = self._next_version(skill_name, current_version)

        data.update(updates)
        data["version"] = new_version
        data["active"] = True

        loader = SkillLoader(Path(self.base_path))
        try:
            candidate = loader._parse_skill(data)
        except Exception as exc:
            self.logger.warning("Skills", f"preview_update: 解析候选版本失败 {skill_name}: {exc}")
            return None
        return candidate

    def update_skill(
        self,
        skill_name: str,
        updates: Dict[str, object],
        *,
        version: Optional[str] = None,
    ) -> List[str]:
        """更新技能 —— 生成新版本并切 active,旧版本保留(M3-04)。

        与旧行为(原地改写 YAML)的区别:
        - 审批补丁后不再覆盖已有文件,历史版本仍可查看/回滚
        - 新版本写为 ``<name>@<new_version>.yaml`` 并标记 ``active: true``
        - 旧版本文件标记 ``active: false``,内容保持不变
        """
        source_path = self._find_source_file(skill_name, version)
        if source_path is None:
            self.logger.warning("Skills", f"update_skill: 找不到技能文件 {skill_name}")
            return []

        try:
            data = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            self.logger.error("Skills", f"update_skill: 读取失败 {source_path}: {exc}")
            return []

        current_version = str(data.get("version") or "1.0.0")
        new_version = self._next_version(skill_name, current_version)

        data.update(updates)
        data["version"] = new_version
        data["active"] = True
        data["updated_at"] = datetime.now().isoformat()

        new_path = source_path.parent / f"{skill_name}@{new_version}.yaml"
        try:
            new_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.error("Skills", f"update_skill: 写入失败 {new_path}: {exc}")
            return []

        written = [str(new_path)]

        # 旧版本降级为非 active(文件内容保留,可回滚)
        for old_path in self._version_files(skill_name, current_version):
            if old_path.resolve() == new_path.resolve():
                continue
            if old_path.suffix.lower() != ".yaml":
                continue
            try:
                old_data = yaml.safe_load(old_path.read_text(encoding="utf-8")) or {}
                old_data["active"] = False
                old_path.write_text(
                    yaml.safe_dump(old_data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                written.append(str(old_path))
            except Exception as exc:
                self.logger.error("Skills", f"update_skill: 降级旧版本失败 {old_path}: {exc}")

        self.reload()
        self.logger.info(
            "Skills",
            f"技能 {skill_name} 更新: v{current_version} → v{new_version}(旧版本已保留)",
        )
        return written

    def match(self, user_input: str, top_k: int = 3) -> List[Skill]:
        """匹配技能 —— 被降级的不参与匹配(docs/14 §2.4)。"""
        demoted = self.demoted_skill_names()
        matched = self._registry.match(user_input, top_k)
        if not demoted:
            return matched
        return [s for s in matched if s.name not in demoted]

    def validate(self, tool_names: List[str]) -> List[str]:
        return self._registry.validate(tool_names)

    def _search_roots(self) -> List[Path]:
        """技能文件的搜索根目录。"""
        return [
            self.base_path,
            self.base_path / "builtin",
            self.base_path / "user",
            self.base_path.parent / "backend" / "skills",
        ]

    def _version_files(self, skill_name: str, version: str) -> List[Path]:
        """精确匹配 ``<name>@<version>.yaml/.md``。

        用精确文件名而不是通配符,避免 demo 误伤 demo2 这类前缀重叠。
        """
        out: List[Path] = []
        seen = set()
        for root in self._search_roots():
            if not root.exists():
                continue
            for suffix in (".yaml", ".md"):
                for path in root.rglob(f"{skill_name}@{version}{suffix}"):
                    key = str(path.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(path)
        return out

    def _find_source_file(self, skill_name: str, version: Optional[str] = None) -> Optional[Path]:
        """找到要更新的源文件。

        - 指定 version:取该版本的文件
        - 未指定:取 active 指针指向的文件
        - 都没有:退回同名的无版本文件
        """
        if version:
            candidates = self._version_files(skill_name, version)
            return candidates[0] if candidates else None

        active = self._registry.active_versions().get(skill_name)
        if active:
            candidates = self._version_files(skill_name, active)
            if candidates:
                return candidates[0]

        for root in self._search_roots():
            if not root.exists():
                continue
            for path in root.rglob(f"{skill_name}.yaml"):
                return path
        return None

    def _next_version(self, skill_name: str, current_version: str) -> str:
        """生成下一个未被占用的版本号。"""
        version = _bump_version(current_version)
        while self._version_files(skill_name, version):
            version = _bump_version(version)
        return version

    def _skill_files(self, skill_name: str, version: Optional[str] = None) -> List[Path]:
        """精确匹配某个技能的文件。

        - ``version=None``:该技能的全部版本 + 无版本号的旧文件
        - ``version`` 指定:只匹配 ``<name>@<version>.yaml/.md``

        用精确的文件名而不是通配符:``demo*`` 会误伤 ``demo2``,
        导致删除 demo 时把 demo2 一起删掉。
        """
        out: List[Path] = []
        seen = set()

        for root in self._search_roots():
            if not root.exists():
                continue

            # 版本化文件:<name>@<version>.yaml / .md
            for path in root.rglob(f"{skill_name}@*"):
                if path.suffix.lower() not in (".yaml", ".md"):
                    continue
                stem = path.stem
                if "@" not in stem:
                    continue
                file_version = stem.split("@", 1)[1]
                if version is not None and file_version != version:
                    continue
                key = str(path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                out.append(path)

            # 无版本号的旧文件:<name>.yaml / <name>.md
            if version is None:
                for suffix in (".yaml", ".md"):
                    legacy = root / f"{skill_name}{suffix}"
                    if not legacy.exists():
                        continue
                    key = str(legacy.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(legacy)

        return out

    def _delete_files(self, skill_name: str, version: Optional[str] = None) -> List[str]:
        removed: List[str] = []
        for path in self._skill_files(skill_name, version):
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
