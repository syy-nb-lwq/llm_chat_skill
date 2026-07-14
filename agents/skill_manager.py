"""Skill Manager Agent - 技能库管理系统

职责:
1. 检索技能 - 判断技能库中是否有能覆盖当前任务的技能
2. 创建技能 - 从教导或交互中创建新技能
3. 更新技能 - 基于反馈或教导更新已有技能
4. 整理技能 - 定时检查、归纳、合并重复技能
"""
from typing import List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

from core.agent_base import BaseAgent
from skills.manager import get_skill_store, Skill, SkillStore
from skills.models import SkillStep


@dataclass
class SkillMatch:
    """技能匹配结果"""
    skill: Skill
    score: float  # 0-1 匹配度
    reason: str   # 匹配原因
    coverage: str  # "full" / "partial" / "none"


@dataclass
class SkillAnalysis:
    """技能分析结果"""
    similar_skills: List[SkillMatch]  # 相似技能
    overlap_ratio: float               # 重叠程度 0-1
    suggestion: str                    # 建议
    can_merge: bool                   # 是否可以合并


class SkillRetrievalAgent(BaseAgent):
    """技能检索 Agent"""

    name = "SkillRetrieval"

    def system_prompt(self) -> str:
        return """你是一个技能检索专家，负责判断用户需求是否能被现有技能库覆盖。"""

    async def find_matching_skills(
        self, user_input: str, top_k: int = 3
    ) -> List[SkillMatch]:
        """查找最匹配当前任务的技能"""
        store = get_skill_store()
        all_skills = store.list_all()

        if not all_skills:
            return []

        # 构建比较 prompt
        skills_text = "\n".join([
            f"- {s.name}: {s.capability}"
            for s in all_skills
        ])

        prompt = f"""用户需求: {user_input}

现有技能库:
{skills_text}

请分析哪些技能能够覆盖用户需求，返回 JSON 数组:
[
  {{"skill_name": "技能名", "score": 0.0-1.0, "reason": "匹配原因", "coverage": "full/partial/none"}}
]

只返回匹配的技能(分数>0.3)，按分数从高到低排序。"""

        try:
            response = await self.think(prompt)
            return self._parse_matches(response, all_skills)
        except Exception as e:
            self.logger.error("SkillRetrieval", f"检索失败: {e}")
            return []

    def _parse_matches(self, response: str, skills: List[Skill]) -> List[SkillMatch]:
        """解析 LLM 返回的匹配结果"""
        import json
        import re

        matches = []
        skill_map = {s.name: s for s in skills}

        # 尝试提取 JSON
        json_match = re.search(r'\[.*\]', response, re.DOTALL)
        if json_match:
            try:
                items = json.loads(json_match.group())
                for item in items:
                    name = item.get("skill_name", "")
                    if name in skill_map:
                        matches.append(SkillMatch(
                            skill=skill_map[name],
                            score=float(item.get("score", 0)),
                            reason=item.get("reason", ""),
                            coverage=item.get("coverage", "none"),
                        ))
            except json.JSONDecodeError:
                pass

        return matches


class SkillCreatorAgent(BaseAgent):
    """技能创建 Agent"""

    name = "SkillCreator"

    def system_prompt(self) -> str:
        return """你是一个技能创建专家，负责从教导或描述中创建新技能。"""

    async def create_skill_from_teaching(
        self, user_input: str
    ) -> Tuple[bool, str, Optional[Skill]]:
        """从教导中创建技能"""
        from agents.skill_trainer import SkillTrainer

        trainer = SkillTrainer()
        return await trainer.teach(user_input)


class SkillUpdaterAgent(BaseAgent):
    """技能更新 Agent"""

    name = "SkillUpdater"

    def system_prompt(self) -> str:
        return """你是一个技能更新专家，负责根据反馈更新已有技能。"""

    async def update_skill(
        self, skill_name: str, feedback: str
    ) -> Tuple[bool, str, Optional[Skill]]:
        """基于反馈更新技能"""
        store = get_skill_store()
        skill = store.get_by_name(skill_name)

        if not skill:
            return False, f"技能 {skill_name} 不存在", None

        prompt = f"""技能名称: {skill.name}
当前能力: {skill.capability}
当前方法: {skill.method}

用户反馈: {feedback}

请分析反馈，生成更新后的技能信息 JSON:
{{
  "capability": "更新后的能力描述",
  "method": "更新后的方法",
  "changes": ["具体变更列表"]
}}

只输出 JSON，不要其他内容。"""

        try:
            response = await self.think(prompt)
            # 解析并更新
            import json
            import re

            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                updates = json.loads(json_match.group())

                # 更新技能
                skill.capability = updates.get("capability", skill.capability)
                skill.method = updates.get("method", skill.method)
                skill.updated_at = datetime.now().isoformat()

                # 持久化
                store.add(skill)

                msg = f"技能 {skill.name} 已更新:\n" + "\n".join(
                    f"- {c}" for c in updates.get("changes", [])
                )
                return True, msg, skill

            return False, "无法解析更新内容", None

        except Exception as e:
            self.logger.error("SkillUpdater", f"更新失败: {e}")
            return False, str(e), None


