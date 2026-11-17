"""方法论自增长 —— 把一次**做成了**的多步任务，提炼成可复用的技能。

## 为什么要有这个

用户对系统的定位是四根柱子：

    使用者的习惯（长期记忆） + 短期记忆 + **方法论（skill）** + 工具（tool/MCP） -> 完成任务

其中「工具自增长」已经做了（`create_tool`），但**方法论只能靠人教**
（`SkillTrainer`，要用户说"教我…"）。

于是出现一个不对称：agent 会给自己造工具，却不会给自己攒方法论。
每做成一件新事，经验就丢一次 —— 下次遇到同类任务，还得从零规划。

**这个模块补的就是这条回路：做成了 → 沉淀成方法论。**

## 三条防线（和工具自增长同一个原则：不乱造）

垃圾技能比没有技能更糟 —— 它会污染检索、占用注入预算、还可能被误选。
所以：

1. **只从成功里学**：失败的轨迹没有复用价值
2. **只从"有实质过程"里学**：至少用到 2 个不同工具，或跑了 2 轮以上。
   一问一答（"算 1+1"）不配叫方法论
3. **默认不激活**：提炼出来的技能先存成**非激活版本**，
   人批准后才进 planner 的候选集。宁可不生效，也不让垃圾技能混进检索

## 顺手做对的一件事：回归样例来自真实成功

技能要能被验证（`ResultValidator` 跑 `Skill.regression`），而回归样例最难写。
这里直接把**这次真实成功的那一轮**转成样例：

    {"name": "...", "input": {"user_input": <这次的原话>},
     "expect": {"tool": <这次真的用了的工具>, "success": true}}

所以提炼出来的技能**一出生就带验证**，不是空壳。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from skills.models import Skill, SkillStep


#: 至少用到这么多个**不同**工具，才值得当方法论
MIN_DISTINCT_TOOLS = 2
#: 或者至少跑这么多轮
MIN_ROUNDS = 2
#: method 正文长度下限（太短说明没提炼出东西）
MIN_METHOD_CHARS = 40
#: patterns 数量上限
MAX_PATTERNS = 6


HARVEST_PROMPT = """你在总结一次**成功完成**的任务，把它提炼成可复用的方法论。

## 这次任务
用户要求：{user_input}

## 实际执行过程（按顺序）
{trajectory}

## 提炼要求

写一套**下次遇到同类任务时能照着做**的方法论，要求：

1. `capability`：一句话说清"这套方法能解决什么问题"（不超过 40 字）
2. `method`：分步骤叙述怎么做（"第一步…；第二步…"），
   **必须基于上面真实的执行过程**，不要编造没发生过的步骤
3. `patterns`：3~6 个用户可能会说的**触发短语**（比如"做日报"、"排障"），
   用于以后匹配同类请求
4. `name`：简短的中文名（4~12 字，能概括这套方法）

**不要**把具体的一次性数据（城市名、数字、链接）写进 method ——
方法论要能复用，不是复述这一次。

