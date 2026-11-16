"""意图闭集 —— L1 判别层的契约。

设计要点(见 `docs/12-意图分层识别方案.md` §2.2):

- 路由层只有 **6 个标签**。它们与下游路由一一对应,不再有两套枚举。
- `weather / search / calculate / translate / code / custom` 这些
  **不是意图,是任务子类**(TaskKind)——它们属于 L2 规划层的结果,
  不该和 `chitchat` 并列比较(原 `IntentCategory` 把两者混在一起,导致
  "查看北京天气" 只能和 "你好" 比相似度)。
- 闭集定义 + 正例 + 易混反例直接进提示词:LLM 看到的是**判据**,
  不是让它自由发挥一个字符串。

`IntentLabel` 是全系统唯一的"意图"枚举;`core/intent_detector.py` 里
的 `IntentCategory` 降级为 **L0 规则明细**(规则命中到哪一条),
不再承担路由职责。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional


class IntentLabel(Enum):
    """L1 闭集:6 个路由标签"""

    CHITCHAT = "chitchat"   # 寒暄/确认/感谢/否定/询问身份
    TEACH = "teach"         # 用户要教新技能/方法论
    MANAGE = "manage"       # 技能管理(列出/版本/回滚/启停)
    RETRY = "retry"         # 重做上一轮
    TASK = "task"           # 需要工具/技能完成
    CLARIFY = "clarify"     # 意图不明或缺关键信息


class TaskKind(Enum):
    """L2 规划结果的任务子类(不是意图,只在规划后作为标签存在)"""

    WEATHER = "weather"
    SEARCH = "search"
    CALCULATE = "calculate"
    TRANSLATE = "translate"
    CODE = "code"
    CUSTOM = "custom"


# ===== 闭集定义(直接进提示词) =====

LABEL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "label": "chitchat",
        "definition": (
            "寒暄、问候、感谢、确认、否定、告别、询问助手身份,"
            "以及**不需要任何工具的闲聊型请求**(讲个笑话、讲个段子、随便聊聊)。"
            "共同点:不需要调用任何工具。"
            "**还包括「输出偏好」**:希望助手用什么语言/格式/语气回答,"
            "或不要 emoji/敬语/免责声明/那种语气 —— 这是对**回答形式**的偏好,"
            "**不是教助手做事的方法**,所以是 chitchat 而不是 teach。"
        ),
        "examples": ["你好", "谢谢", "好的", "你是谁", "再见", "讲个冷笑话"],
        "confusable": [
            "「查看北京天气」是 task,不是 chitchat",
            "「好的,帮我查天气」整体是 task(前半句是确认)",
            "「讲个冷笑话」「给我讲个段子」是 chitchat —— 不需要任何工具的闲聊型请求,不是 task",
            "「以后回答都用中文」「以后回复请用要点式」「不要用 emoji」"
            "「以后别再用那种语气跟我说话了」是**输出偏好** → chitchat,不是 teach",
        ],
    },
    {
        "label": "teach",
        "definition": (
            "用户要教助手一套可复用的方法论/技能/流程,要求助手记住并在以后按此执行。"
            "关键特征:出现「方法/步骤/原则/流程/规范」这类方法论载体,"
            "并且是「教我 / 记住 / 以后按…来 / 每次都…」这种约定语气。"
            "**边界:教的是「做事的方法」(怎么做一件事),不是「说话的方式」(怎么回答)。**"
            "回答用什么语言、什么格式、什么语气、不要 emoji/敬语/免责声明 —— "
            "这些是**输出偏好**,不是方法论 → chitchat。"
            "**注意方向:必须是「用户教助手」(助手要记住、以后照着做)。"
            "如果是「助手教用户」(「教我…怎么用…」「教我怎么看…」),那是 task。**"
        ),
        "examples": [
            "教我一种排查线上问题的方法",
            "记住:以后做日报按三段式来",
            "以后每次发布前都要先跑一遍回归",
        ],
        "confusable": [
            "「遇到雨天要带伞」是 task/chitchat,不是 teach(这是用户说自己的事,不是教助手)",
            "「以后我们开会要提前十分钟」不是 teach(用户自己的安排)",
            "「下次记得提醒我交房租」不是 teach(请求动作,不是方法论)",
            "「以后回答都用中文」「以后回复请用要点式」「不要用 emoji」"
            "「以后别再用那种语气跟我说话了」是**说话方式/输出偏好**,"
            "不是做事的方法 → chitchat",
            "方向相反的一对:「教我一种排查线上问题的方法」= 用户教助手 → teach;"
            "「教我怎么用这个工具」「教我怎么看 K 线图」= 助手教用户 → task",
        ],
    },
    {
        "label": "manage",
        "definition": (
            "对**技能**本身的管理操作:列出有哪些技能、看某个技能的版本/详情、"
            "切换版本、回滚、启用/禁用、删除、重命名。"
            "关键特征:管理动词 + 技能这个对象。"
            "**怎么算「对象」**:① **专属动词**(列出/回滚/切换/启停/删除/重命名)"
            "在本产品里只能作用于技能,没明说对象也算 manage;"
            "② **泛用动词**(查看/看看/有没有)必须**明说「技能」**才算 manage,"
            "没明说对象就按 task 处理。"
        ),
        "examples": [
            "列出所有技能",
            "把版本回滚到 1.0.0",
            "技能列表给我看看",
            "show me skills",
        ],
        "confusable": [
            "「查看北京天气」不是 manage(没有「技能」这个对象)",
            "「查看日的发布状态」不是 manage(对象不是技能)",
            "「顺便看看昨天那个版本号」没说「技能」,且「看看」是泛用动词 → task,不是 manage",
            "「回滚到上一个版本」没说「技能」,但「回滚」是只能作用于技能的专属动词 → manage",
        ],
    },
    {
        "label": "retry",
        "definition": (
            "用户要求把上一轮的动作重做一遍:「重新 / 再来一次 / 再说一遍 / "
            "重跑 / 再执行一次」。"
        ),
        "examples": ["重新来一次", "再说一遍", "再执行一次刚才那个查询"],
        "confusable": [
            "「重新开始一个新话题」偏 chitchat,不是 retry",
        ],
    },
    {
        "label": "task",
        "definition": (
            "需要助手查数据、算数、翻译、搜资料、写代码,或调用某个技能去完成一件事。"
            "注意:**具体用哪个工具由后续规划层决定**,这里只判断「这是不是一个要动手的任务」。"
        ),
        "examples": [
            "查一下厦门今天的天气",
            "帮我搜一下最新的 AI 政策",
            "把这段英文翻译成中文",
            "算一下 128*37 等于多少",
        ],
        "confusable": [
            "「什么是人工智能」是 task(要搜资料),不是 chitchat",
            "「搜索是一个动词吗」是 chitchat —— 在讨论词本身,不是要搜索",
            "「帮我看下今天的舆情」这种「帮我 + 动词」但对象含糊的,仍标 task —— 方向清楚,只是细节缺",
            "「教我怎么用这个工具」「教我怎么看 K 线图」= **助手要输出讲解** → task;"
            "不是 teach(teach 是用户反过来教助手一套做法)",
        ],
    },
    {
        "label": "clarify",
        "definition": (
            "用户的话**连大致方向都猜不出来**(完全看不出要做什么),"
            "或者缺少做这件事所必需的关键信息。"
            "只有在**真的无法判断方向**时才用这个标签;宁可标成 task 走后续流程,"
            "也不要因为含糊就放弃回答。"
            "**注意:口语化、语气词、对象指代不明,都不等于意图不明 —— "
            "只要能猜出一个大致方向,就标 task。**"
        ),
        "examples": ["那个事", "你懂我意思吧", "就那样"],
        "confusable": [
            "凡是还能猜出一个大致方向的,都标成 task —— 漏答比多问一句更糟",
            "「帮我看下今天的情况呗」虽然对象含糊,但方向明确(查东西)→ task,不是 clarify",
            "「这个方案太热了」只是对某物的评价,看不出要做什么 → clarify",
        ],
    },
]


# ===== L1 输出契约 =====

INTENT_RESULT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "enum": [label.value for label in IntentLabel],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "slots": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "date": {"type": "string"},
                "target_skill": {"type": "string"},
                "referent": {"type": "string"},
            },
        },
        "needs_clarify": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["label", "confidence", "reason"],
}


# ===== 提示词 =====

_SYSTEM_PROMPT = """你是一个意图分类器。把用户输入归到下面 6 个标签之一,并给出置信度。

