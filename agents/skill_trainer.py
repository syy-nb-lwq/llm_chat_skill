"""SkillTrainer - 教导意图识别 + 抽取为 Skill + 沉淀"""
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from core.agent_base import BaseAgent
from infra.logger import LogType
from skills.manager import get_skill_store, SkillStore
from skills.models import Skill, SkillStep


# 启发式关键词:粗筛是否是"教导"
_TEACHING_KEYWORDS = [
    "以后", "记住", "下次", "按这个", "按我的", "原则", "方法论",
    "教你", "教你做", "教你分析", "记住这个", "步骤是", "正确做法",
    "以后都", "以后按", "以后要", "应该这样做", "学一下", "学个新技能",
]


# 给 LLM 用的抽取 schema
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "技能名,英文/拼音/简短中文均可,后续会作为文件名",
        },
        "method": {
            "type": "string",
            "description": "总方法论,一段文字,描述如何分析问题",
        },
        "capability": {
            "type": "string",
            "description": "能力描述:这个技能能完成什么任务",
        },
        "patterns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "触发关键词,用户输入含这些词时应使用此技能",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "技能标签,用于分类",
        },
        "steps": {
            "type": "array",
            "description": "可执行步骤(若教导中提到具体工具,可声明 tool 字段)",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "tool": {
                        "type": "string",
                        "description": "weather_query | web_search(可省略,表示纯方法论)",
                    },
                    "params_hint": {
                        "type": "object",
                        "description": "提示参数,会作为 input_schema 的 properties",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
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
    """教导闭环 Agent。

    流程:
    ┌──────────────────────────────────────────────────────────┐
    │ 1. detect()    启发式 + LLM 二次确认是否在教导              │
    │ 2. extract()   从教导文本抽取 Skill(LLM + JSON Schema)     │
    │ 3. persist()   写入 skills/user/<name>@<version>.yaml       │
    └──────────────────────────────────────────────────────────┘
    """

    name = "SkillTrainer"

    def __init__(self):
        super().__init__()
        self.skill_store: SkillStore = get_skill_store()

    # ---------- 1. 教导意图检测 ----------
    def _heuristic_teaching(self, text: str) -> bool:
        return any(kw in text for kw in _TEACHING_KEYWORDS)

    async def detect(self, user_input: str) -> Tuple[bool, float, str]:
        """返回 (是否教导, 置信度, 理由)"""
        if not self._heuristic_teaching(user_input):
            return False, 0.0, "启发式未命中"

        # LLM 二次确认,降低误判
        prompt = f"""判断以下用户输入是否在"教"系统做事(传授方法/原则/步骤),而非普通的提问。

用户输入: {user_input}

如果用户是在:
- 传授一个方法论、原则、做事步骤
- 让系统"记住"或"以后"按某种方式处理
- 教授一个新技能

则 is_teaching=true,否则 false。

严格输出 JSON:
{{"is_teaching": true/false, "confidence": 0.0~1.0, "reason": "简短理由"}}"""
        try:
            obj = await self.think_json(prompt, CONFIRM_SCHEMA)
            return bool(obj.get("is_teaching")), float(obj.get("confidence", 0.5)), \
                   obj.get("reason", "")
        except Exception as e:
            self.logger.warning(LogType.FLOW_STEP, "SkillTrainer",
                                f"LLM 确认失败: {e},降级为启发式结果")
            return self._heuristic_teaching(user_input), 0.5, "LLM 确认失败"

    # ---------- 2. 抽取 Skill ----------
    async def extract_skill(self, user_input: str) -> Optional[Skill]:
        """从教导文本抽取 Skill 结构(不持久化)"""
        self.logger.log_flow("SkillTrainer", f"开始抽取技能: {user_input[:60]}")

        prompt = f"""从以下"教导内容"中抽取一个可复用的技能规格。

教导内容: {user_input}

要求:
- name: 简短英文或拼音(用于文件名),例如 "data_summary" / "travel_plan"
- method: 一段话描述如何分析问题
- capability: 一句话说清这个技能能做什么
- patterns: 3~5 个触发关键词,用户输入包含这些词时应使用本技能
- tags: 1~3 个分类标签
- steps: 若教导中提到具体可执行步骤(尤其涉及工具调用),列出;否则只列 1 个 "summarize" 步骤
  - id 必须唯一(英文+数字)
  - 涉及工具时填 tool(只能是 weather_query / web_search)
  - 涉及参数时填 params_hint(例: {{"query": "搜索词"}})
  - depends_on 列出依赖的其它 step id(无依赖填空数组)

严格按 JSON 输出。"""
        try:
            obj = await self.think_json(prompt, EXTRACT_SCHEMA)
        except Exception as e:
            self.logger.error(LogType.LLM_ERROR, "SkillTrainer", f"抽取失败: {e}")
            return None

        # 构造 Skill
        steps: list = []
        for i, s in enumerate(obj.get("steps") or []):
            sid = s.get("id") or f"step{i+1}"
            input_schema = {}
            if s.get("params_hint"):
                input_schema = {
                    "type": "object",
                    "properties": s["params_hint"],
                }
            steps.append(SkillStep(
                id=sid,
                name=s.get("name", sid),
                description=s.get("description", ""),
                tool=s.get("tool"),
                input_schema=input_schema,
                depends_on=s.get("depends_on", []) or [],
            ))

        # version 处理:同名技能递增
        existing = self.skill_store.get_by_name(obj["name"])
        if existing:
            try:
                major, minor, patch = [int(x) for x in existing.version.split(".")]
                version = f"{major}.{minor}.{patch + 1}"
            except Exception:
                version = "1.1.0"
        else:
            version = "1.0.0"

        skill = Skill(
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
        return skill

    # ---------- 3. 持久化 ----------
    def persist(self, skill: Skill) -> Tuple[bool, str]:
        """写入 skills/user/<name>@<version>.yaml

        Returns: (success, message)
        """
        try:
            base_path = self.skill_store.base_path
            target_dir = base_path / "user"
            target_dir.mkdir(parents=True, exist_ok=True)

            import yaml
            fname = f"{skill.name}@{skill.version}.yaml"
            path = target_dir / fname

            data = skill.to_dict()
            data["updated_at"] = skill.updated_at

            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            # 加入 registry(下次匹配就能用上)
            self.skill_store._registry.add(skill)
            self.skill_store._skills = self.skill_store._registry._by_name

            self.logger.info(LogType.FLOW_STEP, "SkillTrainer",
                             f"沉淀技能: {skill.name} v{skill.version} → {path.name}")
            return True, str(path)
        except Exception as e:
            self.logger.error(LogType.FLOW_STEP, "SkillTrainer", f"持久化失败: {e}")
            return False, str(e)

    # ---------- 4. 一站式 ----------
    async def teach(self, user_input: str) -> Tuple[bool, str, Optional[Skill]]:
        """detect → extract → persist,返回 (是否成功, 消息, Skill)"""
        is_teach, conf, reason = await self.detect(user_input)
        if not is_teach:
            return False, f"未识别为教导意图({reason})", None

        skill = await self.extract_skill(user_input)
        if not skill:
            return False, "技能抽取失败", None

        ok, path_or_err = self.persist(skill)
        if not ok:
            return False, f"保存失败: {path_or_err}", None

        msg = f"已记住新技能: **{skill.name}** v{skill.version}\n" \
              f"能力: {skill.capability}\n" \
              f"触发词: {', '.join(skill.patterns)}\n" \
              f"步骤数: {len(skill.steps)}"
        return True, msg, skill