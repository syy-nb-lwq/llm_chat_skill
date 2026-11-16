"""意图评估集(双通道)。

- **规则通道(L0)**:CI 必跑。只测 L0 守卫层能定案的用例。
- **LLM 通道(L1)**:需要真实 API,`pytest --live` 才跑。

评估集:`tests/data/intent_eval.jsonl`,每行:
```json
{"input": "...", "label": "task", "layer": "L0"|"L1", "note": "可选"}
```

跑法:
```bash
pytest tests/test_intent_eval.py -q              # 只跑规则通道
pytest tests/test_intent_eval.py -q --live       # 加跑 LLM 通道(需真实 key)
```

指标目标(见 `docs/12` §4):
| 指标 | 目标 |
|---|---|
| L0 精确率 | 100%(白名单+结构规则,出现误判即 bug) |
| L1 总体准确率 | ≥ 90% |
| 易混对(manage/task、teach/task)准确率 | ≥ 95% |
"""
import json
from pathlib import Path

import pytest

from core.intent_detector import IntentDetector
from core.intent_labels import IntentLabel


EVAL_FILE = Path(__file__).parent / "data" / "intent_eval.jsonl"
HOLDOUT_FILE = Path(__file__).parent / "data" / "intent_eval_holdout.jsonl"
HOLDOUT2_FILE = Path(__file__).parent / "data" / "intent_eval_holdout2.jsonl"


def load_cases(path=None):
    path = path or EVAL_FILE
    cases = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cases.append(json.loads(line))
    return cases


ALL_CASES = load_cases()
L0_CASES = [c for c in ALL_CASES if c.get("layer") == "L0"]
L1_CASES = [c for c in ALL_CASES if c.get("layer") == "L1"]

# 留出集:**改完提示词之后**才写的用例。
# 它不参与调参,只用来检验"这次改提示词是学到了边界,还是只是背下了句子"。
# 如果留出集掉分,说明改提示词过拟合了。
HOLDOUT_CASES = load_cases(HOLDOUT_FILE) if HOLDOUT_FILE.exists() else []
HOLDOUT2_CASES = load_cases(HOLDOUT2_FILE) if HOLDOUT2_FILE.exists() else []

# 易混对:历史上互相误判过的组合
CONFUSABLE_PAIRS = {("manage", "task"), ("task", "manage"), ("teach", "task"), ("task", "teach")}

# 参与易混判定的标签集合。
#
# **这里原来写错过**:用 (predicted, expected) 是否落在 CONFUSABLE_PAIRS 里来计数,
# 而那 4 个二元组里的两个标签**必然不同** —— 于是"判对"的用例一次都进不了分母,
# confusable_correct 永远是 0,指标变成了"一次易混错都不能有"。
# 正确口径:**按期望标签**判断这条用例是否属于易混簇,再算这个簇上的准确率。
CONFUSABLE_LABELS = {"manage", "task", "teach"}


def test_eval_file_is_sane():
    assert len(ALL_CASES) >= 20
    valid = {l.value for l in IntentLabel}
    for case in ALL_CASES:
        assert case["label"] in valid, f"非法标签: {case}"
        assert case.get("layer") in ("L0", "L1"), f"非法层: {case}"
        assert case["input"].strip()


# ===== 结构守卫(CI 必跑) =====
#
# 旧版这里是 `test_l0_rule_channel`:断言 L0 规则能将评估集里
# 标为 layer="L0" 的用例定下来。**那套规则已删除**。
#
# 为什么:真机基线(130 条)量出来——
#   规则 + LLM = 94.7%；只有 LLM = 97.7%。
# 规则不是没帮上忙,是在拖低准确率。
#
# 现在意图唯一由 L1 判;`layer` 字段只作为**历史标注**保留,
# 不再决定路由。

def test_guard_decides_no_intent_cases():
    """守卫**不该**对评估集里的任何非空输入定案。

    它只能判"不需要语义就能确定"的事(空输入)。
    一旦它开始判语义题,就是后悔率的来源。
    """
    decided = [
        (c["input"], IntentDetector().detect(c["input"]).label)
        for c in ALL_CASES
        if IntentDetector().detect(c["input"]).label is not None
    ]
    assert not decided, f"守卫又开始判意图了: {decided}"


def test_all_cases_reach_l1():
    """所有非空输入都必须落到 L1——否则 L1 通道没被完整测到。"""
    reached = [
        c for c in ALL_CASES
        if IntentDetector().detect(c["input"]).label is None
    ]
    assert len(reached) == len(ALL_CASES), (
        f"只有 {len(reached)}/{len(ALL_CASES)} 条进入 L1"
    )