你**只做分类**:不选工具、不选技能、不规划步骤、不回答问题。

## 判断顺序(从上到下走,命中就停,别再往下想)

1. **retry** —— 用户要重做上一轮:「重新 / 再来一次 / 再说一遍 / 重跑 / 再执行一次」。

2. **manage** —— 对**技能**的管理:
   - 专属动词(列出 / 回滚 / 切换 / 启停 / 删除 / 重命名)→ 本产品里只能作用于技能,没明说对象也算
   - 泛用动词(查看 / 看看 / 有没有)+ **明说「技能」**
   两类都不是 → 不是 manage。

3. **teach** —— **用户要教助手一套做事的办法**,助手要记住、以后照着做。
   **两条硬门槛,必须同时满足:**
   - **门槛 A(方向)**:是「用户 → 助手」,助手要记住。
     如果用户是要**助手给他讲解**(「教我怎么看…」「教我如何用…」)→ 是 **task**,不是 teach。
   - **门槛 B(内容)**:内容是**做事的流程/方法**(怎么查、怎么做、怎么排障),
     不是**回答的形式**(用什么语言、什么格式、什么语气、不要 emoji/敬语/免责声明)。
     后者 → 是 **chitchat**,不是 teach。
   任何一条不满足 → **不是 teach**。

