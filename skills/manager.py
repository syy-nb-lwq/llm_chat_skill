"""技能管理器"""
import re
import uuid
from pathlib import Path
from typing import List, Optional, Dict
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Skill:
    """
    技能 = 方法论 + 步骤
    
    技能是一套完成任务的方法论，不是工具
    """
    id: str = ""
    name: str = ""
    
    # 能力描述：这个技能能做什么
    capability: str = ""
    
    # 匹配模式：什么意图会触发这个技能
    patterns: List[str] = field(default_factory=list)
    
    # 方法论：如何分析问题
    method: str = ""
    
    # 步骤：如何执行
    steps: List[str] = field(default_factory=list)
    
    # 标签：技能分类
    tags: List[str] = field(default_factory=list)
    
    # 示例输入
    examples: List[str] = field(default_factory=list)
    
    # 元数据
    version: str = "1.0"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def __post_init__(self):
        if not self.id:
            self.id = f"skill_{uuid.uuid4().hex[:8]}"
    
    def matches(self, intent: str) -> bool:
        """检查用户意图是否匹配此技能"""
        intent_lower = intent.lower()
        for pattern in self.patterns:
            if pattern.lower() in intent_lower:
                return True
        return False
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "capability": self.capability,
            "patterns": self.patterns,
            "method": self.method,
            "steps": self.steps,
            "tags": self.tags,
            "examples": self.examples
        }


class SkillStore:
    """技能库管理器"""
    
    def __init__(self, path: str = None):
        if path is None:
            # 使用项目根目录的 skills 文件夹
            root = Path(__file__).parent.parent
            path = root / "skills"
        else:
            path = Path(path)
        self.path = path
        self.path.mkdir(exist_ok=True)
        self._skills: Dict[str, Skill] = {}
        self._load_all()
    
    def _load_all(self):
        """加载所有技能"""
        for file in self.path.glob("*.md"):
            skill = self._parse_skill_file(file)
            if skill:
                self._skills[skill.id] = skill
    
    def _parse_skill_file(self, file: Path) -> Optional[Skill]:
        """解析技能文件"""
        try:
            content = file.read_text(encoding="utf-8")
            skill = Skill()
            skill.id = file.stem
            
            # 解析各个字段
            if m := re.search(r"# 技能：(.+)", content):
                skill.name = m.group(1).strip()
            
            if m := re.search(r"## 能力\n([\s\S]+?)(?=##)", content):
                skill.capability = m.group(1).strip()
            
            if m := re.search(r"## 匹配模式\n([\s\S]+?)(?=##)", content):
                skill.patterns = re.findall(r"- (.+)", m.group(1))
            
            if m := re.search(r"## 方法论\n([\s\S]+?)(?=##)", content):
                skill.method = m.group(1).strip()
            
            if m := re.search(r"## 步骤\n([\s\S]+?)(?=##)", content):
                for line in m.group(1).strip().split("\n"):
                    line = line.strip()
                    if line and (line[0].isdigit() or line.startswith("-")):
                        line = re.sub(r"^[\d]+\.\s*", "", line)
                        line = re.sub(r"^-\s*", "", line)
                        if line:
                            skill.steps.append(line)
            
            if m := re.search(r"## 标签\n([\s\S]+?)(?=##)", content):
                skill.tags = re.findall(r"- (.+)", m.group(1))
            
            if not skill.name:
                return None
            
            return skill
        except Exception as e:
            print(f"解析技能失败 {file}: {e}")
            return None
    
    def add(self, skill: Skill) -> str:
        """添加技能"""
        self._skills[skill.id] = skill
        self._save(skill)
        return skill.id
    
    def get(self, skill_id: str) -> Optional[Skill]:
        return self._skills.get(skill_id)
    
    def get_by_name(self, name: str) -> Optional[Skill]:
        """通过名称查找技能"""
        for skill in self._skills.values():
            if skill.name == name:
                return skill
        return None
    
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
        """保存技能到文件"""
        patterns_str = "\n".join(f"- {p}" for p in skill.patterns) if skill.patterns else "- 无"
        steps_str = "\n".join(f"{i+1}. {s}" for i, s in enumerate(skill.steps)) if skill.steps else "- 无"
        tags_str = "\n".join(f"- {t}" for t in skill.tags) if skill.tags else "- 无"
        examples_str = "\n".join(f"- {e}" for e in skill.examples) if skill.examples else "- 无"
        
        content = f"""# 技能：{skill.name}

## 能力
{skill.capability}

## 匹配模式
{patterns_str}

## 方法论
{skill.method}

## 步骤
{steps_str}

## 标签
{tags_str}

## 示例输入
{examples_str}

## 元数据
- 版本: {skill.version}
- 创建时间: {skill.created_at}
"""
        file = self.path / f"{skill.id}.md"
        file.write_text(content, encoding="utf-8")


# 全局实例
_store: Optional[SkillStore] = None


def get_skill_store(path: str = None) -> SkillStore:
    """获取技能库管理器实例"""
    global _store
    if _store is None:
        _store = SkillStore(path)
    return _store


def reset_skill_store():
    """重置技能库"""
    global _store
    _store = None
