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
from core.skill_ledger import get_ledger, normalize_key
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

# ---------------------------------------------------------------------------
# 价值判据的词表。
#
# 一条方法论有没有价值，不等于"写得像不像方法论"（那是 assess_method_quality 管的），
# 也不等于"做了几次"（那是账本 readiness 管的）。它只有一个标准：
#
#     里面有没有 planner 凭常识推不出来、下次不看就会做错/想不到的东西。
#
# 能落入这个标准的信息就四类，下面按类给词表。命中任意一类即算"有价值"。
# ---------------------------------------------------------------------------

#: 坑 / 失败模式：踩过或差点踩的错，以及怎么绕开
_VALUE_PITFALL_WORDS = (
    "坑", "否则", "不要", "别", "误", "错", "失败", "踩", "差点", "陷阱",
    "容易", "千万", "小心", "注意", "遗漏", "忽略", "反例", "边界",
)
#: 反直觉：看起来该这样、其实该那样
_VALUE_COUNTER_WORDS = (
    "其实", "以为", "实际上", "反而", "看起来", "不是", "而是", "往往",
    "别看", "越", "直觉", "表面", "未必", "通常", "误区",
)
#: 条件分支：出现信号 A 走这条路、信号 B 换招（要同时有触发词和动作词）
_VALUE_BRANCH_TRIGGERS = ("如果", "若", "当", "一旦", "遇到", "取决于", "分情况", "看情况")
_VALUE_BRANCH_ACTIONS = ("就", "则", "改", "换", "再", "才", "走", "用", "退")
#: 完成标准：怎么算真做完，而不是"看起来做完了"
_VALUE_DONE_WORDS = (
    "才算", "标准", "核对", "读回", "验证", "确认", "检查", "为准",
    "缺任何", "不能算", "不算做完", "才算完成",
)
#: 纯顺序叙述的信号 —— 只有步骤词、没有任何判断，是被 reject 的对象
_VALUE_SEQUENCE_WORDS = ("第一步", "第二步", "第三步", "然后", "接着", "再", "最后")


