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
                depends_on=s.get("depends_on", []) or [],
            ))

        existing = self.skill_store.get_by_name(obj["name"])
        if existing:
            try:
                major, minor, patch = [int(x) for x in existing.version.split(".")]
                version = f"{major}.{minor}.{patch + 1}"
            except Exception:
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

            self.skill_store._registry.add(skill)
            self.skill_store._skills = self.skill_store._registry._by_name

            self.logger.info("SkillTrainer", f"沉淀技能: {skill.name} v{skill.version} → {path.name}")
            return True, str(path)
        except Exception as e:
            self.logger.error("SkillTrainer", f"持久化失败: {e}")
            return False, str(e)

    async def teach(self, user_input: str) -> Tuple[bool, str, Optional[Skill]]:
        is_teach, conf, reason = await self.detect(user_input)
        if not is_teach:
            return False, f"未识别为教导意图({reason})", None

        skill = await self.extract_skill(user_input)
        if not skill:
            return False, "技能抽取失败", None

        ok, path_or_err = self.persist(skill)
        if not ok:
            return False, f"保存失败: {path_or_err}", None

        msg = f"已记住新技能: **{skill.name}** v{skill.version}"
        return True, msg, skill