class SkillOrganizerAgent(BaseAgent):
    """技能整理 Agent - 定时清理、归纳、合并重复技能"""

    name = "SkillOrganizer"

    def system_prompt(self) -> str:
        return """你是一个技能库管理员，负责定期整理技能库，发现并处理重复或过时的技能。"""

    async def analyze_skill_duplication(self) -> List[SkillAnalysis]:
        """分析技能库中的重复技能"""
        store = get_skill_store()
        all_skills = store.list_all()

        if len(all_skills) < 2:
            return []

        analyses = []

        for i, skill1 in enumerate(all_skills):
            for skill2 in all_skills[i + 1:]:
                analysis = await self._analyze_pair(skill1, skill2)
                if analysis.overlap_ratio > 0.5:  # 重叠超过 50%
                    analyses.append(analysis)

        return analyses

    async def _analyze_pair(self, skill1: Skill, skill2: Skill) -> SkillAnalysis:
        """分析一对技能的相似度"""
        prompt = f"""分析以下两个技能的相似度和关系:

技能1:
  名称: {skill1.name}
  能力: {skill1.capability}
  方法: {skill1.method}

技能2:
  名称: {skill2.name}
  能力: {skill2.capability}
  方法: {skill2.method}

请分析并返回 JSON:
{{
  "overlap_ratio": 0.0-1.0,  // 功能重叠程度
  "suggestion": "建议(合并/保留哪个/删除哪个)",
  "can_merge": true/false,
  "reason": "分析原因"
}}"""

        try:
            response = await self.think(prompt)
            import json
            import re

            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())

                return SkillAnalysis(
                    similar_skills=[
                        SkillMatch(skill1, 1.0, "", "partial"),
                        SkillMatch(skill2, 1.0, "", "partial"),
                    ],
                    overlap_ratio=float(data.get("overlap_ratio", 0)),
                    suggestion=data.get("suggestion", ""),
                    can_merge=bool(data.get("can_merge", False)),
                )

        except Exception as e:
            self.logger.error("SkillOrganizer", f"分析失败: {e}")

        return SkillAnalysis([], 0, "", False)

    async def merge_skills(
        self, skill1_name: str, skill2_name: str
    ) -> Tuple[bool, str, Optional[Skill]]:
        """合并两个技能"""
        store = get_skill_store()
        s1 = store.get_by_name(skill1_name)
        s2 = store.get_by_name(skill2_name)

        if not s1 or not s2:
            return False, "技能不存在", None

        prompt = f"""合并以下两个技能为一个新技能:

技能1: {s1.name}
能力: {s1.capability}
方法: {s1.method}

技能2: {s2.name}
能力: {s2.capability}
方法: {s2.method}

请生成合并后的技能 JSON:
{{
  "name": "合并后的技能名",
  "capability": "合并后的能力描述",
  "method": "合并后的方法",
  "patterns": ["关键词列表"]
}}

只输出 JSON。"""

        try:
            response = await self.think(prompt)
            import json
            import re

            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())

                # 创建合并后的技能
                merged = Skill(
                    name=data.get("name", f"Merged_{s1.name}"),
                    version="1.0.0",
                    capability=data.get("capability", ""),
                    method=data.get("method", ""),
                    patterns=data.get("patterns", []),
                    tags=["merged"],
                    steps=s1.steps or s2.steps or [],
                    source="merged",
                    author="system",
                    updated_at=datetime.now().isoformat(),
                )

                # 删除原技能，添加新技能
                store.remove(skill1_name)
                store.remove(skill2_name)
                store.add(merged)

                return True, f"技能已合并: {merged.name}", merged

        except Exception as e:
            self.logger.error("SkillOrganizer", f"合并失败: {e}")
            return False, str(e), None

        return False, "合并失败", None


class SkillManagerAgent:
    """技能管理器 - 统一入口"""

    def __init__(self):
        self.retrieval = SkillRetrievalAgent()
        self.creator = SkillCreatorAgent()
        self.updater = SkillUpdaterAgent()
        self.organizer = SkillOrganizerAgent()

    async def find_skills_for_task(
        self, user_input: str
    ) -> Tuple[bool, List[SkillMatch], str]:
        """为任务找到匹配的技能

        Returns:
            (has_match, matches, response_message)
        """
        matches = await self.retrieval.find_matching_skills(user_input)

        if not matches:
            return False, [], "没有找到匹配的技能，需要创建新技能"

        # 检查是否有完全覆盖的技能
        full_coverage = [m for m in matches if m.coverage == "full"]
        if full_coverage:
            return True, full_coverage, f"找到 {len(full_coverage)} 个完全匹配的技能"

        return True, matches, f"找到 {len(matches)} 个部分匹配的技能"

    async def create_skill(
        self, user_input: str
    ) -> Tuple[bool, str, Optional[Skill]]:
        """创建新技能"""
        return await self.creator.create_skill_from_teaching(user_input)

    async def update_skill(
        self, skill_name: str, feedback: str
    ) -> Tuple[bool, str, Optional[Skill]]:
        """更新技能"""
        return await self.updater.update_skill(skill_name, feedback)

    async def organize_skills(self) -> Tuple[bool, str, List[SkillAnalysis]]:
        """整理技能库"""
        analyses = await self.organizer.analyze_skill_duplication()

        if not analyses:
            return True, "技能库状态良好，无需整理", []

        msg_parts = [f"发现 {len(analyses)} 个需要整理的情况:"]
        for a in analyses:
            skills = [s.skill.name for s in a.similar_skills]
            msg_parts.append(f"- {', '.join(skills)}: {a.suggestion}")

        return False, "\n".join(msg_parts), analyses

    async def merge_skills(
        self, skill1: str, skill2: str
    ) -> Tuple[bool, str, Optional[Skill]]:
        """合并技能"""
        return await self.organizer.merge_skills(skill1, skill2)


# 单例
_skill_manager: Optional[SkillManagerAgent] = None


def get_skill_manager() -> SkillManagerAgent:
    """获取技能管理器单例"""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManagerAgent()
    return _skill_manager
