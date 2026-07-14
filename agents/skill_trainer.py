"""SkillTrainer - 教导意图识别 + 抽取为 Skill + 沉淀"""
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from core.agent_base import BaseAgent
from skills.manager import get_skill_store, SkillStore
from skills.models import Skill, SkillStep


_TEACHING_KEYWORDS = [
    "以后", "记住", "下次", "按这个", "按我的", "原则", "方法论",
    "教你", "教你做", "教你分析", "记住这个", "步骤是", "正确做法",
    "以后都", "以后按", "以后要", "应该这样做", "学一下", "学个新技能",
]


EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "method": {"type": "string"},
        "capability": {"type": "string"},
        "patterns": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tool": {"type": "string"},
                    "params_hint": {"type": "object"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "name", "description"],
            },
        },
    },
    "required": ["name", "method", "capability", "patterns"],
}


CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "is_teaching": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["is_teaching"],
}


class SkillTrainer(BaseAgent):
    """教导闭环 Agent"""

    name = "SkillTrainer"

    def __init__(self):
        super().__init__()
        self.skill_store: SkillStore = get_skill_store()

    def system_prompt(self) -> str:
        return """你是一个技能训练助手,负责从用户的教导中抽取技能规格。"""

    def _heuristic_teaching(self, text: str) -> bool:
        return any(kw in text for kw in _TEACHING_KEYWORDS)

    async def detect(self, user_input: str) -> Tuple[bool, float, str]:
        if not self._heuristic_teaching(user_input):
            return False, 0.0, "启发式未命中"

        prompt = f"""判断以下用户输入是否在"教"系统做事(传授方法/原则/步骤)。
用户输入: {user_input}
严格输出 JSON: {{"is_teaching": true/false, "confidence": 0.0~1.0, "reason": "简短理由"}}"""
        try:
            obj = await self.think_json(prompt, CONFIRM_SCHEMA)
            return bool(obj.get("is_teaching")), float(obj.get("confidence", 0.5)), obj.get("reason", "")
        except Exception as e:
            self.logger.warning("SkillTrainer", f"LLM 确认失败: {e},降级为启发式结果")
            return self._heuristic_teaching(user_input), 0.5, "LLM 确认失败"

    async def extract_skill(self, user_input: str) -> Optional[Skill]:
        prompt = f"""从教导内容中抽取技能规格:
教导内容: {user_input}

输出 JSON:
{{"name": "简短英文名", "method": "方法论", "capability": "能力描述", "patterns": ["关键词"], "tags": ["标签"], "steps": [{{"id": "step1", "name": "步骤名", "description": "描述"}}]}}"""
        try:
            obj = await self.think_json(prompt, EXTRACT_SCHEMA)
        except Exception as e:
            self.logger.error("SkillTrainer", f"抽取失败: {e}")
            return None

        steps = []
        for i, s in enumerate(obj.get("steps") or []):
            sid = s.get("id") or f"step{i+1}"
            steps.append(SkillStep(
                id=sid,
                name=s.get("name", sid),
                description=s.get("description", ""),
                tool=s.get("tool"),
                params=s.get("params_hint", {}) or s.get("params", {}) or {},
                depends_on=s.get("depends_on", []) or [],
                retry=int(s.get("retry", 0) or 0),
                timeout_s=int(s.get("timeout_s", 30) or 30),
            ))

        existing = self.skill_store.get_by_name(obj["name"])
        if existing:
            try:
                parts = existing.version.split(".")
                major = int(parts[0])
                minor = int(parts[1]) if len(parts) > 1 else 0
                patch = int(parts[2]) if len(parts) > 2 else 0
                version = f"{major}.{minor}.{patch + 1}"
            except (ValueError, IndexError, AttributeError):
                version = "1.1.0"
        else:
            version = "1.0.0"

        return Skill(
            name=obj["name"],
            version=version,
            capability=obj.get("capability", ""),
            method=obj.get("method", ""),
            patterns=obj.get("patterns", []) or [],
            tags=obj.get("tags", []) or [],
            steps=steps,
            examples=[user_input[:200]],
            source="taught",
            author="user",
            updated_at=datetime.now().isoformat(),
        )

    def persist(self, skill: Skill) -> Tuple[bool, str]:
        try:
            base_path = self.skill_store.base_path
            target_dir = base_path / "user"
            target_dir.mkdir(parents=True, exist_ok=True)

            import yaml
            fname = f"{skill.name}@{skill.version}.yaml"
            path = target_dir / fname

            data = skill.to_dict()
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

            self.skill_store.add(skill)

            self.logger.info("SkillTrainer", f"沉淀技能: {skill.name} v{skill.version} → {path.name}")
            return True, str(path)
        except Exception as e:
            self.logger.error("SkillTrainer", f"持久化失败: {e}")
            return False, str(e)

    async def teach(self, user_input: str) -> Tuple[bool, str, Optional[Skill]]:
        is_teach, conf, reason = await self.detect(user_input)
        if not is_teach:
            return False, f"未识别为教导意图({reason})", None

        # 先尝试从输入中抽取技能信息
        skill = await self.extract_skill(user_input)
        
        # 检查是否已有相似技能（基于 capability 相似度）
        if skill:
            similar = await self._find_similar_skill(skill)
            if similar:
                # 询问用户是要更新还是创建新技能
                msg = (f"技能库中已有相似技能:\n"
                       f"**{similar.name}** (v{similar.version})\n"
                       f"能力: {similar.capability}\n\n"
                       f"你要:\n"
                       f"1. 更新现有技能\n"
                       f"2. 创建新技能\n"
                       f"3. 取消")
                return False, msg, None
        
        # 如果信息不完整，询问用户补充
        if not skill or not self._is_skill_complete(skill):
            # 构建交互式教导问题
            questions = self._generate_teach_questions(user_input, skill)
            if questions:
                # 返回交互式问题，让 Agent 询问用户
                return False, questions, None

        if not skill:
            return False, "技能抽取失败", None

        ok, path_or_err = self.persist(skill)
        if not ok:
            return False, f"保存失败: {path_or_err}", None

        msg = f"已记住新技能: **{skill.name}** v{skill.version}"
        return True, msg, skill

    def _is_skill_complete(self, skill: Skill) -> bool:
        """检查技能信息是否完整"""
        if not skill.name or len(skill.name) < 2:
            return False
        if not skill.capability or len(skill.capability) < 5:
            return False
        return True

    def _generate_teach_questions(self, user_input: str, partial_skill: Optional[Skill]) -> str:
        """生成交互式教导问题"""
        questions = []
        
        if not partial_skill or not partial_skill.name:
            questions.append("这个技能叫什么名字？（用简短的英文或中文命名）")
        elif not partial_skill.capability:
            questions.append(f"【{partial_skill.name}】是用来做什么的？")
        elif not partial_skill.steps:
            questions.append(f"【{partial_skill.name}】需要哪些步骤？请描述一下处理流程。")
        else:
            questions.append("这个技能还有什么需要注意的地方吗？直接告诉我，或者输入'没有了'结束。")
        
        return "请帮我完善这个技能的信息：\n" + "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))

    async def _find_similar_skill(self, skill: Skill) -> Optional[Skill]:
        """查找相似的技能（使用 LLM 判断语义相似度）"""
        if not skill.capability:
            return None
        
        all_skills = self.skill_store.list_all()
        for existing in all_skills:
            if not existing.capability:
                continue
            # 使用 LLM 判断是否相似
            try:
                is_similar = await self._is_capability_similar(
                    skill.capability, existing.capability
                )
                if is_similar:
                    return existing
            except Exception:
                continue
        
        return None
    
    async def _is_capability_similar(self, cap1: str, cap2: str) -> bool:
        """使用 LLM 判断两个 capability 是否相似"""
        prompt = f"""判断以下两个技能描述是否描述了相同或相似的任务:

技能1: {cap1}
技能2: {cap2}

如果它们处理的是相同或非常相似的任务类型(如都是日报处理、都是天气查询等),回答 "是"。
如果它们处理的是不同类型的任务,回答 "否"。

只回答 "是" 或 "否":"""

        try:
            response = await self.think(prompt)
            return "是" in response.strip()[:10]
        except Exception:
            return False
