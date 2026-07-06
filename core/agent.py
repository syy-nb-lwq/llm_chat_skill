"""Agent 核心 - 以技能为中心"""
from typing import List, Optional, Callable, Dict, Any

from storage.skill import Skill, get_skill_store


class Agent:
    """Agent - 以技能为核心"""
    
    def __init__(
        self,
        llm_client,
        system_prompt: str = ""
    ):
        self.llm = llm_client
        self.skill_store = get_skill_store()
        self.system_prompt = system_prompt
        self.context: List[Dict] = []
        self.max_iterations = 10
    
    def chat(self, user_input: str, callback: Optional[Callable] = None) -> str:
        """
        核心流程：
        1. 理解任务
        2. 选择技能（方法论 + 步骤）
        3. 执行技能（代码可选）
        """
        self.context.append({"role": "user", "content": user_input})
        
        # 1. 理解任务，选择/生成技能
        skill = self._select_skill(user_input)
        
        if callback:
            callback(f"\n📚 选择技能: {skill.name}\n")
            callback(f"方法论: {skill.method}\n")
            callback(f"步骤: {', '.join(skill.steps)}\n")
        
        # 2. 执行技能
        result = self._execute_skill(skill, user_input, callback)
        
        # 3. 返回结果
        return result
    
    def _select_skill(self, task: str) -> Skill:
        """
        选择或生成技能
        1. 搜索已有技能
        2. 如果没有，生成新技能
        """
        # 搜索已有技能
        tags = self._infer_tags(task)
        matched_skills = self.skill_store.search_by_tags(tags)
        
        if matched_skills:
            # 选择最匹配的
            return matched_skills[0]
        
        # 生成新技能
        return self._create_skill(task)
    
    def _infer_tags(self, task: str) -> List[str]:
        """推断任务标签"""
        tags = []
        task_lower = task.lower()
        
        if any(k in task_lower for k in ["分析", "分析数据"]):
            tags.append("分析")
        if any(k in task_lower for k in ["搜索", "查找", "检索"]):
            tags.append("搜索")
        if any(k in task_lower for k in ["总结", "摘要"]):
            tags.append("总结")
        if any(k in task_lower for k in ["代码", "编程", "写代码"]):
            tags.append("代码")
        if any(k in task_lower for k in ["报告", "文档"]):
            tags.append("文档")
        
        return tags or ["通用"]
    
    def _create_skill(self, task: str) -> Skill:
        """生成新技能"""
        prompt = f"""分析任务：{task}

请生成技能：
1. method: 分析这个任务的方法论
2. steps: 处理步骤列表（3-5步）
3. tags: 标签列表

返回 JSON 格式："""
        
        response = self.llm.chat(
            messages=[{"role": "user", "content": prompt}]
        )
        
        content = response.get("message", {}).get("content", "")
        
        # 解析 LLM 返回，生成 Skill
        import json
        import re
        
        skill = Skill(name=self._extract_skill_name(task))
        skill.method = self._extract_method(content)
        skill.steps = self._extract_steps(content)
        skill.tags = self._infer_tags(task)
        skill.description = f"处理: {task}"
        
        # 保存
        self.skill_store.add(skill)
        
        return skill
    
    def _extract_skill_name(self, task: str) -> str:
        """提取技能名称"""
        # 简单处理：取任务关键词
        words = task[:10] if len(task) > 10 else task
        return f"技能_{words}"
    
    def _extract_method(self, content: str) -> str:
        """提取方法论"""
        lines = content.split('\n')
        method_lines = []
        for line in lines:
            if any(k in line for k in ['方法', '方法论', '思路']):
                method_lines.append(line)
        return '\n'.join(method_lines) or "通用分析方法"
    
    def _extract_steps(self, content: str) -> List[str]:
        """提取步骤"""
        import re
        steps = re.findall(r'\d+\.\s*(.+)', content)
        return steps[:5] if steps else ["理解任务", "分析需求", "输出结果"]
    
    def _execute_skill(self, skill: Skill, task: str, callback: Optional[Callable] = None) -> str:
        """执行技能"""
        # 构建上下文
        messages = []
        
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        
        messages.append({
            "role": "system", 
            "content": f"""技能：{skill.name}
方法论：{skill.method}
步骤：{', '.join(skill.steps)}
代码：{skill.code or '无'}"""
        })
        
        messages.extend(self.context)
        
        # 调用 LLM
        response = self.llm.chat(messages=messages)
        
        result = response.get("message", {}).get("content", "执行完成")
        
        self.context.append({"role": "assistant", "content": result})
        
        return result
    
    def reset(self):
        """重置"""
        self.context.clear()
