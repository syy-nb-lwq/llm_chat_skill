"""高级技能学习 Agent - 主动学习型"""
import json
import re
from typing import Dict, List, Optional
from openai import OpenAI

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
from tools.skill import Skill, get_skill_store
from tools.code_runner import run_code, run_function
from tools.fetch import fetch_webpage


class AdvancedSkillLearner:
    """高级技能学习 Agent - 主动学习型"""
    
    SYSTEM_PROMPT = """你是一个技能学习专家。你能够：

1. 分析用户需求，判断是否需要创建新技能
2. 如果需要，主动搜索相关资料
3. 整合学习资料，生成高质量技能代码
4. 验证代码正确性，自动修复问题

工作流程：
1. 理解用户需求
2. 检查现有技能，判断是否已有相关技能
3. 如果需要新技能，生成搜索查询
4. 综合搜索结果和需求，生成技能代码
5. 验证并保存技能

重要：
- 代码必须完整可执行，包含所有 import
- 必须考虑边界情况
- 必须包含详细的文档字符串"""
    
    def __init__(self, model: str = None, api_key: str = None, base_url: str = None):
        self.client = OpenAI(
            api_key=api_key or LLM_API_KEY,
            base_url=base_url or LLM_BASE_URL
        )
        self.model = model or LLM_MODEL
        self.max_attempts = 3
    
    def analyze_and_learn(self, requirement: str) -> Dict:
        """分析需求并学习技能"""
        store = get_skill_store()
        
        # 1. 检查现有技能
        existing = self._find_similar_skill(requirement, store)
        if existing:
            return {
                "status": "existing",
                "skill": existing,
                "message": f"发现已有相关技能: {existing.name}"
            }
        
        # 2. 分析需求，生成搜索查询
        search_queries = self._generate_search_queries(requirement)
        
        # 3. 主动搜索学习资料
        learning_materials = []
        for query in search_queries[:3]:  # 限制搜索次数
            content = self._search_and_fetch(query)
            if content:
                learning_materials.append({
                    "query": query,
                    "content": content[:3000]  # 限制长度
                })
        
        # 4. 基于需求和资料生成技能
        skill = self._generate_skill(requirement, learning_materials)
        
        if skill:
            # 5. 验证代码
            if self._verify_and_fix(skill):
                store.add(skill)
                return {
                    "status": "learned",
                    "skill": skill,
                    "materials_used": len(learning_materials),
                    "message": f"成功学习新技能: {skill.name}"
                }
            else:
                return {
                    "status": "failed",
                    "skill": skill,
                    "message": "技能代码验证失败"
                }
        
        return {
            "status": "failed",
            "message": "无法生成技能"
        }
    
    def _find_similar_skill(self, requirement: str, store: 'SkillStore') -> Optional[Skill]:
        """查找相似技能"""
        requirement_lower = requirement.lower()
        
        # 提取关键词
        keywords = []
        if '天气' in requirement or 'weather' in requirement_lower:
            keywords.extend(['weather', '天气', 'temperature'])
        if 'excel' in requirement_lower or '表格' in requirement or 'spreadsheet' in requirement_lower:
            keywords.extend(['excel', '表格', 'spreadsheet'])
        if 'pdf' in requirement_lower:
            keywords.extend(['pdf', 'document'])
        if '搜索' in requirement or 'search' in requirement_lower:
            keywords.extend(['search', 'search_web'])
        if '翻译' in requirement or 'translate' in requirement_lower:
            keywords.extend(['translate', 'translation'])
        
        if not keywords:
            # 使用 LLM 判断
            judgment = self._judge_skill_needed(requirement, store.list_all())
            if not judgment["needed"]:
                return store.get_by_name(judgment.get("skill_name", ""))
            keywords = judgment.get("keywords", [])
        
        # 搜索现有技能
        for skill in store.list_all():
            skill_desc = (skill.name + ' ' + skill.description).lower()
            for kw in keywords:
                if kw in skill_desc and skill.code.strip():
                    return skill
        
        return None
    
    def _judge_skill_needed(self, requirement: str, existing_skills: List[Skill]) -> Dict:
        """使用 LLM 判断是否需要新技能"""
        skills_info = "\n".join([f"- {s.name}: {s.description}" for s in existing_skills])
        
        prompt = f"""
用户需求：{requirement}

现有技能：
{skills_info}

请判断：
1. 现有技能是否满足需求？
2. 如果不满足，需要什么类型的新技能？
3. 提供3-5个关键词用于搜索

返回 JSON 格式：
{{"needed": true/false, "skill_name": "技能名称", "keywords": ["关键词1", "关键词2"]}}
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个技能分析专家。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=500
        )
        
        try:
            content = response.choices[0].message.content
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        
        return {"needed": True, "keywords": []}
    
    def _generate_search_queries(self, requirement: str) -> List[str]:
        """生成搜索查询"""
        prompt = f"""
