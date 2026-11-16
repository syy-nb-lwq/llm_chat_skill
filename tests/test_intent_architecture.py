"""意图判定的**架构契约** —— 别再退回"用规则猜用户怎么说"。

## 这个文件在守什么

真机基线(130 条标注用例,`deepseek-flash`)量出来的事实:

| 做法 | 准确率 |
|---|---|
| 规则(L0)+ LLM(L1) | **94.7%** |
| 只有 LLM | **97.7%** |

规则**不是没帮上忙,是在拖低准确率**。所以现在的契约是:

1. **意图的唯一裁决者是 L1**(一次 LLM 调用,6 标签闭集)。
2. **结构守卫只允许判"不需要语义就能确定"的事** —— 目前只有空输入。
3. **不允许任何"猜用户会怎么说"的规则**回到意图判定里。

这个文件替换了原来的 `test_intent_precision_p0.py` ——
那个文件断言的是"『教我…』要判成 teach"这类**字面**行为,
而字面恰恰是错的:「教我怎么用这个工具」和「教我一种排查方法」
字面都含「教我」,语义相反。
"""
import pytest

from core.intent_detector import IntentCategory, IntentDetector


# ============================================================
# 1. 结构守卫:只判结构事实,不判意图
# ============================================================

@pytest.mark.parametrize("text", [
    "", " ", "\n", "\t  \n ",
])
def test_guard_decides_empty_input(text):
    """空输入 → 澄清。这是结构事实,不需要理解语义。"""
    result = IntentDetector().detect(text)
    assert result.category == IntentCategory.CLARIFY
    assert result.need_llm is False


@pytest.mark.parametrize("text", [
    # 以前会被规则判成 chitchat 的
    "你好", "谢谢", "再见", "好的", "你是谁", "hello",
    # 以前会被规则判成 manage 的
    "列出所有技能", "查看技能列表", "把版本回滚到 1.0.0", "show me skills",
    # 以前会被规则判成 teach 的
    "教我一种排查线上问题的方法", "记住:以后做日报按三段式来",
    "以后每次发布前都要先跑一遍回归", "教你一个技能",
    # 以前会被规则判成 retry 的
    "重新来一次", "再说一遍", "再执行一次刚才那个查询",
    # 以前会被规则判成 task 的
    "查一下厦门今天的天气", "算一下 128*37", "把这段英文翻译成中文",
    # 以前会被规则抢走并且判错的
    "教我怎么用这个工具", "教我怎么看K线图", "以后别再用那种语气跟我说话了",
])
def test_guard_does_not_judge_intent(text):
    """**守卫不许判这些** —— 它们全是语义问题,必须交给 L1。

    「你好」看起来"简单明显",但"明显"不是判据:
    真机基线里规则正是靠"看起来明显"把 10 条判错的。
    """
    result = IntentDetector().detect(text)
    assert result.category == IntentCategory.UNKNOWN
    assert result.label is None, f"{text!r} 被守卫判成了 {result.label}"
    assert result.need_llm is True


def test_guard_contract_returns_result_not_none():
    """守卫"没定案"要返回 UNKNOWN 结果,不是 None。

    返回 None 会让所有 `result.category` 的调用方炸掉,
    而且分不清"没判"和"调用失败"。
    """
    result = IntentDetector().detect("随便一句话")
    assert result is not None
    assert result.category == IntentCategory.UNKNOWN


# ============================================================
# 2. 架构契约:意图只有一个裁决者
# ============================================================

def test_no_intent_rule_api_on_detector():
    """守卫模块不再导出任何意图判定函数/规则表。

    这些名字以前存在:L0 和 ManagerAgent 各拿一份去判意图,
    两处判定必然漂移。删掉它们是为了让"想加规则"变得费劲。
    """
    import core.intent_detector as mod

    for gone in (
        "is_teach_intent",
        "is_manage_intent",
        "TEACH_PATTERNS",
        "TEACH_STANDING_RE",
        "TEACH_EXCLUDE_RE",
        "MANAGEMENT_PATTERNS",
    ):
        assert not hasattr(mod, gone), (
            f"`core.intent_detector.{gone}` 又回来了 —— "
            f"规则判意图会把准确率从 97.7% 拖回 94.7%"
        )


def test_manager_has_single_intent_judge():
    """ManagerAgent 不再有规则式的意图判断方法。"""
    from agents.manager import ManagerAgent

    for gone in ("should_learn_skill", "should_answer_directly"):
        assert not hasattr(ManagerAgent, gone), (
            f"`ManagerAgent.{gone}` 又回来了 —— 意图请用 L1 的标签"
        )


@pytest.mark.asyncio
async def test_manager_routes_by_l1_label_not_rules(stub_intent):
    """同一句输入,改 L1 的判定结果 → 路由跟着变。

    这证明意图真的由 L1 决定,而不是被任何规则抢先。
    """
    from agents.manager import IntentType, ManagerAgent

    manager = ManagerAgent()

    stub_intent("chitchat")
    plan = await manager.plan("教我怎么用这个工具")
    assert plan.intent == IntentType.CHITCHAT

    stub_intent("task")
    plan = await manager.plan("教我怎么用这个工具")
    assert plan.intent != IntentType.CHITCHAT