def test_confusable_pairs_are_covered():
    """易混对必须在评估集里被覆盖 —— 否则等于没测。

    判据:每一对易混标签,两个方向都至少有一条用例。
    """
    labels = {c["label"] for c in ALL_CASES}
    for a, b in sorted(CONFUSABLE_PAIRS):
        assert a in labels, f"评估集缺少 {a} 用例"
        assert b in labels, f"评估集缺少 {b} 用例"


def test_eval_set_covers_all_labels():
    """6 个标签都要有用例,否则准确率没意义。"""
    labels = {c["label"] for c in ALL_CASES}
    missing = {l.value for l in IntentLabel} - labels
    assert not missing, f"评估集缺少标签: {missing}"


# ===== LLM 通道(nightly,需要真实 key) =====

@pytest.mark.asyncio
async def test_l1_llm_channel(live_enabled):
    """L1 闭集分类器在评估集上的准确率。"""
    from core.llm_judge import L1Classifier

    classifier = L1Classifier()
    correct = 0
    confusable_total = 0
    confusable_correct = 0
    details = []

    for case in L1_CASES:
        decision = await classifier.classify(case["input"])
        if decision is None:
            details.append(f"{case['input']!r}: L1 无输出")
            continue
        predicted = decision.label.value
        if predicted == case["label"]:
            correct += 1
        else:
            details.append(
                f"{case['input']!r}: 期望 {case['label']},实际 {predicted} "
                f"(conf={decision.confidence:.2f}, reason={decision.reason})"
            )
        if case["label"] in CONFUSABLE_LABELS:
            confusable_total += 1
            if predicted == case["label"]:
                confusable_correct += 1

    total = len(L1_CASES)
    accuracy = correct / total if total else 0.0
    print(f"\nL1 准确率: {correct}/{total} = {accuracy:.1%}")
    for d in details:
        print("  ", d)
    assert accuracy >= 0.90, f"L1 准确率 {accuracy:.1%} < 90%:\n" + "\n".join(details)

    if confusable_total:
        conf_accuracy = confusable_correct / confusable_total
        print(f"易混对准确率: {confusable_correct}/{confusable_total} = {conf_accuracy:.1%}")
        assert conf_accuracy >= 0.95, f"易混对准确率 {conf_accuracy:.1%} < 95%"


@pytest.mark.asyncio
async def test_l1_holdout_channel(live_enabled):
    """留出集:改完提示词之后才写的用例,用来检验泛化。

    **这条测试不打开发集** —— 它是"防过拟合"的闸门:
    开发集涨分但留出集没涨,说明改的是提示词里的句子,不是判据本身。
    """
    from core.llm_judge import L1Classifier

    if not HOLDOUT_CASES:
        pytest.skip("没有留出集")

    classifier = L1Classifier()
    correct = 0
    details = []

    for case in HOLDOUT_CASES:
        decision = await classifier.classify(case["input"])
        if decision is None:
            details.append(f"{case['input']!r}: L1 无输出")
            continue
        predicted = decision.label.value
        if predicted == case["label"]:
            correct += 1
        else:
            details.append(
                f"{case['input']!r}: 期望 {case['label']},实际 {predicted} "
                f"(conf={decision.confidence:.2f}, reason={decision.reason})"
            )

    total = len(HOLDOUT_CASES)
    accuracy = correct / total if total else 0.0
    print(f"\nL1 留出集准确率: {correct}/{total} = {accuracy:.1%}")
    for d in details:
        print("  ", d)
    assert accuracy >= 0.80, f"L1 留出集准确率 {accuracy:.1%} < 80%:\n" + "\n".join(details)


@pytest.mark.asyncio
async def test_l1_holdout2_channel(live_enabled):
    """第二批留出集 —— 又在改完提示词之后才写。

    第一批留出集暴露了一个标注错误(「教我」的方向),改完提示词后
    再写第二批,继续检验"改的是判据,不是句子"。
    """
    from core.llm_judge import L1Classifier

    if not HOLDOUT2_CASES:
        pytest.skip("没有第二批留出集")

    classifier = L1Classifier()
    correct = 0
    details = []

    for case in HOLDOUT2_CASES:
        decision = await classifier.classify(case["input"])
        if decision is None:
            details.append(f"{case['input']!r}: L1 无输出")
            continue
        predicted = decision.label.value
        if predicted == case["label"]:
            correct += 1
        else:
            details.append(
                f"{case['input']!r}: 期望 {case['label']},实际 {predicted} "
                f"(conf={decision.confidence:.2f}, reason={decision.reason})"
            )

    total = len(HOLDOUT2_CASES)
    accuracy = correct / total if total else 0.0
    print(f"\nL1 留出集2准确率: {correct}/{total} = {accuracy:.1%}")
    for d in details:
        print("  ", d)
    assert accuracy >= 0.80, f"L1 留出集2准确率 {accuracy:.1%} < 80%:\n" + "\n".join(details)