用户需求：{requirement}

请生成3个搜索查询，用于查找相关的技术文档、API使用说明等。

要求：
- 查询应该是英文，适合在技术文档网站搜索
- 覆盖需求的不同方面

返回 JSON 数组格式：
["查询1", "查询2", "查询3"]
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个搜索专家。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=300
        )
        
        try:
            content = response.choices[0].message.content
            json_match = re.search(r'\[.*\]', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        except:
            pass
        
        return [requirement, requirement + " python", requirement + " API tutorial"]
    
    def _search_and_fetch(self, query: str) -> Optional[str]:
        """搜索并获取内容"""
        try:
            # 构造搜索 URL（使用 DuckDuckGo）
            search_url = f"https://duckduckgo.com/?q={query}&ia=web"
            
            # 获取搜索结果页
            result = fetch_webpage(search_url)
            if not result["success"]:
                return None
            
            # 简化处理：返回摘要
            text = result.get("text", "")
            if text:
                return f"搜索 '{query}' 的结果摘要：\n{text[:2000]}"
            
            return None
        except Exception as e:
            print(f"搜索失败: {e}")
            return None
    
    def _generate_skill(self, requirement: str, materials: List[Dict]) -> Optional[Skill]:
        """基于需求和资料生成技能"""
        materials_text = ""
        for m in materials:
            materials_text += f"\n\n=== {m['query']} ===\n{m['content']}"
        
        prompt = f"""
请根据以下需求和相关资料，设计一个技能：

## 需求
{requirement}

## 学习资料
{materials_text}

请返回以下格式的 JSON（确保 code 字段包含完整可执行的 Python 代码，包含所有 import）：

```json
{{
    "name": "技能名称（英文，用于函数名，只有一个词或简短的词组）",
    "description": "技能描述",
    "parameters": {{
        "参数名": {{
            "type": "类型",
            "description": "描述"
        }}
    }},
    "code": "完整的 Python 函数代码，包含所有 import 语句",
    "examples": ["使用示例"]
}}
```
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一个技能设计专家。请确保返回的 code 字段包含完整代码，包括所有 import 语句。"},
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
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    data = json.loads(json_match.group())
                else:
                    data = json.loads(content)
            
            # 提取代码
            code_match = re.search(r'```python\s*([\s\S]*?)\s*```', content)
            code = code_match.group(1).strip() if code_match else data.get('code', '')
            
            if not code:
                return None
            
            skill = Skill(
                name=data.get("name", "custom_skill"),
                description=data.get("description", requirement),
                code=code,
                parameters=data.get("parameters", {}),
                examples=data.get("examples", [])
            )
            
            return skill
            
        except Exception as e:
            print(f"生成技能失败: {e}")
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

请修复以下代码中的问题，只返回修复后的完整代码（包含 import 语句）：

{skill.code}
"""
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是一个代码修复专家。只返回修复后的完整代码。"},
                        {"role": "user", "content": fix_prompt}
                    ],
                    max_tokens=2000
                )
                
                fixed_code = response.choices[0].message.content
                code_match = re.search(r'```python\n([\s\S]*?)```', fixed_code)
                if code_match:
                    skill.code = code_match.group(1).strip()
                else:
                    skill.code = fixed_code.strip()
        
        return False


_advanced_learner: Optional[AdvancedSkillLearner] = None


def get_advanced_learner() -> AdvancedSkillLearner:
    global _advanced_learner
    if _advanced_learner is None:
        _advanced_learner = AdvancedSkillLearner()
    return _advanced_learner
