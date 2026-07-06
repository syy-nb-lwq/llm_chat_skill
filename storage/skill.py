"""技能存储 - 技能 = 方法论 + 处理流程 + 代码"""
import json
import re
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Skill:
    """
    技能定义
    
    技能不只是代码，而是一套完成任务的方法论：
    - method: 分析问题的方法
    - steps: 处理步骤/流程
    - code: 可选的执行代码
    """
    id: str
    name: str
    description: str
    
    # 核心：方法论
    method: str = ""  # 分析问题的方法论
    steps: List[str] = field(default_factory=list)  # 处理步骤
    
    # 可选：代码
    code: str = ""  # 可选的执行代码
    
    # 元数据
    version: str = "1.0"
    tags: List[str] = field(default_factory=list)  # 技能标签
    examples: List[str] = field(default_factory=list)  # 使用示例
    
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_markdown(self) -> str:
        """导出为 Markdown"""
        steps_str = "\n".join(f"{i+1}. {s}" for i, s in enumerate(self.steps)) if self.steps else "无"
        examples_str = "\n".join(f"- {ex}" for ex in self.examples) if self.examples else "无"
        tags_str = ", ".join(self.tags) if self.tags else "无"
        
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
{self.code}
```

## 使用示例
{examples_str}
"""
    
    @classmethod
    def from_markdown(cls, content: str, file_id: str = None) -> Optional['Skill']:
        """从 Markdown 导入"""
        try:
            # 提取基本信息
            id_match = re.search(r'- ID: (.+)', content)
            version_match = re.search(r'- 版本: (.+)', content)
            created_match = re.search(r'- 创建时间: (.+)', content)
            tags_match = re.search(r'- 标签: (.+)', content)
            
            name_match = re.search(r'# 技能：(.+)', content)
            name = name_match.group(1).strip() if name_match else "unknown"
            
            desc_match = re.search(r'## 描述\n(.+?)(?=##)', content, re.DOTALL)
            description = desc_match.group(1).strip() if desc_match else ""
            
            method_match = re.search(r'## 方法论\n(.+?)(?=##)', content, re.DOTALL)
            method = method_match.group(1).strip() if method_match else ""
            
            # 提取步骤
            steps = []
            steps_match = re.search(r'## 处理步骤\n([\s\S]+?)(?=##)', content)
            if steps_match:
                for line in steps_match.group(1).strip().split('\n'):
                    line = line.strip()
                    if line and line[0].isdigit():
                        steps.append(re.sub(r'^\d+\.\s*', '', line))
            
            # 提取代码
            code_match = re.search(r'```python\n([\s\S]*?)```', content)
            code = code_match.group(1).strip() if code_match else ""
            
            # 提取标签
            tags = []
            if tags_match:
                tags = [t.strip() for t in tags_match.group(1).split(',')]
            
            # 提取示例
            examples = re.findall(r'- (.+)', content)
            
            return cls(
                id=id_match.group(1).strip() if id_match else file_id or str(uuid.uuid4()),
                name=name,
                description=description,
                method=method,
                steps=steps,
                code=code,
                version=version_match.group(1).strip() if version_match else "1.0",
                tags=tags,
                examples=examples,
                created_at=created_match.group(1).strip() if created_match else None
            )
        except Exception as e:
            print(f"解析技能失败: {e}")
            return None
    
    def needs_code(self) -> bool:
        """是否需要代码"""
        return bool(self.code.strip())
    
    def get_capabilities(self) -> List[str]:
        """获取技能提供的核心能力"""
        return self.tags or []


class SkillStore:
    """技能存储"""
    
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
        # 检查是否已有同名技能
        for existing in self._skills.values():
            if existing.name.lower() == skill.name.lower():
                # 更新
                existing.method = skill.method
                existing.steps = skill.steps
                existing.code = skill.code
                existing.tags = skill.tags
                self._save(existing)
                return existing.id
        
        self._skills[skill.id] = skill
        self._save(skill)
        return skill.id
    
    def get(self, skill_id: str) -> Optional[Skill]:
        """获取技能"""
        return self._skills.get(skill_id)
    
    def get_by_name(self, name: str) -> Optional[Skill]:
        """按名称获取"""
        for skill in self._skills.values():
            if skill.name.lower() == name.lower():
                return skill
        return None
    
    def search_by_tags(self, tags: List[str]) -> List[Skill]:
        """按标签搜索"""
        results = []
        for skill in self._skills.values():
            if any(tag in skill.tags for tag in tags):
                results.append(skill)
        return results
    
    def list_all(self) -> List[Skill]:
        """列出所有技能"""
        return list(self._skills.values())
    
    def delete(self, skill_id: str) -> bool:
        """删除技能"""
        if skill_id in self._skills:
            skill = self._skills.pop(skill_id)
            file = self.path / f"{skill.id}.md"
            if file.exists():
                file.unlink()
            return True
        return False
    
    def _save(self, skill: Skill):
        """保存技能"""
        file = self.path / f"{skill.id}.md"
        file.write_text(skill.to_markdown(), encoding="utf-8")
    
    def reload(self):
        """重新加载"""
        self._skills.clear()
        self._load_all()
    
    def count(self) -> int:
        """技能数量"""
        return len(self._skills)


# 全局实例
_store: Optional[SkillStore] = None


def get_skill_store(path: str = "skills") -> SkillStore:
    global _store
    if _store is None:
        _store = SkillStore(path)
    return _store
