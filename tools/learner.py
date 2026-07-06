"""技能学习 Agent - 子 Agent"""
import json
import re
from typing import Dict, List, Optional
from openai import OpenAI

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
from tools.skill import Skill, get_skill_store
from tools.code_runner import run_code, run_function


class SkillLearningAgent:
    """技能学习子 Agent"""
    
    def __init__(self, model: str = None, api_key: str = None, base_url: str = None):
        self.client = OpenAI(
            api_key=api_key or LLM_API_KEY,
            base_url=base_url or LLM_BASE_URL
        )
        self.model = model or LLM_MODEL
        self.max_attempts = 3
    
    def learn_from_description(self, requirement: str) -> Optional[Skill]:
        """从需求描述学习技能"""
        # 检查是否已存在相似技能
        store = get_skill_store()
        existing = self._find_similar_skill(requirement, store)
        if existing:
            return existing
        
        prompt = f"""
请根据以下需求设计一个技能：

{requirement}

请返回以下格式的 JSON：
{{
    "name": "技能名称（英文，用于函数名）",
    "description": "技能描述",
    "parameters": {{
        "param_name": {{
            "type": "参数类型",
            "description": "参数描述"
        }}
    }},
    "code": "完整的 Python 函数代码，包含 import 语句",
    "examples": ["使用示例1", "使用示例2"]
}}
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个技能设计专家。请确保返回的代码是完整可执行的，包含所有必要的 import 语句。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=4000
        )
        
        content = response.choices[0].message.content
        
        try:
            # 提取 JSON
            json_match = re.search(r'```json\s*(.+?)\s*```', content, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1))
            else:
                json_match = re.search(r'\{.+}', content, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                else:
                    data = json.loads(content)
            
            # 提取代码
            code_match = re.search(r'```python\s*(.+?)\s*```', content, re.DOTALL)
            code = code_match.group(1).strip() if code_match else data.get('code', '')
            
            if not code:
                return None
            
            skill = Skill(
                name=data["name"],
                description=data["description"],
                code=code,  # 确保代码不为空
                parameters=data.get("parameters", {}),
                examples=data.get("examples", [])
            )
            
            # 验证代码
            if self._verify_and_fix(skill):
                # 保存到存储
                store.add(skill)
                return skill
            
            return None
            
        except Exception as e:
            print(f"解析技能定义失败: {e}")
            return None
    
    def _find_similar_skill(self, requirement: str, store: 'SkillStore') -> Optional[Skill]:
        """查找相似的已存在技能"""
        requirement_lower = requirement.lower()
        
        # 关键词匹配
        keywords = []
        if '天气' in requirement or 'weather' in requirement_lower:
            keywords.append('weather')
        if 'excel' in requirement_lower or '表格' in requirement:
            keywords.append('excel')
        if 'pdf' in requirement_lower:
            keywords.append('pdf')
        
        if not keywords:
            return None
        
        # 检查现有技能
        for skill in store.list_all():
            skill_desc = (skill.name + ' ' + skill.description).lower()
            for kw in keywords:
                if kw in skill_desc and skill.code.strip():
                    return skill
        
        return None
    
    def _verify_and_fix(self, skill: Skill, max_attempts: int = 3) -> bool:
        """验证并修复代码"""
        for attempt in range(max_attempts):
            result = run_code(skill.code)
            
            if result.startswith("✅"):
                return True
            
            if attempt < max_attempts - 1:
                fix_prompt = f"""
原始代码有错误：
{result}

请修复以下代码中的问题，只返回修复后的代码（包含 import 语句）：

{skill.code}
"""
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是一个代码修复专家。只返回修复后的完整代码，包含所有 import 语句。"},
                        {"role": "user", "content": fix_prompt}
                    ],
                    max_tokens=2000
                )
                
                fixed_code = response.choices[0].message.content
                code_match = re.search(r'```python\n(.+?)```', fixed_code, re.DOTALL)
                if code_match:
                    skill.code = code_match.group(1).strip()
                else:
                    # 如果没有代码块，尝试清理
                    lines = fixed_code.split('\n')
                    code_lines = []
                    in_code = False
                    for line in lines:
                        if '```' in line:
                            in_code = not in_code
                            continue
                        if in_code or (line.strip() and not line.startswith('#')):
                            code_lines.append(line)
                    if code_lines:
                        skill.code = '\n'.join(code_lines).strip()
        
        return False
    
    def create_skill(self, name: str, description: str, code: str, 
                    parameters: dict, examples: List[str] = None) -> Skill:
        """创建技能"""
        skill = Skill(
            name=name,
            description=description,
            code=code,
            parameters=parameters,
            examples=examples or []
        )
        
        if not self._verify_and_fix(skill):
            print("警告：代码验证失败，但仍将保存")
        
        store = get_skill_store()
        skill_id = store.add(skill)
        skill.id = skill_id
        
        return skill
    
    def save_skill(self, skill: Skill) -> str:
        """保存技能到文件"""
        store = get_skill_store()
        return store.add(skill)
    
    def list_skills(self) -> List[Skill]:
        """列出所有技能"""
        store = get_skill_store()
        return store.list_all()
    
    def delete_skill(self, skill_id: str) -> bool:
        """删除技能"""
        store = get_skill_store()
        return store.delete(skill_id)


_learner: Optional[SkillLearningAgent] = None


def get_learner() -> SkillLearningAgent:
    global _learner
    if _learner is None:
        _learner = SkillLearningAgent()
    return _learner