HARVEST_PROMPT = """你在复盘一次**成功完成**的任务。目标是让系统对"这类事该怎么做"多懂一点，
而不是又多一条流水账。

## 这次任务
用户要求：{user_input}

## 实际执行过程
{trajectory}

## 已有的能力档案
{existing}

## 第一步：归类，并判断这次有没有新东西

同一类事必须归到同一条档案。**不要因为换了个说法就另起一条** ——
这正是过去技能库失控的原因：「充值完成处理」和「充值后重生成」被记成了两条。

- 如果这次属于上面某个已有档案 → `capability_key` 原样用那个 key，`reuse` 给 true
- 如果确实都不属于 → 自己起一个 key，规则：
  - 英文小写 snake_case，6~40 字符
  - 描述的是**一类事**，不是这一次
  - ❌ `charge_after_regenerate`、`fix_temp_bug`、`do_it_again` —— 这种只对这次成立
  - ✅ `short_drama_video_publish`、`ecom_listing_batch` —— 换个对象照样成立
  - 自检：把 key 念一遍，如果它里面含着这次的时间/对象/数字，就说明起错了

归类之后，判断这次**相对已有方法论**是什么性质，填进 `novelty`：

| novelty | 含义 | 要不要写 method |
|---|---|---|
| `new` | 出现了上一版没有的判断 / 坑 / 完成标准 | **必须写** |
| `same` | 与已有方法论一致，这次没有新东西 | 可以不写，系统沿用档案里的 |
| `not_worth` | 这类事本身不值得沉淀（一次性的、换个场景完全用不上） | 不写 |

**`same` 不是"没价值"。** 一类事反复做都说不出新东西，恰恰说明做法已经稳定 ——
这正是它该固化成技能的时候。只有 `not_worth` 才是真的不值得记。

## 第二步：写方法论（novelty = new 时必写）

不要复述执行顺序 —— 调过哪些工具、什么顺序，系统已经单独存成结构化步骤了。
下面这些写出来都是废的：

- ❌「第一步调用 A；第二步调用 B」—— 换个场景 A/B 根本不存在
- ❌ 把工具名、任务编号、参数名写进正文
- ❌ 复述这次的具体数据（城市、金额、文件名、链接）

要写的是三类判断：

1. **判断规则**：出现什么信号走这条路，出现什么信号该换招
2. **坑**：踩过或差点踩到的地方，以及怎么发现的
3. **完成标准**：怎么算真做完，而不是"看起来做完了"

用「如果…就…」「先确认…再…」这类句式，200~400 字。

**如果已有档案里已有方法论，你写的是"合并后的完整版"** ——
上一版仍然成立的部分要保留，不要只写这次新增的那一条。

### 最重要的一个字段：insight

`insight` 不是 method 的摘要，而是**这次真正学到的、光看任务描述推不出来的一点**。
自检方法：把它删掉，下次遇到同类任务会不会做错或走弯路？会 → 值得写；不会 → 这次没攒到东西。

- ✅「发布失败先看产物能不能被读回，能读回就是调用偶发失败、直接重试；读不回才是内容问题」
- ✅「这个平台批量上架的限额是按店铺算、不是按账号算，换个店铺要重新探一次」
- ❌「这次用 python 脚本处理了数据」（这是做了什么，不是学到了什么）
- ❌「要注意质量，多检查」（谁都会说的空话）

如果你实在想不出这样一句 insight，把 `novelty` 给 `same` 或 `not_worth`，别硬凑。

## 第三步：如果归到了已晋升成技能的档案（has_skill=true）

除 `novelty` 外，还要在 `revision_note` 里写清**这次比上一版多知道了什么**：
- ✅「上一版说失败就重试；这次发现配额耗尽重试无用，得先去查额度」
- ❌「又做了一次，流程一样」—— 那就把 `novelty` 给 `same`，不会产出修订

## 输出

只输出一个 JSON 对象：

{{"novelty": "new",
  "reason": "novelty=not_worth 时说明为什么这类事不值得沉淀",
  "capability_key": "short_drama_video_publish",
  "reuse": true,
  "revision_note": "仅当 novelty=new 且归到已有技能的档案时填",
  "name": "4~12 字中文名",
  "capability": "一句话说清这套判断能解决什么（不超过 40 字）",
  "insight": "这次真正学到的、反直觉的一点；想不出就留空并把 novelty 给 same",
  "method": "判断规则 + 坑 + 完成标准，不要出现任何工具名",
  "patterns": ["3~6 个用户会怎么开口的通用说法，不要照抄这次的原话"]}}
"""


