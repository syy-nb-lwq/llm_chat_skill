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

#: 工具标识符：``python_exec`` / ``read_text_file(path)`` 这种。
#: 方法论正文里出现它，基本等于在念调用日志 —— 工具顺序已经存在 steps 里了。
_TOOL_IDENT_RE = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b|\b[a-z][a-z0-9_]*\(\)")
_STEP_WORD_RE = re.compile(r"第[一二三四五六七八九十]+步")
#: 「判断」用词 —— 方法论该有的是这些，而不是「调哪个工具」
_JUDGE_WORDS = (
    "如果", "若", "当", "否则", "前提", "关键", "取决于", "优先", "改用", "退",
    "确认", "核对", "校验", "直到", "避免", "不能", "必须", "宁可", "千万",
    "坑", "失败", "误报", "注意", "切记", "区分", "分清", "先判断",
)


HARVEST_PROMPT = """你在复盘一次**成功完成**的任务，目标是把这次的经验变成**下次能直接复用的判断力**。

## 这次任务
用户要求：{user_input}

## 实际执行过程
{trajectory}

## 重要前提：不要复述执行顺序
调用过哪些工具、按什么顺序调用，系统**已经单独存成结构化步骤**了。
所以下面这些写出来都是废的：

- ❌「第一步调用 A；第二步调用 B；第三步调用 C」—— 换个场景，A/B/C 根本不存在
- ❌ 把工具名、任务编号、参数名写进正文
- ❌ 复述这次的具体数据（城市、金额、文件名、链接）

## 要提炼的是「判断」，不是「步骤」

写清这三类东西：

1. **判断规则**：出现什么信号该走这条路；出现什么信号说明该换招
   （例：「发布失败要先分清是产物本身有问题、还是调用偶发失败；前者回去改内容，后者直接重试」）
2. **坑**：这次踩过或差点踩到的地方，以及是怎么发现的
   （例：「用相对路径会报找不到文件，动手前先确认工作区根目录」）
3. **完成标准**：怎么才算真做完，而不是「看起来做完了」
   （例：「产物必须能被重新读回并核对内容，才算交付完成」）

用「如果…就…」「先确认…再…」这类句式，200~400 字。

## 输出
只输出一个 JSON 对象：

{{"worth_keeping": true,
  "reason": "若 worth_keeping=false，说明这次为什么不值得沉淀",
  "name": "4~12 字中文名",
  "capability": "一句话说清这套判断能解决什么（不超过 40 字）",
  "method": "判断规则 + 坑 + 完成标准，不要出现任何工具名",
  "patterns": ["3~6 个用户会怎么开口的通用说法，不要照抄这次的原话"]}}

**宁缺毋滥**：如果这次做成只是因为任务简单，或者换个场景完全用不上，
把 `worth_keeping` 给 false —— 一条注水的技能比没有技能更糟。
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
            payload: Dict[str, Any] = {"origin": origin, "skill": asdict(skill)}
            quality = getattr(skill, "quality", None)
            if quality:
                # 让"为什么这条该改/该丢"跟着草稿走，界面不用重算
                payload["quality"] = quality
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
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
                    "quality": payload.get("quality") or _quality_of(raw, payload.get("origin") or ""),
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


def _quality_of(raw: Dict[str, Any], origin: str = "") -> Dict[str, Any]:
    """老草稿没有 quality 字段 —— 现算一遍，别让界面显示空白。"""
    return assess_method_quality(
        raw.get("method") or "",
        origin=origin,
        patterns=raw.get("patterns") or [],
    )


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


def approve_draft(draft_id: str, store=None, drafts=None) -> tuple:
    """批准一个草稿 → **落盘** + 激活（planner 立刻能看到，重启也不丢）。"""
    if drafts is None:
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

    # 真 store 走 persist（写 yaml + reload）；测试用的轻量 store 只有 add
    persist = getattr(store, "persist", None)
    try:
        if callable(persist):
            if persist(skill) is None:
                return False, "写入技能库失败：磁盘写入异常（详见后端日志）"
        else:
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


#: 脱敏：长 token / URL / 绝对路径 不该进提炼提示词
_SCRUB_RES = (
    re.compile(r"https?://\S+"),
    re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9\-_]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),
    re.compile(r"[A-Za-z]:\\[^\s，。；、]+"),
)


def _scrub(text: str, limit: int = 80) -> str:
    """把错误信息压成可读的一小段（去掉链接/密钥/路径，再截断）。"""
    out = str(text or "")
    for pattern in _SCRUB_RES:
        out = pattern.sub("<已省略>", out)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > limit:
        out = out[:limit].rstrip() + "…"
    return out


def _build_trajectory(tool_results: Dict[str, Any], task_specs: Optional[Dict[str, Any]] = None) -> str:
    """把执行过程压成给 LLM 看的描述。

    **只写工具名和参数键，不写参数值** —— 参数值里可能有用户隐私数据。

    但成功的/失败的**结果信号**必须写：只给一串工具名，模型能提炼出来的
    只有「第一步调 A、第二步调 B」；给了失败原因，它才可能写出
    「报错说文件没找到，所以下一轮先确认了根目录」这种真正可迁移的经验。
    """
    lines = []
    specs = task_specs or {}
    for i, (tid, res) in enumerate((tool_results or {}).items(), 1):
        spec = specs.get(tid) or specs.get(str(tid).split("#")[0]) or {}
        ttype = str((spec or {}).get("type") or tid)
        keys = ", ".join(sorted((spec or {}).get("params", {}) or {})) if isinstance(spec, dict) else ""
        params = f"参数键: {keys}" if keys else "无参数"
        if getattr(res, "success", False):
            lines.append(f"{i}. {ttype}({params}) -> 成功")
        else:
            lines.append(f"{i}. {ttype}({params}) -> 失败: {_scrub(getattr(res, 'error', ''))}")
    return "\n".join(lines) or "(无工具调用)"


def assess_method_quality(
    method: str,
    *,
    origin: str = "",
    patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """判断一段 method 是「方法论」还是「调用日志的散文版」。

    校准依据是真实沉淀出来的那批草稿：其中大多数写成了
    「第一步，用 python_exec 执行…；第二步，调用 publish_artifact 发布…」——
    工具名 + 顺序，换个场景一个字都用不上。

    Returns:
        ``{"reject": bool, "issues": [...], "warnings": [...], ...}``
        - ``issues`` 命中即**不该进技能库**（污染检索、占注入预算）
        - ``warnings`` 只是提醒，人批准前最好改一版
    """
    text = (method or "").strip()
    issues: List[str] = []
    warnings: List[str] = []

    idents = sorted(set(_TOOL_IDENT_RE.findall(text)))
    if len(idents) >= 2:
        issues.append(
            f"正文点名了 {len(idents)} 个工具（{', '.join(idents[:4])}），这是在复述调用顺序"
        )
    elif idents:
        warnings.append(f"正文出现工具名 {idents[0]}，方法论该写判断，不该写工具")

    steps = len(_STEP_WORD_RE.findall(text))
    judges = sum(1 for w in _JUDGE_WORDS if w in text)
    if steps >= 3 and judges == 0:
        warnings.append(f"分了 {steps} 步却没有一个判断条件，读起来像执行日志")
    elif steps >= 4 and judges <= 1:
        warnings.append(f"分了 {steps} 步却几乎没有判断条件，读起来像执行日志")
    if len(text) < 100:
        warnings.append("正文偏短，可迁移的内容不多")

    # 触发词直接抄用户原话 → 以后只有原话能命中，等于没写
    origin_norm = re.sub(r"\s+", "", origin or "")
    for p in (patterns or []):
        pn = re.sub(r"\s+", "", p or "")
        if len(pn) >= 6 and pn and pn in origin_norm:
            warnings.append(f"触发词「{p}」是这次原话的片段，建议改成更通用的说法")
            break

    return {
        "reject": bool(issues),
        "issues": issues,
        "warnings": warnings,
        "steps": steps,
        "judge_hits": judges,
        "tool_idents": idents,
        "chars": len(text),
    }


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
    return _build_skill_from_plan(
        payload,
        user_input=user_input,
        tool_results=tool_results,
        task_specs=task_specs,
    )[0]


def _build_skill_from_plan(
    payload: Dict[str, Any],
    *,
    user_input: str,
    tool_results: Dict[str, Any],
    task_specs: Optional[Dict[str, Any]] = None,
) -> tuple:
    """内部版：连「为什么不合格」一起返回，好让 harvest 说清原因。"""
    name = _slug(str(payload.get("name") or ""))
    method = str(payload.get("method") or "").strip()
    capability = str(payload.get("capability") or "").strip()
    patterns = [
        str(p).strip() for p in (payload.get("patterns") or []) if str(p).strip()
    ][:MAX_PATTERNS]

    if not name or not method:
        return None, "提炼结果不合格（名字/正文缺失）"
    if len(method) < MIN_METHOD_CHARS:
        return None, f"正文太短（{len(method)} 字，至少要 {MIN_METHOD_CHARS} 字）"
    if not patterns:
        return None, "没有触发词，以后匹配不上"

    quality = assess_method_quality(method, origin=user_input, patterns=patterns)
    if quality["reject"]:
        return None, "提炼成了执行日志而不是方法论：" + "；".join(quality["issues"])

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

    skill = Skill(
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
    # 质量提示随草稿一起存，界面上让人批准前先看一眼
    setattr(skill, "quality", quality)
    return skill, ""


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

    # 模型自己判断这次不值得沉淀（任务太简单 / 换个场景用不上）
    if payload.get("worth_keeping") is False:
        return HarvestResult(
            skipped=True,
            reason="模型判断这次没有可复用价值：" + str(payload.get("reason") or "未说明"),
        )

    skill, why_bad = _build_skill_from_plan(
        payload,
        user_input=user_input,
        tool_results=tool_results,
        task_specs=task_specs,
    )
    if skill is None:
        return HarvestResult(reason=why_bad or "提炼结果不合格")

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
