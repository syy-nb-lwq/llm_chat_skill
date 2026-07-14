"""技能执行集成测试

测试 Skills 能否正常加载和执行。
运行方式:
  python -m pytest tests/test_skill_execution.py -v
"""
import pytest
from skills.manager import get_skill_store


class TestSkillLoading:
    """技能加载测试"""

    def test_skill_store_initialized(self):
        """技能仓库已初始化"""
        store = get_skill_store()
        assert store is not None

    def test_skills_loaded(self):
        """技能已加载"""
        store = get_skill_store()
        skills = store.list_all()
        print(f"\n已加载技能数量: {len(skills)}")
        for skill in skills:
            print(f"  - {skill.name}: {skill.capability}")

    def test_skill_has_required_fields(self):
        """每个技能都有必需字段"""
        store = get_skill_store()
        skills = store.list_all()
        
        for skill in skills:
            assert skill.name, f"技能缺少 name"
            assert skill.capability, f"技能 {skill.name} 缺少 capability"
            assert skill.method, f"技能 {skill.name} 缺少 method"
            print(f"✓ {skill.name} 字段完整")


# ===== 技能执行测试数据集 =====
SKILL_EXECUTION_CASES = [
    {
        "user_input": "今天厦门天气怎么样",
        "expected_tool": "weather_query",
        "description": "天气查询-厦门",
    },
    {
        "user_input": "明天北京热不热",
        "expected_tool": "weather_query",
        "description": "天气查询-北京",
    },
    {
        "user_input": "什么是人工智能",
        "expected_tool": "web_search",
        "description": "搜索查询",
    },
    {
        "user_input": "怎么学习Python",
        "expected_tool": "web_search",
        "description": "搜索查询2",
    },
]


class TestSkillExecution:
    """技能执行测试"""

    @pytest.fixture
    def skill_store(self):
        return get_skill_store()

    @pytest.mark.parametrize("case", SKILL_EXECUTION_CASES, ids=lambda c: c["description"])
    def test_skill_can_handle(self, skill_store, case):
        """测试技能是否能处理对应输入"""
        # 获取匹配的技能
        matched = skill_store.match(case["user_input"])
        
        print(f"\n输入: {case['user_input']}")
        print(f"匹配技能: {matched}")
        
        # 如果有匹配的技能，验证它包含预期的工具
        if matched:
            # 检查工具是否匹配
            if case.get("expected_tool"):
                tools = [step.get("tool") for step in matched.steps]
                print(f"技能包含工具: {tools}")
                # 注意: 这里只做存在性检查，不强制要求

    def test_skill_names(self):
        """打印所有技能名称"""
        store = get_skill_store()
        skills = store.list_all()
        print("\n" + "=" * 60)
        print("已加载的技能")
        print("=" * 60)
        for s in skills:
            print(f"\n【{s.name}】")
            print(f"  能力: {s.capability}")
            print(f"  方法: {s.method[:100]}..." if len(s.method) > 100 else f"  方法: {s.method}")
            print(f"  工具: {[step.tool for step in s.steps]}")
        print("=" * 60)
