"""SkillTrainer - 教导意图识别 + 抽取为 Skill + 沉淀(支持多轮状态,M1-01/M1-06)

变更:
- 引入 TeachingSession 状态机,可在多轮对话中补全 name/method/capability
- 不再调用 Agent 上不存在的 self.llm(M1-07)
- 重复技能决策:reuse / update_new / cancel
- 草稿生成走 validate 流水线(M1-05)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.agent_base import BaseAgent
from skills.manager import get_skill_store, SkillStore
from skills.models import Skill, SkillStep
from skills.validator import validate_skill

from agents.teaching_session import (
    REQUIRED_FIELDS,
    TeachingSession,
    TeachingSessionStore,
    TeachingStatus,
    get_teaching_store,
)


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
    """教导闭环 Agent(支持多轮)"""

    name = "SkillTrainer"

    def __init__(self, teaching_store: Optional[TeachingSessionStore] = None):
        super().__init__()
        self.skill_store: SkillStore = get_skill_store()
        self.teaching_store = teaching_store or get_teaching_store()

    def system_prompt(self) -> str:
        return """你是一个技能训练助手,负责从用户的教导中抽取技能规格。"""

    # ===== 入口 =====

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

    async def start_or_continue(
        self,
        user_input: str,
        user_id: str = "default",
        session_id: str = "default",
    ) -> TeachingSession:
        """处理一轮用户输入,推进教学状态机。

        返回最新的 TeachingSession 状态(已保存)。
        """
        # 1) 找当前活跃教学会话
        ts = self.teaching_store.find_active_for(user_id, session_id)
        if ts is None:
            ts = TeachingSession.new(user_id=user_id, session_id=session_id)

        # 2) 记录本轮证据
        ts.evidence_turns.append({"role": "user", "content": user_input})
        ts.touch()

        # 3) 处理重复技能决策(用户输入以特殊前缀选择)
        choice = self._parse_user_choice(user_input)
        if choice and ts.duplicate_of:
            ts.user_choice = choice
            if choice == "cancel":
                ts.status = TeachingStatus.CANCELLED
                self.teaching_store.save(ts)
                return ts
            if choice == "reuse":
                # 直接复用已有技能:状态设为 active 但不再创建新版本
                ts.status = TeachingStatus.ACTIVE
                self.teaching_store.save(ts)
                return ts
            # update_new: 继续走到 draft 流程,使用新版本
            ts.user_choice = "update_new"

        # 4) 用 LLM 抽取增量字段
        try:
            extracted = await self.extract_skill(user_input)
        except Exception as e:
            self.logger.error("SkillTrainer", f"抽取失败: {e}")
            extracted = None

        if extracted:
            # merge 到 partial_skill(只覆盖 LLM 真实给出的字段)
            for f in ("name", "method", "capability"):
                v = getattr(extracted, f, "")
                if v and (f not in ts.partial_skill or not ts.partial_skill.get(f)):
                    ts.partial_skill[f] = v
            for f in ("patterns", "tags"):
                vals = getattr(extracted, f, None) or []
                if vals:
                    merged = list(ts.partial_skill.get(f, []) or [])
                    for v in vals:
                        if v not in merged:
                            merged.append(v)
                    ts.partial_skill[f] = merged
            if extracted.steps:
                ts.partial_skill.setdefault("steps", [])
                existing_ids = {s.get("id") for s in ts.partial_skill["steps"]}
                for s in extracted.steps:
                    if s.id in existing_ids:
                        continue
                    ts.partial_skill["steps"].append({
                        "id": s.id,
                        "name": s.name,
                        "description": s.description,
                        "tool": s.tool,
                        "params": s.params,
                        "depends_on": s.depends_on,
                    })
                    existing_ids.add(s.id)

        # 5) 算 missing_fields
        ts.missing_fields = [
            f for f in REQUIRED_FIELDS
            if not (ts.partial_skill.get(f) or "").strip()
        ]

        # 6) 检测重复
        name = ts.partial_skill.get("name") or ""
        if name and not ts.duplicate_of:
            existing = self.skill_store.get_by_name(name)
            if existing:
                ts.duplicate_of = existing.name

        # 7) 决定状态
        if ts.missing_fields:
            ts.status = TeachingStatus.COLLECTING
            ts.current_question = self._next_question(ts)
        else:
            # 信息完整,生成草稿并校验
            ts.draft_skill = dict(ts.partial_skill)
            skill_obj = self._build_skill_obj(ts)
            issues = validate_skill(skill_obj, tool_names=self.skill_store.list_tool_names())
            if issues:
                ts.status = TeachingStatus.COLLECTING
                ts.current_question = "草稿校验未通过:\n" + "\n".join(f"- {i}" for i in issues)
            else:
                ts.status = TeachingStatus.DRAFT
                ts.current_question = self._render_draft(ts)

        self.teaching_store.save(ts)
        return ts

    def cancel(self, user_id: str, session_id: str) -> bool:
        ts = self.teaching_store.find_active_for(user_id, session_id)
        if not ts:
            return False
        ts.status = TeachingStatus.CANCELLED
        self.teaching_store.save(ts)
        return True

    def confirm_and_publish(self, user_id: str, session_id: str) -> Tuple[bool, str, Optional[Skill]]:
        """用户在草稿阶段确认 → 校验 → 写盘 → 注册。"""
        ts = self.teaching_store.find_active_for(user_id, session_id)
        if not ts or ts.status not in (TeachingStatus.DRAFT,):
            return False, "当前没有可发布的草稿", None
        if not ts.draft_skill:
            return False, "草稿为空", None

        skill = self._build_skill_obj(ts)
        # 决定版本:
        # - 如果是 update_new 模式且同名,版本号 +0.0.1
        if ts.duplicate_of and ts.user_choice == "update_new":
            existing = self.skill_store.get_by_name(ts.duplicate_of)
            skill.version = self._bump_version(existing.version if existing else "1.0.0")
        else:
            existing = self.skill_store.get_by_name(skill.name)
            if existing and existing.version == skill.version:
                skill.version = self._bump_version(existing.version)

        issues = validate_skill(skill, tool_names=self.skill_store.list_tool_names())
        if issues:
            return False, "发布前校验失败:\n" + "\n".join(f"- {i}" for i in issues), None

        ok, path_or_err = self.persist(skill)
        if not ok:
            return False, f"保存失败: {path_or_err}", None

        ts.status = TeachingStatus.ACTIVE
        self.teaching_store.save(ts)
        msg = f"已发布技能: **{skill.name}** v{skill.version}"
        return True, msg, skill

    # ===== helpers =====

    def _parse_user_choice(self, user_input: str) -> Optional[str]:
        u = user_input.strip().lower()
        if u in ("1", "reuse", "复用", "用现有的", "用已有"):
            return "reuse"
        if u in ("2", "update_new", "新建版本", "创建新版本", "新版本"):
            return "update_new"
        if u in ("3", "cancel", "取消"):
            return "cancel"
        return None

    def _next_question(self, ts: TeachingSession) -> str:
        f = ts.missing_fields[0]
        prompts = {
            "name": "请给这个技能起个名字(英文短名或简短中文),例如 `DailyReport`。",
            "method": "请描述这个技能的处理方法论/步骤(如果分步,可用 1) 2) 3)。",
            "capability": "请用一句话描述这个技能能做什么(供后续检索匹配)。",
        }
        return prompts.get(f, f"请补充字段: {f}")

    def _render_draft(self, ts: TeachingSession) -> str:
        d = ts.draft_skill or {}
        lines = [
            "已收集到完整信息,草稿如下:",
            f"**名称**: {d.get('name','')}",
            f"**能力**: {d.get('capability','')}",
            f"**方法**: {(d.get('method','') or '')[:200]}",
            f"**关键词**: {', '.join(d.get('patterns', []) or []) or '(无)'}",
            f"**步骤**: {len(d.get('steps', []) or [])} 条",
            "",
            "请确认(回复「确认」/「确认发布」)或继续补充修改。",
        ]
        return "\n".join(lines)

    def _build_skill_obj(self, ts: TeachingSession) -> Skill:
        d = ts.draft_skill or {}
        steps_raw = d.get("steps", []) or []
        steps = [
            SkillStep(
                id=s.get("id") or f"step{i+1}",
                name=s.get("name", ""),
                description=s.get("description", ""),
                tool=s.get("tool"),
                params=s.get("params", {}) or {},
                depends_on=s.get("depends_on", []) or [],
            )
            for i, s in enumerate(steps_raw)
        ]
        return Skill(
            name=d.get("name", "").strip(),
            version=d.get("version") or "1.0.0",
            capability=d.get("capability", "").strip(),
            method=d.get("method", "").strip(),
            patterns=d.get("patterns", []) or [],
            tags=d.get("tags", []) or [],
            steps=steps,
            examples=[t["content"][:200] for t in ts.evidence_turns if t.get("role") == "user"][:3],
            source="taught",
            author=ts.user_id,
            updated_at=datetime.now().isoformat(),
        )

    def _bump_version(self, version: str) -> str:
        try:
            parts = version.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            patch = int(parts[2]) if len(parts) > 2 else 0
            return f"{major}.{minor}.{patch + 1}"
        except Exception:
            return "1.0.1"

    # ===== 兼容旧 API =====

    async def extract_skill(self, user_input: str) -> Optional[Skill]:
        """从单轮输入抽取一个 Skill(不写盘,供 start_or_continue 内部使用)。"""
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
            ))
        return Skill(
            name=obj.get("name", "").strip(),
            version="1.0.0",
            capability=obj.get("capability", ""),
            method=obj.get("method", ""),
            patterns=obj.get("patterns", []) or [],
            tags=obj.get("tags", []) or [],
            steps=steps,
            examples=[user_input[:200]],
            source="taught",
            author="user",
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
            data["active"] = True  # 新发布的版本默认就是 active
            path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

            self.skill_store.add(skill, set_active=True)
            self.skill_store.reload()

            self.logger.info("SkillTrainer", f"沉淀技能: {skill.name} v{skill.version} → {path.name}")
            return True, str(path)
        except Exception as e:
            self.logger.error("SkillTrainer", f"持久化失败: {e}")
            return False, str(e)

    async def teach(self, user_input: str, user_id: str = "default",
                    session_id: str = "default") -> Tuple[bool, str, Optional[Skill]]:
        """兼容旧单轮 teach()。

        - 走 TeachingSession 状态机
        - 若抽取信息完整且无重复 → 自动 confirm_and_publish
        - 否则返回 mid-state
        """
        ts = await self.start_or_continue(
            user_input, user_id=user_id, session_id=session_id,
        )
        if ts.status == TeachingStatus.DRAFT:
            # 单轮已完整 → 自动确认发布
            ok, msg, skill = self.confirm_and_publish(user_id, session_id)
            if ok and skill is not None:
                return True, msg, skill
            return False, msg, None
        if ts.status == TeachingStatus.ACTIVE:
            return True, ts.current_question or "已完成", None
        return False, ts.current_question, None
