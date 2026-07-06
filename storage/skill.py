"""技能存储"""
import json
import re
import uuid
from pathlib import Path
from typing import List, Optional, Dict
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Skill:
    """
    技能 = 方法论 + 步骤 + 代码（可选）
    
    技能不只是代码，是一套完成任务的方法论
    """
    id: str = ""
    name: str = ""
    description: str = ""
    
    # 方法论：如何分析问题
    method: str = ""
    
    # 步骤：如何执行
    steps: List[str] = field(default_factory=list)
    
    # 代码：可选的执行代码
    code: str = ""
    
    # 标签：技能分类
    tags: List[str] = field(default_factory=list)
    
    # 示例
    examples: List[str] = field(default_factory=list)
    
    # 元数据
    version: str = "1.0"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def __post_init__(self):
        if not self.id:
            self.id = f"skill_{uuid.uuid4().hex[:8]}"
    
    def to_markdown(self) -> str:
        """导出为 Markdown"""
        steps_str = "\n".join(f"{i+1}. {s}" for i, s in enumerate(self.steps)) if self.steps else "无"
        tags_str = ", ".join(self.tags) if self.tags else "无"
        examples_str = "\n".join(f"- {e}" for e in self.examples) if self.examples else "无"
        
        return f"""# 技能：{self.name}

## 元信息
- ID: {self.id}
- 版本: {self.version}
- 标签: {tags_str}
- 创建时间: {self.created_at}

## 描述
{self.description}

## 方法论
{self.method}

## 处理步骤
{steps_str}

## 代码（可选）
```python
{self.code or "无"}
```

## 使用示例
{examples_str}
"""
    
    @classmethod
    def from_markdown(cls, content: str, file_id: str = "") -> Optional["Skill"]:
        """从 Markdown 解析"""
        try:
            skill = cls()
            
            # ID
            if m := re.search(r"- ID: (.+)", content):
                skill.id = m.group(1).strip()
            elif file_id:
                skill.id = file_id
            
            # 版本
            if m := re.search(r"- 版本: (.+)", content):
                skill.version = m.group(1).strip()
            
            # 标签
            if m := re.search(r"- 标签: (.+)", content):
                skill.tags = [t.strip() for t in m.group(1).split(",")]
            
            # 名称
            if m := re.search(r"# 技能：(.+)", content):
                skill.name = m.group(1).strip()
            
            # 描述
            if m := re.search(r"## 描述\n(.+?)(?=##)", content, re.DOTALL):
                skill.description = m.group(1).strip()
            
            # 方法论
            if m := re.search(r"## 方法论\n(.+?)(?=##)", content, re.DOTALL):
                skill.method = m.group(1).strip()
            
            # 步骤
            skill.steps = []
            if m := re.search(r"## 处理步骤\n([\s\S]+?)(?=##)", content):
                for line in m.group(1).strip().split("\n"):
                    line = line.strip()
                    if line and line[0].isdigit():
                        skill.steps.append(re.sub(r"^\d+\.\s*", "", line))
            
            # 代码
            if m := re.search(r"```python\n([\s\S]+?)```", content):
                skill.code = m.group(1).strip()
            
            # 示例
            skill.examples = re.findall(r"- (.+)", content)
            
            if not skill.name:
                return None
            
            return skill
        except Exception as e:
            print(f"解析技能失败: {e}")
            return None


class SkillStore:
    """技能库"""
    
    def __init__(self, path: str = "skills"):
        self.path = Path(path)
        self.path.mkdir(exist_ok=True)
        self._skills: Dict[str, Skill] = {}
        self._load_all()
    
    def _load_all(self):
        """加载所有技能"""
        for file in self.path.glob("*.md"):
            skill = Skill.from_markdown(file.read_text(encoding="utf-8"), file.stem)
            if skill:
                self._skills[skill.id] = skill
    
    def add(self, skill: Skill) -> str:
        """添加技能"""
        # 检查同名
        for s in self._skills.values():
            if s.name.lower() == skill.name.lower():
                # 更新
                s.method = skill.method
                s.steps = skill.steps
                s.code = skill.code
                s.tags = skill.tags
                self._save(s)
                return s.id
        
        self._skills[skill.id] = skill
        self._save(skill)
        return skill.id
    
    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills.get(skill_id)
    
    def get_by_name(self, name: str) -> Optional[Skill]:
        for s in self._skills.values():
            if s.name.lower() == name.lower():
                return s
        return None
    
    def search_by_tags(self, tags: List[str]) -> List[Skill]:
        return [s for s in self._skills.values() 
                if any(t in s.tags for t in tags)]
    
    def list_all(self) -> List[Skill]:
        return list(self._skills.values())
    
    def delete(self, skill_id: str) -> bool:
        if skill_id in self._skills:
            skill = self._skills.pop(skill_id)
            file = self.path / f"{skill.id}.md"
            if file.exists():
                file.unlink()
            return True
        return False
    
    def _save(self, skill: Skill):
        file = self.path / f"{skill.id}.md"
        file.write_text(skill.to_markdown(), encoding="utf-8")
    
    def reload(self):
        self._skills.clear()
        self._load_all()
    
    def count(self) -> int:
        return len(self._skills)


# 全局实例
_store: Optional[SkillStore] = None


def get_skill_store(path: str = "skills") -> SkillStore:
    global _store
    if _store is None:
        _store = SkillStore(path)
    return _store
