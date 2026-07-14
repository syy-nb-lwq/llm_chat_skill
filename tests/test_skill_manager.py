"""Skill Manager 测试"""
import pytest
from agents.skill_manager import (
    SkillManagerAgent, get_skill_manager,
    SkillRetrievalAgent, SkillOrganizerAgent
)


class TestSkillManager:
    """技能管理器测试"""

    @pytest.fixture
    def manager(self):
        return get_skill_manager()

    def test_singleton(self):
        """单例测试"""
        m1 = get_skill_manager()
        m2 = get_skill_manager()
        assert m1 is m2

    def test_sub_agents(self):
        """子 Agent 测试"""
        manager = get_skill_manager()
        assert manager.retrieval is not None
        assert manager.creator is not None
        assert manager.updater is not None
        assert manager.organizer is not None


class TestSkillRetrieval:
    """技能检索测试"""

    @pytest.fixture
    def retrieval(self):
        return SkillRetrievalAgent()

    @pytest.mark.asyncio
    async def test_find_matching_skills(self, retrieval):
        """查找匹配技能"""
        results = await retrieval.find_matching_skills("查询天气")
        print(f"\n找到 {len(results)} 个匹配技能")
        for r in results:
            print(f"  - {r.skill.name}: {r.score} ({r.coverage})")


class TestSkillOrganizer:
    """技能整理测试"""

    @pytest.fixture
    def organizer(self):
        return SkillOrganizerAgent()

    @pytest.mark.asyncio
    async def test_analyze_duplication(self, organizer):
        """分析重复技能"""
        analyses = await organizer.analyze_skill_duplication()
        print(f"\n发现 {len(analyses)} 个重复情况")
        for a in analyses:
            print(f"  - 重叠度: {a.overlap_ratio}, 建议: {a.suggestion}")
