"""能力档案账本 —— 技能晋升之前的那段路。

## 为什么需要它

原来的沉淀是：**一次任务做完 → 立刻产出一条技能**。
于是「充值完成处理」和「充值后重生成」在 27 分钟内沉淀了两条，
系统完全不知道它们是同一件事。

技能的定义是「一类任务的做法」，所以它的最小观察单位**不能是一次任务**。
这个模块补的就是中间那层：先把同类任务记到同一个**能力档案**下，
攒够证据才允许晋升成技能；晋升之后再有新证据，产出的是**修订**，不是新增。

## 为什么"同类"不由字符串判断

拿 15 条真实草稿实测过：按原话字符 bigram + 工具集合 Jaccard 聚类，
最高相似度 0.32，任何阈值都聚不出第二组。
「充值完了」和「重新生成 我新充值了金额」在字面上几乎无关。

所以归类只能靠语义。好消息是 harvest 本来就要调一次 LLM，
把"归到哪个档案"塞进同一次调用即可，增量成本接近零。

## 一个档案的一生

    created ──(证据 < 3)──> accumulating ──(证据 ≥ 3 且无纠正)──> promoted
                                                                    │
                                                    新证据 ──> revision 草稿（v+1）

账本只记"发生了什么"，不记"该怎么写" —— 正文永远由 LLM 产出、人批准。

## 存放

`memory/skills/ledger.json`，一个 JSON。用文件不用 DB 是因为：
数据量在百级、要能直接 cat 出来看、要能进 git 对比历史。
写入走 tmp + replace，避免半截文件。
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from infra.logger import get_logger

#: 攒够几次成功观察，才允许把档案晋升成技能。
#: 3 而不是 2：两次可能只是同一批工作的两半，三次才像"一类事"。
PROMOTE_MIN_EVIDENCE = 3

#: 有过纠正的档案，需要额外这么多次干净成功才洗清。
#: 被纠正说明原来的做法错了，不能靠次数堆过去。
PROMOTE_EXTRA_AFTER_CORRECTION = 2

#: 单个档案最多留多少条证据（防止无限涨，也够看出稳定性了）
MAX_EVIDENCE = 50

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{5,39}$")


def normalize_key(raw: str) -> str:
    """把模型给的能力 key 规范成 snake_case；不合规返回空串。"""
    text = (raw or "").strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]", "", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text if _KEY_RE.match(text) else ""


class CompetenceLedger:
    """能力档案账本。线程安全，进程内共享一个实例。"""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            path = Path(__file__).parent.parent / "memory" / "skills" / "ledger.json"
        self.path = Path(path)
        self.logger = get_logger()
        self._lock = threading.RLock()
        self._data: Optional[Dict[str, Any]] = None

    # ---------- 读写 ----------

    def _load(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        data: Dict[str, Any] = {"version": 1, "competences": {}}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("competences"), dict):
                    data = raw
            except (OSError, ValueError) as exc:
                self.logger.warning("SkillLedger", f"读取账本失败（按空账本继续）: {exc}")
        self._data = data
        return data

    def _flush(self) -> None:
        data = self._load()
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except (OSError, ValueError) as exc:
            # 账本坏了不该影响主流程：这次晋升失败，下次再来
            self.logger.warning("SkillLedger", f"写入账本失败: {exc}")
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def reload(self) -> None:
        with self._lock:
            self._data = None

    # ---------- 查询 ----------

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._load()["competences"].values())

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._load()["competences"].get(key)

    def catalog(self, limit: int = 20) -> List[Dict[str, Any]]:
        """给 LLM 看的档案摘要 —— 让它尽量归到已有档案，而不是另起一个。

        按证据数倒序：最近做得多的更可能被复用。
        """
        with self._lock:
            items = list(self._load()["competences"].values())
        items.sort(key=lambda c: (-len(c.get("evidence") or []), c.get("updated_at", "")))
        out = []
        for c in items[:limit]:
            out.append({
                "key": c.get("key"),
                "title": c.get("title", ""),
                "capability": c.get("capability", ""),
                "method": (c.get("method") or "")[:240],
                "tools": (c.get("tools") or [])[:6],
                "times": len(c.get("evidence") or []),
                "has_skill": bool(c.get("skill_id")),
            })
        return out

    # ---------- 写入 ----------

    def record(
        self,
        key: str,
        *,
        title: str = "",
        capability: str = "",
        method: str = "",
        patterns: Optional[List[str]] = None,
        tools: Optional[List[str]] = None,
        user_input: str = "",
        session_id: str = "",
        rounds: int = 0,
        corrected: bool = False,
        note: str = "",
        insight: str = "",
    ) -> Dict[str, Any]:
        """记一条证据。这是"做了一次"的唯一落点，无论最后产不产技能。

        `method` 只在**有新判断**时传。它跟着档案走，是为了让"第 3 次够格"时
        手里有正文可用 —— 那次模型往往判"和上次一样"，不会重写正文。

        `insight` 是这次真正学到的、反直觉的一点。攒着它是因为：晋升时不只看
        "做了几次"，还要看"这几攒下来到底学到了什么" —— 没有 insight 的档案
        说明每次都在重复同一个平凡动作，次数再多也不该变成技能。
        """
        key = normalize_key(key)
        if not key:
            raise ValueError("能力 key 不合规")
        now = datetime.now().isoformat()
        with self._lock:
            data = self._load()
            comp = data["competences"].get(key)
            if comp is None:
                comp = {
                    "key": key,
                    "title": title or key,
                    "capability": capability,
                    "method": method,
                    "patterns": list(patterns or []),
                    "tools": [],
                    "insights": [],
                    "evidence": [],
                    "skill_id": "",
                    "skill_name": "",
                    "version": "",
                    "promoted_at": "",
                    "revisions": 0,
                    "created_at": now,
                    "updated_at": now,
                }
                data["competences"][key] = comp
            # 描述用新的一次刷新（越新的越准），但不清空已有工具
            if title:
                comp["title"] = title
            if capability:
                comp["capability"] = capability
            # 只有这次的判断比上一版更完整时才覆盖正文 —— 空值不许把好的冲掉
            if method:
                comp["method"] = method
            if patterns:
                comp["patterns"] = list(patterns)[:8]
            merged = list(comp.get("tools") or [])
            for t in tools or []:
                if t and t not in merged:
                    merged.append(t)
            comp["tools"] = merged[:12]

            # insight 去重累积到档案层：晋升判定要看"攒下来到底学到了什么"
            if insight:
                insights = comp.setdefault("insights", [])
                if insight[:300] not in insights:
                    insights.append(insight[:300])
                comp["insights"] = insights[:MAX_EVIDENCE]

            comp["evidence"].append({
                "at": now,
                "user_input": (user_input or "")[:200],
                "session_id": session_id,
                "tools": list(tools or [])[:12],
                "rounds": int(rounds or 0),
                "corrected": bool(corrected),
                "insight": insight[:300],
                "note": note[:200],
            })
            if len(comp["evidence"]) > MAX_EVIDENCE:
                comp["evidence"] = comp["evidence"][-MAX_EVIDENCE:]
            comp["updated_at"] = now
            self._flush()
            return dict(comp)

    def forget(self, key: str) -> bool:
        """丢掉一条档案。给"这类事其实不值得沉淀"一个出口。"""
        with self._lock:
            data = self._load()
            if key not in data["competences"]:
                return False
            del data["competences"][key]
            self._flush()
            return True

    def bind_skill(self, key: str, *, skill_id: str, skill_name: str, version: str) -> None:
        """档案晋升成技能后，把技能挂回档案 —— 以后同一个 key 走的都是修订。"""
        with self._lock:
            comp = self._load()["competences"].get(key)
            if comp is None:
                return
            comp["skill_id"] = skill_id
            comp["skill_name"] = skill_name
            comp["version"] = version
            comp["promoted_at"] = datetime.now().isoformat()
            comp["updated_at"] = comp["promoted_at"]
            self._flush()

    def bump_revision(self, key: str, version: str = "") -> int:
        with self._lock:
            comp = self._load()["competences"].get(key)
            if comp is None:
                return 0
            comp["revisions"] = int(comp.get("revisions") or 0) + 1
            if version:
                comp["version"] = version
            comp["updated_at"] = datetime.now().isoformat()
            self._flush()
            return comp["revisions"]

    # ---------- 晋升判定 ----------

    @staticmethod
    def readiness(comp: Dict[str, Any]) -> Tuple[bool, str]:
        """这个档案够不够格成技能。返回 (是否够格, 原因)。"""
        evidence = comp.get("evidence") or []
        total = len(evidence)
        corrected = sum(1 for e in evidence if e.get("corrected"))
        need = PROMOTE_MIN_EVIDENCE + corrected * PROMOTE_EXTRA_AFTER_CORRECTION

        if total < need:
            detail = f"已观察 {total} 次"
            if corrected:
                detail += f"（其中 {corrected} 次被纠正，需 {need} 次干净成功）"
            return False, f"{detail}，至少需要 {need} 次才像一类任务"

        # 光有次数不够 —— 得真学到了东西。
        #
        # "做了 3 次"只证明这件事反复发生，不证明里面有任何 planner 推不出来的判断。
        # 一个每天-pointless 地重复的简单动作，做 100 次也不配当技能。
        # 这里要求至少留下一句 insight，或一条带判断的 method；都没有就是纯重复。
        has_insight = any((e.get("insight") or "").strip() for e in evidence) \
            or any((s or "").strip() for s in (comp.get("insights") or []))
        has_method = bool((comp.get("method") or "").strip())
        if not has_insight and not has_method:
            return False, f"已观察 {total} 次，但一次都没说出学到了什么（无 insight、无方法论正文）—— 纯重复不成技能"

        # 不强制"跨天"：一个下午把同一套流程跑 5 遍，本来就是同一类事。
        # 天数只作为描述信息，不作为门槛 —— 门槛是次数 × 价值。
        days = {str(e.get("at") or "")[:10] for e in evidence if e.get("at")}
        span = f"跨 {len(days)} 天" if len(days) > 1 else "同日多次"
        return True, f"已观察 {total} 次（{span}）、被纠正 {corrected} 次、有可迁移判断"


_ledger: Optional[CompetenceLedger] = None
_ledger_lock = threading.Lock()


def get_ledger() -> CompetenceLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = CompetenceLedger()
        return _ledger


def reset_ledger(path: Optional[Path] = None) -> CompetenceLedger:
    """测试用：换一个账本实例。"""
    global _ledger
    with _ledger_lock:
        _ledger = CompetenceLedger(path)
        return _ledger