4. **task** —— 助手要做一件事:查数据 / 算数 / 翻译 / 搜资料 / 写代码 / 调用技能 /
   **输出一段讲解**。「什么是X」「教我怎么看X」「教我如何用X」「翻译成英文」都在这里。
   **对象含糊也算 task**(「帮我看下今天的情况呗」「顺便看看昨天那个版本号」)——
   宁可 task 也不要 clarify。

5. **chitchat** —— 不需要任何工具:
   - 寒暄 / 感谢 / 确认 / 否定 / 告别 / 询问助手身份
   - 闲聊型请求(讲个笑话、讲个段子、随便聊聊)
   - **对回答形式的偏好**(用什么语言、什么语气、不要 emoji/敬语/免责声明)

6. **clarify** —— **连大致方向都猜不出来**。
   口语化、语气词、对象指代不明,**都不算** clarify。

## 标签定义(细节)
{definitions}

## 易混边界(重点看)
{confusables}

## 输出要求
1. `label` 必须严格从下面 6 个值里选(括号里是判断依据,选之前先看一眼):
   - `chitchat` = 不需要任何工具的闲聊,**或对回答形式的偏好**(语言/格式/语气)
   - `teach` = **用户**教**助手**一套做事的办法(助手要记住、以后照着做)
   - `manage` = 对**技能**做管理操作(列出/回滚/切换/启停/删除)
   - `retry` = 重做上一轮
   - `task` = **助手**要做一件事(查数据/算数/翻译/搜资料/写代码/**输出一段讲解**)
   - `clarify` = 连用户想干什么都猜不出来
   不要发明新标签,不要用近义词。
   **注意 `teach` 和 `task` 的方向相反**:
   `teach` 是**用户当老师**(助手要记住);
   `task` 是**助手当老师**(助手要输出讲解给别人听)。
   「教我怎么看K线图」「教我怎么用这个工具」= 助手当老师 → **task**。
2. `confidence` 反映你的把握程度,不是"答案有多重要":
   - 0.9 以上 = 几乎确定
   - 0.75~0.9 = 较有把握
   - 0.75 以下 = 分类本身拿不准。
     **这不代表要标 clarify** —— 「我分不清这两个标签」是**把握程度**问题,
     clarify 是「**用户想做什么**不明」问题,两回事。
     拿不准的时候,先问自己"我能猜出用户大致要干什么吗":
     能猜出 → 选最接近的标签(task/chitchat),拿不准就标 task;
     完全猜不出 → 才标 clarify。
3. `reason` 一句话说明依据,不要复述输入。
4. 不确定时给低 confidence,**不要**给一个高 confidence 的猜测。
5. **只输出一个 JSON 对象**,不要输出任何解释性文字、不要用 markdown 代码块。

输出格式(**注意字段顺序:先写 `reason`,再写 `label`**):
{{"reason": "先写依据:用户是想让谁做什么", "label": "<上述枚举之一>", "confidence": <0~1 的数字>, "slots": {{"city": "可选", "date": "可选", "target_skill": "可选", "referent": "可选"}}, "needs_clarify": false}}

**为什么顺序重要**:先把依据写出来,再选标签。
`label` 是**依据的结论**,不是凭语感直接蹦出来的第一个词 ——
输入里出现了「教」「以后」「版本」这类字眼,不代表就该选同名标签。
"""


def build_definitions_block() -> str:
    """把闭集定义渲染成提示词片段。"""
    lines = []
    for spec in LABEL_DEFINITIONS:
        lines.append(f"- {spec['label']}: {spec['definition']}")
        lines.append(f"  正例: {'; '.join(spec['examples'])}")
        for c in spec["confusable"]:
            lines.append(f"  易混: {c}")
    return "\n".join(lines)


def build_confusables_block() -> str:
    """把所有易混反例汇成一段,让 LLM 看到跨标签的边界。"""
    lines = []
    for spec in LABEL_DEFINITIONS:
        for c in spec["confusable"]:
            lines.append(f"- {c}")
    return "\n".join(lines)


def build_system_prompt() -> str:
    """构造 L1 的系统提示词:闭集定义 + 正例 + 易混边界 + 输出要求。"""
    return _SYSTEM_PROMPT.format(
        definitions=build_definitions_block(),
        confusables=build_confusables_block(),
    )


def build_intent_prompt(
    user_input: str,
    recent_turns: Optional[List[Dict[str, str]]] = None,
    profile: str = "",
) -> str:
    """构造 L1 的**用户**提示词(只放待判内容;判据在系统提示词里)。

    Args:
        user_input: 本轮用户输入
        recent_turns: 最近几轮对话(可空)
        profile:     召回的用户画像摘要(可空;长期记忆落地后才有)
    """
    parts = []
    if profile:
        parts.append(f"关于这个用户的已知信息(可能影响判断):\n{profile}")
    if recent_turns:
        history = []
        for turn in recent_turns:
            role = "用户" if turn.get("role") == "user" else "助手"
            content = (turn.get("content") or "").strip()
            if content:
                history.append(f"- {role}: {content}")
        if history:
            parts.append("最近对话:\n" + "\n".join(history))
    parts.append(f"请分类下面这句话:\n{user_input}")
    return "\n\n".join(parts)


def parse_label(value: Any) -> Optional[IntentLabel]:
    """把 LLM 返回的字符串解析成 `IntentLabel`,非法值返回 None。"""
    if isinstance(value, IntentLabel):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    for label in IntentLabel:
        if label.value == text:
            return label
    return None


def label_to_task_kind(label: Optional[IntentLabel]) -> Optional[TaskKind]:
    """意图标签 → 任务子类。

    **只有 `task` 才有任务子类**;具体子类(天气/搜索/计算…)由 L2 规划层决定,
    L1 不细分(D1 决策),所以这里只能给出最粗的 `custom`。
    """
    if label is not IntentLabel.TASK:
        return None
    return TaskKind.CUSTOM
