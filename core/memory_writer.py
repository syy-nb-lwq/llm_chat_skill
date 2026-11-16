"""用户画像写入管线 —— 长期记忆(L2)的主链写路径

分层(docs/13 §2.1):
- L1 短期记忆 = 会话内容,已经全量落库(`core/session_memory.py`)
- L2 长期记忆 = 用户画像(`preference`/`habit`/`method`/`constraint`/`fact`)

**为什么这里只写画像,不写 episode:**
会话内容已经全量保留并且可检索,再为每轮写一条 episode 记忆是重复存储。
长期记忆的价值在于"蒸馏出这个人是谁",所以这里只产出**可独立成立的画像陈述**。

管线(docs/13 §2.4):
    规则候选提取 → 敏感信息检查 → 去重/冲突检测 → 置信度门槛 → 入库 + 建索引
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from infra.logger import get_logger

#: 低于这个置信度的候选不入库
MIN_CONFIDENCE = 0.6

#: 画像类记忆类型
PROFILE_TYPES = ("preference", "habit", "method", "constraint", "fact")


# ===== 敏感信息 =====

#: 命中即拒绝入库(宁可漏记,不可泄漏)
_SENSITIVE_PATTERNS = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "身份证号"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "银行卡号"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "API Key"),
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=：]\s*\S+"), "密码"),
    (re.compile(r"(?i)\b(token|secret|secret_key)\s*[:=：]\s*\S+"), "密钥"),
    (re.compile(r"(?<!\d)\d{6}\s*(?:是|为)?\s*(?:我的)?(?:支付|交易|取款)?密码"), "支付密码"),
)


def find_sensitive(text: str) -> Optional[str]:
    """检查敏感信息,命中返回类别名,否则 None"""
    if not text:
        return None
    for pattern, label in _SENSITIVE_PATTERNS:
        if pattern.search(text):
            return label
    return None


# ===== 候选模型 =====

@dataclass
class MemoryCandidate:
    """一条待写入的画像候选"""
    type: str
    content: str
    confidence: float
    importance: float = 0.5
    scope: str = "user"
    normalized_key: str = ""
    rule: str = ""


# ===== 规则提取 =====

@dataclass(frozen=True)
class _Rule:
    """一条画像提取规则

    原则(docs/13 §2.8):**低频高准,宁缺毋滥**。
    只认用户明确表达"我怎么样/以后怎么/不要怎么"的句式。
    """
    type: str
    pattern: "re.Pattern[str]"
    confidence: float
    importance: float = 0.5
    name: str = ""
    #: 可选的前置条件:匹配到的文本还要满足这个判断才会被采纳。
    #: 用来挡住"句式对、但说的不是同一件事"的情况。
    guard: Optional[Any] = None


#: 「以后…」这类句式只有在**指向助手的行为/产出**时才算偏好。
#:
#: 「以后我们开会要提前十分钟」是用户自己的安排,不是"希望助手怎么做" ——
#: 把它记成 preference,就会往系统提示词里塞一条"用户偏好:提前开会",
#: 助手可能真的照着调整行为,这就是典型的长期记忆污染(docs/13 的"宁缺毋滥")。
#:
#: 代价是召回率会掉一点:「以后我每天早起」这类纯个人安排不再入库。
#: 这个取舍由 `tests/data/profile_eval.jsonl` 的基线量化,不是拍脑袋。
_ASSISTANT_FACING_RE = re.compile(
    r"回答|输出|回复|答案|答|写|生成|整理|汇报|报告|日报|周报|文档|代码|翻译|搜索|"
    r"解释|分析|总结|列举|列个|画图|绘制|做|用|按|保持|提供|转成|改成|"
    r"发布|提交|部署|上线|测试|评审|排查|定位|修复|复盘|需求|接口|日志|配置|数据|"
    r"跑|检查|给"
)


def _is_assistant_facing(content: str) -> bool:
    """这段话是不是在说「希望助手怎么做」"""
    return bool(_ASSISTANT_FACING_RE.search(content or ""))


#: 指代当前这一轮的说法 ——「我喜欢**这个**方案」是对眼前东西的反应,
#: 不是长期偏好。记进画像等于让助手长期背着一条没意义的约束。
_DEICTIC_RE = re.compile(r"^(?:这个|这份|这款|这种|这样|那个|那份|本次|刚才|刚刚)")

#: 显式偏好的前缀
_LIKE_PREFIX_RE = re.compile(
    r"^(?:我喜欢|我偏爱|我倾向于|我更希望|我更喜欢|我更愿意)"
)


def _is_durable_preference(content: str) -> bool:
    """「我喜欢这个方案」是**对当前这一轮**的反应,不是长期偏好。

    「我喜欢简洁的结论」是长期偏好,该记;
    「我喜欢这个方案」是当场的一句评价,不该进画像。
    """
    m = _LIKE_PREFIX_RE.match(content or "")
    if not m:
        return True
    return not _DEICTIC_RE.match((content[m.end():] or "").strip())


#: 虚词清洗表(构造 normalized_key 用)
_FILLER = (
    "以后", "今后", "从今以后", "每次", "都", "请", "一定要", "记得", "帮我",
    "我希望", "我想要", "麻烦", "我", "你", "的", "了", "吧", "呢", "啊", "呀",
    "，", ",", "。", ".", "!", "!", "?", "?", "、", " ", "\t", "\n",
)


def _skeleton(text: str) -> str:
    """把一句话压成"骨架",用于判断两条画像是否在说同一件事。

    例:"以后回答都用中文" 与 "回答请用中文" → 骨架都是 "回答用中文"。
    这是个启发式:它的作用只是让"用户改主意"能触发 supersede 而不是堆两条,
    压得不够准最多是多留一条记忆,不会错删。
    """
    s = text.strip()
    for token in _FILLER:
        s = s.replace(token, "")
    return s[:32]


#: 可枚举的「单槽」维度。
#:
#: 冲突检测的难点:用户说"以后回答都用中文",过两天说"以后回答都用英文" ——
#: 字面完全不同,但说的是**同一件事的两个版本**,必须让新的取代旧的,
#: 否则画像里会同时躺着两条互相矛盾的指令。
#:
#: 这里只对**能枚举清楚**的维度做槽位归并(语言/格式/长度/表情),
#: 其余情况退回字面骨架 —— 不能枚举的东西("不要用括号" vs "不要用分号")
#: 是不同的约束,混成一个槽会丢信息。
_SLOT_RULES: tuple = (
    ("output_language", re.compile(r"中文|英文|日文|日语|English|Chinese|english")),
    ("output_format", re.compile(
        r"三段式|要点式|列表|表格|分点|项目符号|[Mm]arkdown|markdown"
    )),
    ("output_length", re.compile(r"\d+\s*字|简短|精简|详细|简练|啰嗦")),
    ("emoji", re.compile(r"[Ee]moji|emoji|表情符号|表情包|颜文字")),
)


def _match_slot(content: str) -> str:
    """把内容归到某个单槽维度;归不上返回空串"""
    for slot, pattern in _SLOT_RULES:
        if pattern.search(content):
            return slot
    return ""


def _normalized_key(type_: str, content: str) -> str:
    """画像去重/冲突的键。

    优先按单槽维度归并(同一维度的新表述 = 改主意),
    归不上才退回字面骨架(不同表述 = 两条独立记忆)。
    """
    slot = _match_slot(content)
    if slot:
        return f"{type_}:slot:{slot}"
    return f"{type_}:{_skeleton(content)}"


#: 画像提取规则表
_RULES: tuple = (
    # ---- 雷区 constraint(用户明确禁止) ----
    _Rule(
        type="constraint",
        pattern=re.compile(
            r"(?:不要|别|禁止|不准|不许|不能)(?:再)?(?:用|使用|加|写|出现|给我)?\s*"
            r"[^\n,，。;；!?！？]{1,30}"
        ),
        confidence=0.8,
        importance=0.9,
        name="prohibition",
    ),
    # ---- 偏好 preference(显式偏好) ----
    _Rule(
        type="preference",
        pattern=re.compile(
            r"(?:以后|今后|以后都|以后请|从现在起)[^\n,，。;；!?！？]{2,40}"
        ),
        confidence=0.9,
        importance=0.85,
        name="future_preference",
        guard=_is_assistant_facing,
    ),
    _Rule(
        type="preference",
        pattern=re.compile(
            r"(?:我喜欢|我偏爱|我倾向于|我更希望|我更喜欢|我更愿意)[^\n,，。;；!?！？]{1,40}"
        ),
        confidence=0.75,
        importance=0.75,
        name="explicit_like",
        guard=_is_durable_preference,
    ),
    _Rule(
        type="preference",
        pattern=re.compile(
            r"(?:回答|输出|回复|答案)(?:请|要|必须)?(?:用|按|保持)[^\n,，。;；!?！？]{1,30}"
        ),
        confidence=0.7,
        importance=0.8,
        name="output_preference",
    ),
    # ---- 习惯 habit(重复出现的行为模式) ----
    _Rule(
        type="habit",
        pattern=re.compile(
            r"(?:我习惯|我通常|我一般|我一向|我平时|惯例上)[^\n,，。;；!?！？]{2,40}"
        ),
        confidence=0.8,
        importance=0.8,
        name="habit",
    ),
    _Rule(
        type="habit",
        pattern=re.compile(
            r"(?:每次|每逢|每当)[^\n,，。;；!?！？]{1,40}(?:都|就|会|先|要)[^\n,，。;；!?！？]{0,40}"
        ),
        confidence=0.7,
        importance=0.7,
        name="every_time",
    ),
    # ---- 方法论 method(用户给的做法/步骤) ----
    _Rule(
        type="method",
        pattern=re.compile(
            r"(?:应该先|要先|需要先|先)[^\n,，。;；!?！？]{1,30}"
            r"(?:再|然后|接着|之后)[^\n,，。;；!?！？]{1,40}"
        ),
        confidence=0.75,
        importance=0.85,
        name="step_order",
    ),
    _Rule(
        type="method",
        pattern=re.compile(
            r"(?:正确做法|标准做法|规范做法|最好的做法|流程是|步骤是|方法论是)"
            r"[^\n,，。;；!?！？]{2,60}"
        ),
        confidence=0.85,
        importance=0.9,
        name="explicit_method",
    ),
    # ---- 稳定事实 fact(用户是什么样的人) ----
    _Rule(
        type="fact",
        pattern=re.compile(
            r"(?:我是|我在|我做|我从事|我的职业是|我的岗位是)[^\n,，。;；!?！？]{2,40}"
        ),
        confidence=0.7,
        importance=0.7,
        name="self_fact",
    ),
)

#: 明确要求"记住"的句式 —— 置信度直接拉满
_EXPLICIT_REMEMBER = re.compile(
    r"(?:记住|记一下|记下|请记住|你要记住|帮我记住)[^\n。!?]{1,60}"
)

#: 疑问句/元问题不当成画像(用户在问,不是在陈述)
_QUESTION_TAIL = re.compile(r"(?:吗|呢|么|是不是|对不对|怎么样)\s*[?？]?\s*$")


def _is_statement(text: str) -> bool:
    """判断是不是"陈述/指令"而不是提问"""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.endswith(("?", "？")):
        return False
    return not _QUESTION_TAIL.search(stripped)


def extract_candidates(user_input: str) -> List[MemoryCandidate]:
    """从一句用户输入里提取画像候选(纯规则,0 次 LLM 调用)。

    宁可漏,不可错 —— 这是长期记忆写路径的基本要求:
    一条错误的"用户偏好"会持续影响此后每一轮。
    """
    if not user_input or not _is_statement(user_input):
        return []

    text = user_input.strip()
    candidates: List[MemoryCandidate] = []
    seen: set = set()
    #: (类型, 起点, 终点) —— 用于同类型重叠去重
    spans: List[Tuple[str, int, int]] = []

    def _add(
        type_: str,
        content: str,
        confidence: float,
        importance: float,
        rule: str,
        span: Optional[Tuple[int, int]] = None,
    ):
        content = content.strip(" ,，。.、;；")
        if len(content) < 4:
            return
        key = _normalized_key(type_, content)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            MemoryCandidate(
                type=type_,
                content=content,
                confidence=confidence,
                importance=importance,
                normalized_key=key,
                rule=rule,
            )
        )
        if span is not None:
            spans.append((type_, span[0], span[1]))

    # 显式"记住…":不管句式,直接最高置信
    for m in _EXPLICIT_REMEMBER.finditer(text):
        _add(
            "preference", m.group(0), 0.95, 0.9, "explicit_remember",
            span=(m.start(), m.end()),
        )

    # 规则表
    for rule in _RULES:
        for m in rule.pattern.finditer(text):
            if rule.guard is not None and not rule.guard(m.group(0)):
                continue
            _add(
                rule.type, m.group(0), rule.confidence, rule.importance, rule.name,
                span=(m.start(), m.end()),
            )

    return _drop_overlaps(candidates, spans)


def _drop_overlaps(
    candidates: List[MemoryCandidate],
    spans: List[Tuple[str, int, int]],
) -> List[MemoryCandidate]:
    """同类型、内容重叠 → 只留更完整的那条。

    例:"正确做法是先备份数据库再执行迁移" 会同时命中 `explicit_method`
    ("正确做法是先备份数据库再执行迁移") 和 `step_order`("先备份数据库再执行迁移")。
    两条内容几乎一样,存两条既浪费注入预算(画像≤400 字符),
    又会让同一件事在提示词里出现两遍。
    """
    if not spans or len(spans) < 2:
        return candidates

    # span 与 candidates 一一对应(spans 只在 _add 成功追加时增长,顺序一致)
    kept: List[int] = []
    for i, (_type, start, end) in enumerate(spans):
        covered = False
        for j, (other_type, o_start, o_end) in enumerate(spans):
            if i == j or other_type != _type:
                continue
            # 别人的范围完整包住我 → 我是更短的那条,丢掉
            if o_start <= start and o_end >= end and (o_end - o_start) > (end - start):
                covered = True
                break
        if not covered:
            kept.append(i)

    if len(kept) == len(candidates):
        return candidates
    # kept 里是 spans 的下标;candidates 与 spans 同序,直接按 spans 下标取
    return [candidates[i] for i in kept]


# ===== 写入器 =====

@dataclass
class WriteResult:
    """一次写入的结果(给日志和测试看)"""
    added: List[str] = field(default_factory=list)
    superseded: List[str] = field(default_factory=list)
    skipped_sensitive: List[str] = field(default_factory=list)
    skipped_low_confidence: List[str] = field(default_factory=list)
    skipped_duplicate: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.superseded)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": list(self.added),
            "superseded": list(self.superseded),
            "skipped_sensitive": list(self.skipped_sensitive),
            "skipped_low_confidence": list(self.skipped_low_confidence),
            "skipped_duplicate": list(self.skipped_duplicate),
        }


class MemoryWriter:
    """把用户画像写进长期记忆库

    调用方:`Agent.handle()` 末尾 —— 必须异步、不阻塞响应。
    """

    def __init__(self, semantic_memory=None):
        # 延迟取单例:测试里可以注入替身
        self._semantic_memory = semantic_memory
        self.logger = get_logger()

    @property
    def store(self):
        if self._semantic_memory is None:
            from core.semantic_memory import get_semantic_memory

            self._semantic_memory = get_semantic_memory()
        return self._semantic_memory

    async def record_turn(
        self,
        user_input: str,
        *,
        user_id: str,
        session_id: str = "",
        turn_id: str = "",
        tenant_id: str = "default",
    ) -> WriteResult:
        """一轮对话结束后抽取并写入画像。

        任何失败都只记日志、绝不抛出 —— 记忆写入不能影响本轮响应。
        """
        result = WriteResult()
        if not user_id or not user_input:
            return result

        try:
            candidates = extract_candidates(user_input)
        except Exception as exc:
            self.logger.warning("MemoryWriter", f"候选提取失败: {exc}")
            return result

        for candidate in candidates:
            try:
                await self._write_one(
                    candidate,
                    result=result,
                    user_id=user_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    tenant_id=tenant_id,
                )
            except Exception as exc:
                self.logger.warning(
                    "MemoryWriter",
                    f"写入画像失败({candidate.type}): {exc}",
                )

        if result.total:
            self.logger.info(
                "MemoryWriter",
                f"画像写入: 新增 {len(result.added)} 条,"
                f"取代 {len(result.superseded)} 条",
            )
        return result

    async def _write_one(
        self,
        candidate: MemoryCandidate,
        *,
        result: WriteResult,
        user_id: str,
        session_id: str,
        turn_id: str,
        tenant_id: str,
    ) -> None:
        # 1) 敏感信息检查
        sensitive = find_sensitive(candidate.content)
        if sensitive:
            result.skipped_sensitive.append(f"{candidate.type}:{sensitive}")
            self.logger.warning(
                "MemoryWriter", f"拒绝写入疑似{sensitive}的内容(不入库)"
            )
            return

        # 2) 置信度门槛
        if candidate.confidence < MIN_CONFIDENCE:
            result.skipped_low_confidence.append(candidate.content)
            return

        store = self.store
        db = store.memory_db

        # 3) 去重 / 冲突检测
        existing = db.get_by_key(user_id, candidate.type, candidate.normalized_key)
        if existing is not None:
            if _skeleton(existing.content) == _skeleton(candidate.content):
                result.skipped_duplicate.append(candidate.content)
                return
            # 同一件事说了新版本 → 新者胜,旧者标记被取代
            embedding = await self._embed(candidate.content)
            new_entry = db.add(
                type=candidate.type,
                content=candidate.content,
                embedding=embedding,
                user_id=user_id,
                tenant_id=tenant_id,
                scope=candidate.scope,
                confidence=candidate.confidence,
                importance=candidate.importance,
                supersedes_id=existing.id,
                source_session_id=session_id,
                source_turn_id=turn_id,
                normalized_key=candidate.normalized_key,
                metadata={"rule": candidate.rule},
            )
            db.supersede(existing.id, new_entry.id)
            result.superseded.append(new_entry.id)
            return

        # 4) 入库 + 建索引
        embedding = await self._embed(candidate.content)
        entry = db.add(
            type=candidate.type,
            content=candidate.content,
            embedding=embedding,
            user_id=user_id,
            tenant_id=tenant_id,
            scope=candidate.scope,
            confidence=candidate.confidence,
            importance=candidate.importance,
            source_session_id=session_id,
            source_turn_id=turn_id,
            normalized_key=candidate.normalized_key,
            metadata={"rule": candidate.rule},
        )
        result.added.append(entry.id)

    async def _embed(self, content: str) -> Optional[List[float]]:
        """生成向量;失败降级为无向量(FTS 仍可用)"""
        service = getattr(self.store, "embedding_service", None)
        if service is None:
            return None
        try:
            return await service.embed(content)
        except Exception as exc:
            self.logger.warning("MemoryWriter", f"生成嵌入向量失败(降级无向量): {exc}")
            return None


# ---- 全局单例 ----

_writer: Optional[MemoryWriter] = None


def get_memory_writer() -> MemoryWriter:
    global _writer
    if _writer is None:
        _writer = MemoryWriter()
    return _writer


def reset_memory_writer() -> None:
    global _writer
    _writer = None
