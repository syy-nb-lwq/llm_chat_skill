"""技能和代码执行工具"""
import json
from tools.skill import get_skill_store, Skill
from tools.learner import get_learner
from tools.advanced_learner import get_advanced_learner
from tools.code_runner import run_code, run_function


def create_skill(name: str, description: str, code: str, 
                parameters: dict = None, examples: list = None) -> str:
    """创建新技能"""
    try:
        if not code or not code.strip():
            return "❌ 技能代码不能为空"
        
        learner = get_learner()
        skill = learner.create_skill(
            name=name,
            description=description,
            code=code,
            parameters=parameters or {},
            examples=examples or []
        )
        return f"✅ 技能已创建\n\nID: {skill.id}\n名称: {skill.name}\n\n代码:\n{skill.code}"
    except Exception as e:
        return f"❌ 创建技能失败: {str(e)}"


def learn_skill(requirement: str) -> str:
    """从需求学习技能（主动学习型）"""
    try:
        learner = get_advanced_learner()
        result = learner.analyze_and_learn(requirement)
        
        if result["status"] == "existing":
            skill = result["skill"]
            return f"🔄 发现已有技能\n\nID: {skill.id}\n名称: {skill.name}\n描述: {skill.description}\n\n可直接使用此技能执行任务。"
        
        elif result["status"] == "learned":
            skill = result["skill"]
            return f"✅ 新技能学习成功\n\nID: {skill.id}\n名称: {skill.name}\n描述: {skill.description}\n使用资料数: {result.get('materials_used', 0)}\n\n代码:\n{skill.code}"
        
        elif result["status"] == "failed":
            skill = result.get("skill")
            if skill:
                return f"⚠️ 技能生成但验证失败\n\n名称: {skill.name}\n代码:\n{skill.code}\n\n请手动修复代码问题。"
            return "❌ 技能学习失败"
        
        return "❌ 未知错误"
        
    except Exception as e:
        return f"❌ 学习失败: {str(e)}"


def load_skill(skill_id: str) -> str:
    """加载技能"""
    try:
        store = get_skill_store()
        skill = store.get(skill_id)
        
        if not skill:
            return f"❌ 未找到技能: {skill_id}"
        
        if not skill.code.strip():
            return f"❌ 技能 {skill_id} 代码为空"
        
        return f"✅ 技能已加载\n\nID: {skill.id}\n名称: {skill.name}\n描述: {skill.description}\n\n代码:\n{skill.code}"
    except Exception as e:
        return f"❌ 加载失败: {str(e)}"


def list_skills() -> str:
    """列出所有技能"""
    try:
        store = get_skill_store()
        skills = store.list_valid()
        
        if not skills:
            return "📦 当前没有已加载的技能"
        
        output = [f"📦 共有 {len(skills)} 个有效技能:\n"]
        for s in skills:
            output.append(f"\n【{s.id}】")
            output.append(f"  名称: {s.name}")
            output.append(f"  描述: {s.description}")
        
        return "\n".join(output)
    except Exception as e:
        return f"❌ 列出技能失败: {str(e)}"


def delete_skill(skill_id: str) -> str:
    """删除技能"""
    try:
        store = get_skill_store()
        success = store.delete(skill_id)
        
        if success:
            return f"✅ 技能已删除: {skill_id}"
        else:
            return f"❌ 未找到技能: {skill_id}"
    except Exception as e:
        return f"❌ 删除失败: {str(e)}"


def execute_code(code: str) -> str:
    """执行 Python 代码"""
    return run_code(code)


def execute_skill_function(skill_id: str, func_name: str = None, kwargs: dict = None) -> str:
    """执行技能中的函数"""
    try:
        store = get_skill_store()
        skill = store.get(skill_id)
        
        if not skill:
            return f"❌ 未找到技能: {skill_id}"
        
        if not skill.code.strip():
            return f"❌ 技能 {skill_id} 代码为空，无法执行"
        
        if not func_name:
            func_name = skill.name
        
        return run_function(skill.code, func_name, kwargs or {})
    except Exception as e:
        return f"❌ 执行失败: {str(e)}"


def execute_skill_by_name(name: str, kwargs: dict = None) -> str:
    """按名称执行技能"""
    try:
        store = get_skill_store()
        skill = store.get_by_name(name)
        
        if not skill:
            return f"❌ 未找到技能: {name}"
        
        if not skill.code.strip():
            return f"❌ 技能 {name} 代码为空"
        
        return run_function(skill.code, skill.name, kwargs or {})
    except Exception as e:
        return f"❌ 执行失败: {str(e)}"


def get_skill_code(skill_id: str) -> str:
    """获取技能的代码"""
    try:
        store = get_skill_store()
        skill = store.get(skill_id)
        
        if not skill:
            return f"❌ 未找到技能: {skill_id}"
        
        return skill.code
    except Exception as e:
        return f"❌ 获取代码失败: {str(e)}"
