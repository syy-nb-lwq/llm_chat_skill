"""SkillMerger - 技能版本合并:当同一 Skill 有多个版本时,LLM 判断哪个更好或合并为新版本"""
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from skills.models import Skill, SkillStep
from core.memory import get_memory_store


@dataclass
class MergeResult:
    """合并结果"""
    success: bool
    merged_skill: Optional[Skill] = None
    reason: str = ""
    kept_versions: List[str] = field(default_factory=list)


# ---- Feature Flag ----
SELF_EVOLUTION_ENABLED = False


def _load_flag():
    try:
        from infra.config import config
        return bool(config.self_evolution_enabled)
    except Exception:
        return False


def get_self_evolution_enabled() -> bool:
    return _load_flag()


class SkillMerger:
    """技能版本合并器

    职责:
    1. 当同一 Skill 有多个版本时,LLM 判断哪个更好或合并为新版本
    2. 支持手动触发合并(用户请求"优化 xxx 技能")
    3. 自动触发合并(同一 Skill 有 3+ 版本时)

    合并流程:
    v1.0.0 (手工)  ─┐
                       ├─▶ LLM 评估 ─▶ v1.2.0 (合并版,标记 source="merged")
    v1.1.0 (Teach) ─┘
    """

    def __init__(self):
        self.logger = get_logger()
        self._llm_client = None

    def _get_llm_client(self):
        if self._llm_client is None:
            try:
                from infra.llm import get_llm_client
                self._llm_client = get_llm_client()
            except Exception as e:
                self.logger.warning("SkillMerger", f"无法初始化 LLM: {e}")
        return self._llm_client

    async def merge(
        self,
        skill_name: str,
        versions: List[Skill],
    ) -> MergeResult:
        """合并同一技能多个版本

        Args:
            skill_name: 技能名称
            versions: 该技能的所有版本列表

        Returns:
            MergeResult: 合并结果
        """
        if not get_self_evolution_enabled():
            return MergeResult(
                success=False,
                reason="self_evolution_disabled",
            )

        if len(versions) < 2:
            return MergeResult(
                success=False,
                reason="需要至少 2 个版本才能合并",
            )

        self.logger.info("SkillMerger", f"开始合并技能: {skill_name}, 版本数: {len(versions)}")

        # 构建 prompt
        prompt = self._build_merge_prompt(skill_name, versions)

        # 调用 LLM
        llm = self._get_llm_client()
        if llm is None:
            return MergeResult(
                success=False,
                reason="LLM 不可用",
            )

        try:
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": "你是一个技能融合专家。根据多个版本的技能,生成一个融合版本。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )

            # 解析 JSON
            merged_skill = self._parse_merge_response(skill_name, response, versions)
            if merged_skill is None:
                return MergeResult(
                    success=False,
                    reason="LLM 输出无法解析",
                )

            self.logger.info("SkillMerger", f"合并成功: {skill_name} -> v{merged_skill.version}")
            return MergeResult(
                success=True,
                merged_skill=merged_skill,
                reason="合并成功",
                kept_versions=[v.version for v in versions],
            )

        except Exception as e:
            self.logger.error("SkillMerger", f"合并失败: {e}")
            return MergeResult(
                success=False,
                reason=str(e),
            )

    def _build_merge_prompt(self, skill_name: str, versions: List[Skill]) -> str:
        """构建合并 prompt"""
        lines = [
            f"技能名称: {skill_name}",
            f"版本数量: {len(versions)}",
            "",
        ]

        for i, v in enumerate(versions):
            lines.append(f"--- 版本 {i+1} (v{v.version}, 来源: {v.source}) ---")
            lines.append(f"能力: {v.capability}")
            lines.append(f"方法论: {v.method}")
            if v.patterns:
                lines.append(f"触发词: {', '.join(v.patterns)}")
            if v.steps:
                lines.append(f"步骤数: {len(v.steps)}")
                for step in v.steps[:3]:  # 最多 3 个步骤
                    lines.append(f"  - {step.name}: {step.description}")
            lines.append("")

        lines.append("请分析每个版本的优劣,生成一个融合版本。输出 JSON 格式:")
        lines.append("""{
  "version": "1.2.0",
  "method": "融合后的方法论",
  "patterns": ["pattern1", "pattern2"],
  "steps": [
    {"id": "step1", "name": "步骤1", "description": "...", "tool": "xxx"}
  ]
}""")

        return "\n".join(lines)

    def _parse_merge_response(
        self,
        skill_name: str,
        response: str,
        versions: List[Skill],
    ) -> Optional[Skill]:
        """解析 LLM 合并响应"""
        import re

        # 提取 JSON
        m = re.search(r"\{[\s\S]*\}", response)
        if not m:
            return None

        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return None

        # 找最新版本号,递增
        latest_version = max(v.version for v in versions)
        try:
            major, minor, patch = latest_version.split(".")
            new_version = f"{major}.{minor}.{int(patch) + 1}"
        except Exception:
            new_version = "1.0.0"

        # 构建合并后的 Skill
        merged = Skill(
            name=skill_name,
            version=new_version,
            capability=versions[0].capability,
            method=data.get("method", versions[0].method),
            patterns=data.get("patterns", versions[0].patterns),
            tags=list(set().union(*[set(v.tags) for v in versions])),
            steps=self._parse_steps(data.get("steps", [])),
            examples=list(set().union(*[set(v.examples) for v in versions])),
            source="merged",
            author="SkillMerger",
        )

        return merged

    def _parse_steps(self, steps_data: List[Dict]) -> List[SkillStep]:
        """解析步骤数据"""
        steps = []
        for i, s in enumerate(steps_data):
            step = SkillStep(
                id=s.get("id", f"step_{i+1}"),
                name=s.get("name", f"步骤{i+1}"),
                description=s.get("description", ""),
                tool=s.get("tool"),
                params=s.get("params", {}),
                depends_on=s.get("depends_on", []),
                retry=s.get("retry", 0),
                timeout_s=s.get("timeout_s", 30),
            )
            steps.append(step)
        return steps

    async def suggest_merge(
        self,
        skill_name: str,
        versions: List[Skill],
    ) -> Optional[str]:
        """建议合并(但不执行),返回 LLM 的分析和建议

        用于前端展示,让用户确认后再执行合并。
        """
        if not get_self_evolution_enabled():
            return None

        if len(versions) < 2:
            return None

        llm = self._get_llm_client()
        if llm is None:
            return None

        prompt = self._build_merge_prompt(skill_name, versions)

        try:
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": "你是一个技能优化专家。分析以下技能版本,给出优化建议。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            return response
        except Exception as e:
            self.logger.warning("SkillMerger", f"suggest_merge 失败: {e}")
            return None


def merge_skills(
    skill_name: str,
    versions: List[Skill],
) -> MergeResult:
    """便捷函数:同步合并技能(内部使用 asyncio.run)"""
    import asyncio
    merger = SkillMerger()
    try:
        return asyncio.run(merger.merge(skill_name, versions))
    except Exception:
        return MergeResult(success=False, reason="合并失败")
