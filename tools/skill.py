"""技能系统 - 管理和加载技能"""
import os
import json
import re
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Callable
from datetime import datetime


class Skill:
    """技能定义"""
    
    def __init__(self, name: str, description: str, code: str = "", 
                 parameters: dict = None, examples: List[str] = None,
                 version: str = "1.0", created_at: str = None):
        self.name = name
        self.description = description
        self.code = code or ""
        self.parameters = parameters or {}
        self.examples = examples or []
        self.version = version
        self.created_at = created_at or datetime.now().isoformat()
        self.id = self._generate_id()
    
    def _generate_id(self) -> str:
        """生成唯一ID"""
        return f"skill_{self.name.lower().replace(' ', '_')}_{uuid.uuid4().hex[:8]}"
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "code": self.code,
            "parameters": self.parameters,
            "examples": self.examples,
            "version": self.version,
            "created_at": self.created_at
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Skill':
        skill = cls(
            name=data.get("name", "unknown"),
            description=data.get("description", ""),
            code=data.get("code", ""),
            parameters=data.get("parameters", {}),
            examples=data.get("examples", []),
            version=data.get("version", "1.0"),
            created_at=data.get("created_at")
        )
        skill.id = data.get("id", skill.id)
        return skill
    
    def to_markdown(self) -> str:
        """转换为 Markdown 格式"""
        params_str = json.dumps(self.parameters, ensure_ascii=False, indent=2)
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
{params_str}
```

## 函数代码
```python
{self.code}
```

## 使用示例
{examples_str}
"""
    
    @classmethod
    def from_markdown(cls, content: str, file_id: str = None) -> Optional['Skill']:
        """从 Markdown 解析"""
        try:
            # 提取名称
            name_match = re.search(r'# 技能：(.+)', content)
            name = name_match.group(1).strip() if name_match else "unknown"
            
            # 提取描述
            desc_match = re.search(r'## 描述\n(.+?)(?=##|$)', content, re.DOTALL)
            description = desc_match.group(1).strip() if desc_match else ""
            
            # 提取代码
            code_match = re.search(r'```python\n([\s\S]*?)```', content)
            code = code_match.group(1).strip() if code_match else ""
            
            # 如果代码为空，跳过这个技能
            if not code.strip():
                return None
            
            # 提取参数
            params_match = re.search(r'```json\n([\s\S]*?)```', content)
            parameters = json.loads(params_match.group(1)) if params_match else {}
            
            # 提取示例
            examples = re.findall(r'`([^`]+)`', content)
            
            version_match = re.search(r'- 版本: (.+)', content)
            created_match = re.search(r'- 创建时间: (.+)', content)
            id_match = re.search(r'- ID: (.+)', content)
            
            skill = cls(
                name=name,
                description=description,
                code=code,
                parameters=parameters,
                examples=examples,
                version=version_match.group(1).strip() if version_match else "1.0",
                created_at=created_match.group(1).strip() if created_match else None
            )
            
            if id_match:
                skill.id = id_match.group(1).strip()
            elif file_id:
                skill.id = file_id
            
            return skill
        except Exception as e:
            print(f"解析技能文件失败: {e}")
            return None


class SkillStore:
    """技能存储"""
    
    def __init__(self, store_dir: str = "skills"):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(exist_ok=True)
        self._skills: Dict[str, Skill] = {}
        self._load_all()
    
    def _load_all(self):
        """加载所有技能"""
        for file in self.store_dir.glob("*.md"):
            try:
                content = file.read_text(encoding='utf-8')
                # 传递文件ID
                file_id = file.stem
                skill = Skill.from_markdown(content, file_id)
                if skill:
                    self._skills[skill.id] = skill
                else:
                    # 代码为空，删除文件
                    print(f"删除无效技能文件: {file.name} (代码为空)")
                    file.unlink()
            except Exception as e:
                print(f"加载技能失败 {file}: {e}")
    
    def add(self, skill: Skill) -> str:
        """添加技能"""
        # 检查是否已存在同名技能
        for existing in self._skills.values():
            if existing.name.lower() == skill.name.lower():
                # 更新现有技能
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
        """列出有效技能（代码不为空）"""
        return [s for s in self._skills.values() if s.code.strip()]
    
    def delete(self, skill_id: str) -> bool:
        """删除技能"""
        if skill_id in self._skills:
            skill = self._skills.pop(skill_id)
            file_path = self.store_dir / f"{skill.id}.md"
            if file_path.exists():
                file_path.unlink()
            return True
        return False
    
    def _save(self, skill: Skill):
        """保存技能到文件"""
        file_path = self.store_dir / f"{skill.id}.md"
        file_path.write_text(skill.to_markdown(), encoding='utf-8')
    
    def reload(self):
        """重新加载"""
        self._skills.clear()
        self._load_all()
    
    def count(self) -> int:
        """技能数量"""
        return len(self._skills)


# 全局单例
_skill_store: Optional[SkillStore] = None


def get_skill_store() -> SkillStore:
    global _skill_store
    if _skill_store is None:
        _skill_store = SkillStore()
    return _skill_store
