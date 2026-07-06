"""技能存储"""
import json
import re
import uuid
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class Skill:
    """技能"""
    id: str
    name: str
    description: str
    code: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)
    version: str = "1.0"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_markdown(self) -> str:
        """导出为 Markdown"""
        params_json = json.dumps(self.parameters, ensure_ascii=False, indent=2)
        examples_str = "\n".join(f"- `{ex}`" for ex in self.examples) if self.examples else "无"
        
        return f"""# 技能：{self.name}

## 元信息
- ID: {self.id}
- 版本: {self.version}
- 创建时间: {self.created_at}

## 描述
{self.description}

## 参数说明
```json
{params_json}
```

## 代码
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
            
            name_match = re.search(r'# 技能：(.+)', content)
            name = name_match.group(1).strip() if name_match else "unknown"
            
            desc_match = re.search(r'## 描述\n(.+?)(?=##|$)', content, re.DOTALL)
            description = desc_match.group(1).strip() if desc_match else ""
            
            # 提取代码
            code_match = re.search(r'```python\n([\s\S]*?)```', content)
            code = code_match.group(1).strip() if code_match else ""
            
            if not code:
                return None
            
            # 提取参数
            params_match = re.search(r'```json\n([\s\S]*?)```', content)
            parameters = json.loads(params_match.group(1)) if params_match else {}
            
            # 提取示例
            examples = re.findall(r'`([^`]+)`', content)
            
            skill = cls(
                id=id_match.group(1).strip() if id_match else file_id or str(uuid.uuid4()),
                name=name,
                description=description,
                code=code,
                parameters=parameters,
                examples=examples,
                version=version_match.group(1).strip() if version_match else "1.0",
                created_at=created_match.group(1).strip() if created_match else None
            )
            
            return skill
        except Exception as e:
            print(f"解析技能失败: {e}")
            return None


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
            else:
                # 代码为空，删除文件
                print(f"删除无效技能: {file.name}")
                file.unlink()
    
    def add(self, skill: Skill) -> str:
        """添加技能"""
        # 检查是否已有同名技能
        for existing in self._skills.values():
            if existing.name.lower() == skill.name.lower():
                existing.code = skill.code
                existing.description = skill.description
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
    
    def list_all(self) -> List[Skill]:
        """列出所有技能"""
        return list(self._skills.values())
    
    def list_valid(self) -> List[Skill]:
        """列出有效技能"""
        return [s for s in self._skills.values() if s.code.strip()]
    
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