class HarvestResult:
    """提炼结果。

    `status` 说清这次到底发生了什么 —— 这比"成功/失败"重要得多，
    因为"记了一笔但还不够格"是**正常且预期**的结果，不是失败：

    - ``accumulating``：记进能力档案了，同类观察还不够，暂不产出技能
    - ``drafted``：够格了，产出新技能草稿（等人批准）
    - ``revision``：该档案已有技能，产出的是**修订**草稿（版本 +1）
    - ``skipped``：这次没有沉淀价值（任务太简单 / 模型判定无新意）
    - ``failed``：调用或校验失败
    """

    __slots__ = ("skill", "skipped", "reason", "draft_id", "status", "competence_key")

    def __init__(
        self,
        skill: Optional[Skill] = None,
        skipped: bool = False,
        reason: str = "",
        draft_id: str = "",
        status: str = "",
        competence_key: str = "",
    ):
        self.skill = skill
        self.skipped = skipped
        self.reason = reason
        self.draft_id = draft_id
        self.competence_key = competence_key
        if status:
            self.status = status
        elif skill is not None:
            self.status = "drafted"
        elif skipped:
            self.status = "skipped"
        else:
            self.status = "accumulating"

    def __bool__(self) -> bool:
        return self.skill is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "harvested": self.skill is not None,
            "skipped": self.skipped,
            "status": self.status,
            "competence_key": self.competence_key,
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

    def save(
        self,
        skill: Skill,
        *,
        origin: str = "",
        revision_of: str = "",
        base_version: str = "",
        competence_key: str = "",
    ) -> str:
        """存草稿。

        `revision_of` 非空表示这是**给已有技能打的修订**，不是新技能 ——
        批准时走升版本，而不是往库里再加一条。
        """
        self._ensure()
        path = self._path(skill.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 用户原话里可能直接贴了 API key / 内网地址（真实发生过：两条草稿
            # 的 origin 明文存着 sk-... 和 http://x.x.x.x:port）。存盘前统一脱敏，
            # 这里不截断，保留足够上下文给人判断这条草稿讲的是什么。
            payload: Dict[str, Any] = {"origin": _scrub_origin(origin), "skill": asdict(skill)}
            if revision_of:
                payload["revision_of"] = revision_of
                payload["base_version"] = base_version
            if competence_key:
                payload["competence_key"] = competence_key
            quality = getattr(skill, "quality", None)
            if quality:
                # 让"为什么这条该改/该丢"跟着草稿走，界面不用重算
                payload["quality"] = quality
            value = getattr(skill, "value", None)
            if value:
                # "凭什么算有价值"同样跟着草稿走，供人复核
                payload["value"] = value
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.error("SkillDraft", f"保存草稿失败 {path}: {exc}")
        return skill.id

    def get_payload(self, draft_id: str) -> Optional[Dict[str, Any]]:
        """取完整草稿（含 revision_of / competence_key 这些元信息）。"""
        path = self._path(draft_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.logger.warning("SkillDraft", f"读取草稿失败 {path}: {exc}")
            return None

    def get(self, draft_id: str) -> Optional[Skill]:
        payload = self.get_payload(draft_id)
        if payload is None:
            return None
        return _skill_from_dict(payload.get("skill") or {})

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
                    "value": payload.get("value") or {},
                    # 修订草稿：改的是哪个技能、基于哪一版
                    "revision_of": payload.get("revision_of") or "",
                    "base_version": payload.get("base_version") or "",
                    "competence_key": payload.get("competence_key") or "",
                })
            except Exception:
                continue
        return out

    def find_by_competence(self, key: str) -> Optional[Dict[str, Any]]:
        """按能力 key 找未审批的草稿 —— 同一类事只留一条待办，别越攒越多。"""
        if not key or not self.base.exists():
            return None
        for path in sorted(self.base.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (payload.get("competence_key") or "") != key:
                continue
            raw = payload.get("skill") or {}
            return {
                "id": raw.get("id"),
                "path": str(path),
                "revision_of": payload.get("revision_of") or "",
                "base_version": payload.get("base_version") or "",
            }
        return None

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
    """批准一个草稿 → **落盘** + 激活（planner 立刻能看到，重启也不丢）。

    草稿分两种，批准的含义不同：

    - 新技能：写进 `skills/user/`，同时把能力档案挂上这个技能
    - 修订（``revision_of`` 非空）：**不新增**，走升版本，旧版保留可回滚
    """
    if drafts is None:
        drafts = get_draft_store()
    payload = drafts.get_payload(draft_id) or {}
    skill = drafts.get(draft_id)
    if skill is None:
        return False, f"草稿不存在: {draft_id}"

    if store is None:
        from skills.manager import get_skill_store

        store = get_skill_store()

    revision_of = payload.get("revision_of") or ""
    competence_key = payload.get("competence_key") or ""

    if revision_of:
        return _approve_revision(
            draft_id, skill, payload, store, drafts, revision_of, competence_key
        )

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

    # 档案挂上技能：以后同类证据产的是修订，不是新技能
    if competence_key:
        try:
            get_ledger().bind_skill(
                competence_key,
                skill_id=skill.id,
                skill_name=skill.name,
                version=skill.version,
            )
        except (ValueError, OSError) as exc:
            drafts.logger.warning("SkillLedger", f"档案挂载失败 {competence_key}: {exc}")

    drafts.delete(draft_id)
    return True, f"技能 {skill.name} 已启用"


def _approve_revision(
    draft_id: str,
    skill: Skill,
    payload: Dict[str, Any],
    store,
    drafts,
    revision_of: str,
    competence_key: str,
) -> tuple:
    """批准修订：升版本而不是新增一条技能。"""
    target = None
    getter = getattr(store, "get", None)
    if callable(getter):
        target = getter(revision_of)
    if target is None:
        target = store.get_by_name(skill.name)
    if target is None:
        return False, "修订目标技能不存在（可能已被删除），请丢弃这条草稿"

    updater = getattr(store, "update_skill", None)
    if not callable(updater):
        return False, "当前技能库不支持升版本，无法应用修订"

    updates = {
        "capability": skill.capability or target.capability,
        "method": skill.method,
        "patterns": skill.patterns or target.patterns,
        "steps": [s.to_dict() for s in skill.steps] if skill.steps else [],
        "examples": list(target.examples) + [e for e in skill.examples if e not in target.examples][:5],
    }
    if not updates["steps"]:
        updates.pop("steps")

    try:
        written = updater(target.name, updates)
    except (OSError, ValueError, RuntimeError) as exc:
        return False, f"升版本失败: {exc}"
    if not written:
        return False, "升版本失败：写盘异常（详见后端日志）"

    version = ""
    for item in written:
        match = re.search(r"@([\d.]+)\.ya?ml$", str(item))
        if match:
            version = match.group(1)
            break

    if competence_key:
        try:
            get_ledger().bump_revision(competence_key, version=version)
        except (ValueError, OSError) as exc:
            drafts.logger.warning("SkillLedger", f"修订计数失败 {competence_key}: {exc}")

    drafts.delete(draft_id)
    shown = f"v{version}" if version else "新版本"
    return True, f"技能 {target.name} 已升级到 {shown}"


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


def _scrub_origin(text: str, limit: int = 200) -> str:
    """脱敏用户原话里的密钥/链接/路径，供草稿 origin 存储。

    和 ``_scrub`` 共用规则，但不压到 80 字 —— origin 是给人看"这条草稿源自哪句话"，
    要留够上下文。密钥这类敏感串无论如何都不能明文落盘。
    """
    out = str(text or "")
    for pattern in _SCRUB_RES:
        out = pattern.sub("<已省略>", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit].rstrip() + ("…" if len(out) > limit else "")


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


def _format_catalog(items: List[Dict[str, Any]], limit: int = 12) -> str:
    """把已有能力档案压成提示词里的一段清单。

    给清单是为了让模型**归到已有档案**，而不是每次另起一个 key ——
    「充值完成处理」和「充值后重生成」就是这么变成两条技能的。
    """
    if not items:
        return "（还没有任何能力档案，这次会是第一条）"
    lines: List[str] = []
    for i, c in enumerate(items[:limit], 1):
        parts = [f"{i}. key={c.get('key')}"]
        if c.get("title"):
            parts.append(f"「{c['title']}」")
        if c.get("capability"):
            parts.append(str(c["capability"])[:40])
        if c.get("tools"):
            parts.append("工具=" + ",".join(str(t) for t in c["tools"][:5]))
        parts.append(f"已记 {c.get('times', 0)} 次")
        if c.get("has_skill"):
            parts.append("已成技能")
        lines.append(" | ".join(parts))
        if c.get("method"):
            # 带上已有正文，模型才能写"合并后的完整版"，而不是从零开始重写
            condensed = re.sub(r"\s+", " ", str(c["method"]))[:180]
            lines.append(f"   已有方法论：{condensed}")
    if len(items) > limit:
        lines.append(f"……另有 {len(items) - limit} 个档案未列出")
    return "\n".join(lines)


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


def assess_value(
    method: str,
    *,
    insight: str = "",
    capabilities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """判断一条方法论**有没有可迁移的价值** —— 回答"凭什么值得成为 skill"。

    这条闸门和 ``assess_method_quality`` 的分工：

    - ``assess_method_quality`` 管**形式**：是不是把工具流水账写成了散文
    - ``assess_value`` 管**实质**：内容里有没有"下次换场景还用得上"的判断

    判据只有一条，来自用户的产品定义：

        skill 是做事的方法，不是做过的事的流水账。
        一条 method 值钱，当且仅当它含有 planner 光看任务描述推不出来的信息 ——
        反直觉的判断、踩过的坑、隐式的完成标准。
        把这些删掉，剩下"先做 A 再做 B"的部分，planner 每次都能自己推出来，
        固化它只会用上次的路径覆盖这次的计划，让系统变笨。

    ``insight`` 是让模型显式说出的"这次真正学到的、反直觉的一点"。它和规则
    检测是**或**的关系：规则命中任一硬信号即算有价值；规则没命中、但模型
    说清了一句具体的反直觉洞察，也算。两者都拿不出 → 判无价值。

    Returns:
        ``{"valuable": bool, "signals": [...], "reason": str, ...}``
        ``signals`` 是命中的硬信号类型，界面可展示"它凭什么算有价值"。
    """
    text = (method or "").strip()
    insight = (insight or "").strip()
    signals: List[str] = []

    # 硬信号 1：踩过的坑 / 失败模式
    pit = [w for w in _VALUE_PITFALL_WORDS if w in text]
    if pit:
        signals.append("踩坑/失败模式：" + "、".join(pit[:3]))

    # 硬信号 2：反直觉判断
    counter = [w for w in _VALUE_COUNTER_WORDS if w in text]
    if counter:
        signals.append("反直觉判断：" + "、".join(counter[:3]))

    # 硬信号 3：条件分支（触发词 + 动作词同时出现，避免"当…"这种孤词误报）
    has_trigger = any(w in text for w in _VALUE_BRANCH_TRIGGERS)
    has_action = any(w in text for w in _VALUE_BRANCH_ACTIONS)
    if has_trigger and has_action:
        signals.append("按信号分支处理")

    # 硬信号 4：显式完成标准
    done = [w for w in _VALUE_DONE_WORDS if w in text]
    if done:
        signals.append("完成标准：" + "、".join(done[:3]))

    valuable = bool(signals)

    # 规则没抓到硬信号，但模型说清了一句具体的反直觉洞察 —— 也算。
    # 要求 ≥15 字，挡住"要注意质量"这种谁都会说的空话。
    if not valuable and len(insight) >= 15:
        valuable = True
        signals.append("模型给出的反直觉洞察：" + insight[:40])

    if valuable:
        reason = "含可迁移判断：" + "；".join(signals)
    else:
        # 说清楚为什么判它没价值 —— 这条理由会展示在草稿面板上，人可复核
        steps = sum(1 for w in _VALUE_SEQUENCE_WORDS if w in text)
        if steps >= 2:
            reason = f"通篇是操作顺序（{steps} 个步骤词），没有踩坑/反直觉/完成标准 —— 这类判断 planner 每次都能自己推"
        else:
            reason = "找不到可迁移的判断（无踩坑、无反直觉、无分支、无完成标准），像是一次性经过的记录"

    return {
        "valuable": valuable,
        "signals": signals,
        "reason": reason,
        "insight_used": bool(signals and not pit and not counter and not (has_trigger and has_action) and not done),
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

    # 自动沉淀出来的技能**只带方法论，不带可重放步骤**。
    #
    # steps 曾经由工具序列自动生成，但参数是空的（参数值在脱敏时被删掉了）。
    # planner 一旦选中这种技能，DAGExecutor 会把 `web_search(空)` 一个个跑下去，
    # 从头失败到尾 —— 技能库反而成了故障源。
    # 参数完整的 DAG 只能人教（SkillTrainer）或手写，机器攒出来的部分只配当判断。
    # 回归样例仍然来自这次真实成功，用来记"这类任务长什么样"。
    seq = _tool_sequence(tool_results, task_specs)
    steps: List[SkillStep] = []

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
        # steps 故意留空（空参数 DAG 会从头失败），但**工具清单要留下** ——
        # 它来自这次真实成功的执行序列，是子智能体按需裁剪工具集的依据。
        required_tools=seq,
        examples=[user_input[:200]],
        regression=regression,
        id=f"skill_{uuid.uuid4().hex[:10]}",
        source="harvested",
        author="auto",
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )
    # 质量提示 + 价值判定随草稿一起存，界面上让人批准前先看一眼
    setattr(skill, "quality", quality)
    insight_for_value = str(payload.get("insight") or "").strip()
    setattr(skill, "value", assess_value(method, insight=insight_for_value))
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
    ledger=None,
    session_id: str = "",
) -> HarvestResult:
    """一次成功任务结束后调用 —— 但**不一定产出技能**。

    这是和旧实现的根本区别：这里记的是"做了一次这类事"，不是"这个技能第 N 版"。
    产出与否由能力档案的证据量决定：

        第 1 次 → 只记账，不产草稿
        第 2 次 → 只记账，不产草稿
        第 3 次 → 够格，产出技能草稿（等人批准）
        第 4 次起 → 该技能已存在，产出**修订**草稿（版本 +1）

    Returns:
        HarvestResult —— `status` 区分 accumulating / drafted / revision / skipped / failed。
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

    if ledger is None:
        ledger = get_ledger()

    # --- 一次 LLM 调用同时干两件事：归到哪个档案 + 这次多知道了什么 ---
    try:
        if llm is None:
            from infra.llm import get_llm_client

            llm = get_llm_client()
        prompt = HARVEST_PROMPT.format(
            user_input=(user_input or "")[:800],
            trajectory=_build_trajectory(tool_results, task_specs),
            existing=_format_catalog(ledger.catalog()),
        )
        raw = await llm.chat_with_retry(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_retries=2,
        )
    except Exception as exc:
        logger.warning("SkillHarvest", f"提炼调用失败: {exc}")
        return HarvestResult(status="failed", reason=f"LLM 调用失败: {exc}")

    payload = _extract_json(raw or "")
    if not payload:
        return HarvestResult(status="failed", reason="模型输出不是合法 JSON")

    novelty = _novelty_of(payload)
    comp_key = normalize_key(str(payload.get("capability_key") or ""))

    if not comp_key:
        # 模型直接说"这类事不值得沉淀"时，key 缺失是正常的 —— 不是失败，只是没东西可记
        if novelty == "not_worth":
            return HarvestResult(
                skipped=True,
                status="skipped",
                reason="模型判断这类事不值得沉淀：" + str(payload.get("reason") or "未说明"),
            )
        return HarvestResult(
            status="failed",
            reason="模型没给出合规的能力 key（需要英文 snake_case），这次无法归类",
        )

    # 这类事本身不值得沉淀 —— 一条都不记，否则档案表会被一次性任务塞满
    if novelty == "not_worth":
        return HarvestResult(
            skipped=True,
            status="skipped",
            reason="模型判断这类事不值得沉淀：" + str(payload.get("reason") or "未说明"),
        )

    name = _slug(str(payload.get("name") or ""))
    capability = str(payload.get("capability") or "").strip()[:120]
    method = str(payload.get("method") or "").strip()
    insight = str(payload.get("insight") or "").strip()
    patterns = [str(p).strip() for p in (payload.get("patterns") or []) if str(p).strip()]

    # --- 价值闸门：这次到底有没有攒下"下次用得上"的东西 ---
    # 放在记账之前：模型可能嘴上说 new，交上来的却是"先做 A 再做 B"的流水账。
    # 规则先过一遍硬信号，模型给的 insight 做补充，两者都拿不出就当没学到东西。
    value = assess_value(method, insight=insight)
    if novelty == "new" and not value["valuable"]:
        # 没价值不覆盖档案里已有的好正文 —— 这次的判断不足以推翻上一版
        method = ""
        insight = ""

    # --- 记账：无论最后产不产技能，这一笔都要落下 ---
    # 不落账的话，第 1 次、第 2 次永远不会被记成"同类做了几次"，阈值就永远到不了。
    try:
        comp = ledger.record(
            comp_key,
            title=name,
            capability=capability,
            method=method if novelty == "new" else "",
            patterns=patterns if novelty == "new" else None,
            tools=seq,
            user_input=user_input or "",
            session_id=session_id,
            rounds=int(rounds or 0),
            insight=insight,
        )
    except (ValueError, OSError) as exc:
        return HarvestResult(status="failed", competence_key=comp_key, reason=f"记账失败: {exc}")

    bound_skill = str(comp.get("skill_id") or "")
    revision_note = str(payload.get("revision_note") or "").strip()

    # 判 same 时模型不重写正文、也不给触发词 —— 全部沿用档案里已经沉淀的那一版。
    # 否则"稳定"反而会因为"没内容可写"被卡在晋升之前。
    if not method and comp.get("method"):
        payload["method"] = str(comp["method"])
    if not patterns and comp.get("patterns"):
        payload["patterns"] = list(comp["patterns"])
    if not name and comp.get("title"):
        payload["name"] = str(comp["title"])
    if not capability and comp.get("capability"):
        payload["capability"] = str(comp["capability"])

    # --- 一个档案已经有技能了 → 产出的是修订，不是新技能 ---
    if bound_skill:
        if novelty != "new" or not revision_note:
            return HarvestResult(
                skipped=True,
                status="skipped",
                competence_key=comp_key,
                reason="该技能已存在，这次没有新判断 —— 重复不构成修订",
            )
        skill, why_bad = _build_skill_from_plan(
            payload,
            user_input=user_input,
            tool_results=tool_results,
            task_specs=task_specs,
        )
        if skill is None:
            return HarvestResult(status="failed", competence_key=comp_key, reason=why_bad or "")
        # 沿用原技能名：升版本要靠名字找文件，模型这次可能换了个说法
        skill.name = str(comp.get("skill_name") or skill.name)
        try:
            if drafts is None:
                drafts = get_draft_store()
            draft_id = _save_draft(
                drafts,
                skill,
                origin=(user_input or "")[:200],
                revision_of=bound_skill,
                base_version=str(comp.get("version") or ""),
                competence_key=comp_key,
            )
        except Exception as exc:
            logger.warning("SkillHarvest", f"写入修订草稿失败: {exc}")
            return HarvestResult(status="failed", competence_key=comp_key, reason=f"写入草稿失败: {exc}")

        logger.info("SkillHarvest", f"产出修订草稿: {skill.name} ({revision_note[:40]})")
        return HarvestResult(
            skill=skill,
            status="revision",
            competence_key=comp_key,
            draft_id=draft_id,
            reason=f"技能已有，这次是修订：{revision_note[:80]}",
        )

    # --- 还没成技能 → 够格才产出 ---
    ready, why_ready = ledger.readiness(comp)
    if not ready:
        return HarvestResult(
            skipped=True,
            status="accumulating",
            competence_key=comp_key,
            reason=f"已记入能力档案「{comp_key}」：{why_ready}，够格了再产出技能",
        )

    skill, why_bad = _build_skill_from_plan(
        payload,
        user_input=user_input,
        tool_results=tool_results,
        task_specs=task_specs,
    )
    if skill is None:
        return HarvestResult(status="failed", competence_key=comp_key, reason=why_bad or "提炼结果不合格")

    # 名字撞了已有技能也不能硬塞（可能是另一条线攒出来的同名）
    try:
        if store is None:
            from skills.manager import get_skill_store

            store = get_skill_store()
        if store.get_by_name(skill.name) is not None:
            return HarvestResult(
                skipped=True,
                status="skipped",
                competence_key=comp_key,
                reason=f"已有同名技能 {skill.name}，不自动覆盖（请人工判断是否升级）",
            )
    except (OSError, ValueError) as exc:
        logger.warning("SkillHarvest", f"检查同名技能失败: {exc}")

    try:
        if drafts is None:
            drafts = get_draft_store()
        draft_id = _save_draft(
            drafts,
            skill,
            origin=(user_input or "")[:200],
            competence_key=comp_key,
        )
    except Exception as exc:
        logger.warning("SkillHarvest", f"写入草稿失败: {exc}")
        return HarvestResult(status="failed", competence_key=comp_key, reason=f"写入草稿失败: {exc}")

    logger.info("SkillHarvest", f"够格了，产出技能草稿: {skill.name}（{why_ready}）")
    return HarvestResult(
        skill=skill,
        status="drafted",
        competence_key=comp_key,
        draft_id=draft_id,
        reason=f"凑够观察次数（{why_ready}），已存为草稿，批准后生效",
    )


def _novelty_of(payload: Dict[str, Any]) -> str:
    """这次的观察属于哪种：``new`` / ``same`` / ``not_worth``。

    三者混为一谈会出大事 —— 把「老样子」当成「不值得沉淀」，
    这个档案就永远攒不到晋升阈值：做法越稳定，反而越沉淀不下来。
    实测里第 3 次就栽在这：模型说"完全同类、没有新判断"，
    于是一条本来该晋升的技能被永久卡在 2 次。
    """
    raw = str(payload.get("novelty") or "").strip().lower()
    if raw in ("new", "same", "not_worth"):
        return raw
    # 旧格式兼容：worth_keeping=false 当时把"重复"和"没价值"混在一起说
    return "not_worth" if payload.get("worth_keeping") is False else "new"


def _save_draft(drafts, skill: Skill, *, origin: str, **meta) -> str:
    """存草稿；同一能力 key 已有未审批草稿就**覆盖那一条**，不重复堆。

    否则第 4、5、6 次观察会各产一份草稿，库里又是一堆重复。
    """
    finder = getattr(drafts, "find_by_competence", None)
    existing = finder(meta.get("competence_key") or "") if callable(finder) else None
    if existing and existing.get("id"):
        skill.id = existing["id"]
    return drafts.save(skill, origin=origin, **meta)