只输出一个 JSON 对象：
{{"name": "...", "capability": "...", "method": "...", "patterns": ["...", "..."]}}
"""


class HarvestResult:
    """提炼结果（成功/跳过/失败都能说清原因）。"""

    __slots__ = ("skill", "skipped", "reason", "draft_id")

    def __init__(
        self,
        skill: Optional[Skill] = None,
        skipped: bool = False,
        reason: str = "",
        draft_id: str = "",
    ):
        self.skill = skill
        self.skipped = skipped
        self.reason = reason
        self.draft_id = draft_id

    def __bool__(self) -> bool:
        return self.skill is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "harvested": self.skill is not None,
            "skipped": self.skipped,
            "reason": self.reason,
            "draft_id": self.draft_id,
            "skill": self.skill.name if self.skill else None,
        }


# ============================================================
# 草稿队列
# ============================================================
#
# 为什么不直接写进技能库：`SkillStore.list_all()` 只返回**激活版本**，
# 一个 `set_active=False` 的技能存进去就查不出来了（只能靠 id 取）。
# 那样"待批准的技能"在界面上根本看不见，等于黑盒。
#
# 所以用一个独立的草稿队列（和工具提案的 `/api/tool-proposals` 同一个模式）：
# 提炼出来 → 进草稿 → 人批准 → 才 `store.add(set_active=True)` 进技能库。

class SkillDraftStore:
    """技能草稿的持久化（一个草稿一个 JSON 文件）。"""

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory" / "skills" / "drafts"
        self.base = Path(base_path)
        self.logger = get_logger()

    def _ensure(self) -> None:
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.logger.warning("SkillDraft", f"建目录失败 {self.base}: {exc}")

    def _path(self, draft_id: str) -> Path:
        return self.base / f"{draft_id}.json"

    def save(self, skill: Skill, *, origin: str = "") -> str:
        self._ensure()
        path = self._path(skill.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"origin": origin, "skill": asdict(skill)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.error("SkillDraft", f"保存草稿失败 {path}: {exc}")
        return skill.id

    def get(self, draft_id: str) -> Optional[Skill]:
        path = self._path(draft_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return _skill_from_dict(payload.get("skill") or {})
        except Exception as exc:
            self.logger.warning("SkillDraft", f"读取草稿失败 {path}: {exc}")
            return None

    def list(self) -> List[Dict[str, Any]]:
        if not self.base.exists():
            return []
        out: List[Dict[str, Any]] = []
        for path in sorted(self.base.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                raw = payload.get("skill") or {}
                out.append({
                    "id": raw.get("id"),
                    "name": raw.get("name"),
                    "capability": raw.get("capability"),
                    "method": raw.get("method"),
                    "patterns": raw.get("patterns") or [],
                    "steps": [
                        {"tool": s.get("tool"), "name": s.get("name")}
                        for s in (raw.get("steps") or [])
                    ],
                    "examples": raw.get("examples") or [],
                    "origin": payload.get("origin") or "",
                    "created_at": raw.get("created_at") or "",
                })
            except Exception:
                continue
        return out

    def delete(self, draft_id: str) -> bool:
        path = self._path(draft_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except Exception:
            return False


def _skill_from_dict(data: Dict[str, Any]) -> Optional[Skill]:
    if not data:
        return None
    steps = [
        SkillStep(**{k: v for k, v in s.items() if k in SkillStep.__dataclass_fields__})
        for s in (data.get("steps") or [])
        if isinstance(s, dict)
    ]
    known = set(Skill.__dataclass_fields__)
    payload = {k: v for k, v in data.items() if k in known}
    payload["steps"] = steps
    try:
        return Skill(**payload)
    except Exception:
        return None


_draft_store: Optional[SkillDraftStore] = None


def get_draft_store() -> SkillDraftStore:
    global _draft_store
    if _draft_store is None:
        _draft_store = SkillDraftStore()
    return _draft_store


def reset_draft_store() -> None:
    global _draft_store
    _draft_store = None


def approve_draft(draft_id: str, store=None) -> tuple:
    """批准一个草稿 → 进技能库并**激活**（planner 立刻能看到）。"""
    drafts = get_draft_store()
    skill = drafts.get(draft_id)
    if skill is None:
        return False, f"草稿不存在: {draft_id}"

    if store is None:
        from skills.manager import get_skill_store

        store = get_skill_store()

    existing = store.get_by_name(skill.name)
    if existing is not None:
        return False, f"已有同名技能 {skill.name}，请改名或走技能升级流程"

    try:
        store.add(skill, set_active=True)
    except Exception as exc:
        return False, f"写入技能库失败: {exc}"

    drafts.delete(draft_id)
    return True, f"技能 {skill.name} 已启用"


def _tool_sequence(tool_results: Dict[str, Any], task_specs: Optional[Dict[str, Any]] = None) -> List[str]:
    """本次用到的工具名（按出现顺序，去重）。"""
    seq: List[str] = []
    specs = task_specs or {}
    for tid in (tool_results or {}):
        ttype = ""
        spec = specs.get(tid) or specs.get(str(tid).split("#")[0])
        if isinstance(spec, dict):
            ttype = str(spec.get("type") or "")
        if not ttype:
            ttype = str(tid)
        if ttype and ttype not in seq:
            seq.append(ttype)
    return seq


def _build_trajectory(tool_results: Dict[str, Any], task_specs: Optional[Dict[str, Any]] = None) -> str:
    """把执行过程压成给 LLM 看的描述。

    **只写工具名和参数键，不写参数值** —— 参数值里可能有用户隐私数据。
    """
    lines = []
    specs = task_specs or {}
    for i, (tid, res) in enumerate((tool_results or {}).items(), 1):
        spec = specs.get(tid) or specs.get(str(tid).split("#")[0]) or {}
        ttype = str((spec or {}).get("type") or tid)
        keys = ", ".join(sorted((spec or {}).get("params", {}) or {})) if isinstance(spec, dict) else ""
        ok = "成功" if getattr(res, "success", False) else "失败"
        lines.append(f"{i}. {ttype}({keys}) -> {ok}")
    return "\n".join(lines) or "(无工具调用)"


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠 JSON（容忍 ```json 包裹和前后废话）。"""
    if not text:
        return None
    blob = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", blob, re.S)
    if m:
        blob = m.group(1)
    else:
        start, end = blob.find("{"), blob.rfind("}")
        if start >= 0 and end > start:
            blob = blob[start:end + 1]
    try:
        return json.loads(blob)
    except Exception:
        return None


def _slug(name: str) -> str:
    """技能名规范：去掉空白和标点，保留中英文数字。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", (name or "").strip())
    return cleaned[:20]


def build_skill_from_plan(
    payload: Dict[str, Any],
    *,
    user_input: str,
    tool_results: Dict[str, Any],
    task_specs: Optional[Dict[str, Any]] = None,
) -> Optional[Skill]:
    """把 LLM 的提炼结果拼成一个 Skill 对象（含自动回归样例）。"""
    name = _slug(str(payload.get("name") or ""))
    method = str(payload.get("method") or "").strip()
    capability = str(payload.get("capability") or "").strip()
    patterns = [
        str(p).strip() for p in (payload.get("patterns") or []) if str(p).strip()
    ][:MAX_PATTERNS]

    if not name or not method:
        return None
    if len(method) < MIN_METHOD_CHARS:
        return None
    if not patterns:
        return None

    # steps 来自**真实执行过的工具**，不是编的
    seq = _tool_sequence(tool_results, task_specs)
    steps = [
        SkillStep(
            id=f"s{i + 1}",
            name=f"步骤{i + 1}:{t}",
            description=f"调用 {t}",
            tool=t,
        )
        for i, t in enumerate(seq)
    ]

    # 回归样例来自这次真实成功 —— 技能一出生就带验证
    regression: List[Dict[str, Any]] = []
    if seq:
        regression.append({
            "name": "来自首次成功执行",
            "input": {"user_input": user_input},
            "expect": {"tool": seq[0], "success": True},
        })

    return Skill(
        name=name,
        version="1.0.0",
        capability=capability[:120],
        method=method,
        patterns=patterns,
        tags=["harvested"],
        steps=steps,
        examples=[user_input[:200]],
        regression=regression,
        id=f"skill_{uuid.uuid4().hex[:10]}",
        source="harvested",
        author="auto",
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )


async def harvest(
    user_input: str,
    tool_results: Dict[str, Any],
    *,
    rounds: int = 1,
    task_specs: Optional[Dict[str, Any]] = None,
    store=None,
    drafts: Optional[SkillDraftStore] = None,
    llm=None,
) -> HarvestResult:
    """从一次成功的任务里提炼方法论。

    Returns:
        HarvestResult —— 明确区分「提炼成功」「不满足条件跳过」「提炼失败」。
    """
    logger = get_logger()

    # --- 闸门 1：有没有实质过程 ---
    seq = _tool_sequence(tool_results or {}, task_specs)
    if len(seq) < MIN_DISTINCT_TOOLS and int(rounds or 1) < MIN_ROUNDS:
        return HarvestResult(
            skipped=True,
            reason=(
                f"过程太简单（{len(seq)} 个工具、{rounds} 轮），"
                f"不值得当方法论"
            ),
        )
    if not any(getattr(r, "success", False) for r in (tool_results or {}).values()):
        return HarvestResult(skipped=True, reason="这次没有成功的工具调用，没有可复用的经验")

    # --- 提炼 ---
    try:
        if llm is None:
            from infra.llm import get_llm_client

            llm = get_llm_client()
        prompt = HARVEST_PROMPT.format(
            user_input=(user_input or "")[:800],
            trajectory=_build_trajectory(tool_results, task_specs),
        )
        raw = await llm.chat_with_retry(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_retries=2,
        )
    except Exception as exc:
        logger.warning("SkillHarvest", f"提炼调用失败: {exc}")
        return HarvestResult(reason=f"LLM 调用失败: {exc}")

    payload = _extract_json(raw or "")
    if not payload:
        return HarvestResult(reason="模型输出不是合法 JSON")

    skill = build_skill_from_plan(
        payload,
        user_input=user_input,
        tool_results=tool_results,
        task_specs=task_specs,
    )
    if skill is None:
        return HarvestResult(reason="提炼结果不合格（名字/正文/触发词缺失或太短）")

    # --- 去重：同名技能已存在就不覆盖（人来判断要不要升级） ---
    try:
        if store is None:
            from skills.manager import get_skill_store

            store = get_skill_store()
        if store.get_by_name(skill.name) is not None:
            return HarvestResult(
                skipped=True,
                reason=f"已有同名技能 {skill.name}，不自动覆盖（请人工判断是否升级）",
            )
    except Exception as exc:
        logger.warning("SkillHarvest", f"检查同名技能失败: {exc}")

    # --- 存成**草稿**，等人批准才进技能库 ---
    #
    # 不直接 `store.add(set_active=False)`：`list_all()` 只返回激活版本，
    # 那样"待批准的技能"在界面上永远看不到，等于黑盒。
    try:
        if drafts is None:
            drafts = get_draft_store()
        draft_id = drafts.save(skill, origin=(user_input or "")[:200])
    except Exception as exc:
        logger.warning("SkillHarvest", f"写入草稿失败: {exc}")
        return HarvestResult(reason=f"写入草稿失败: {exc}")

    logger.info("SkillHarvest", f"提炼出新方法论: {skill.name}（草稿待批准）")
    return HarvestResult(
        skill=skill,
        reason="已存为草稿，批准后才会被 planner 选中",
        draft_id=draft_id,
    )
