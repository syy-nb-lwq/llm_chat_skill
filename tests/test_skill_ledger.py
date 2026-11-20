"""能力档案账本 —— 技能晋升之前那一段的规则。

## 为什么单独测它

技能的数量失控，根子不在提示词，在**没有中间层**：
每完成一次任务就直接产一条技能，系统里没有「这是第几次做同类事」的概念。
账本就是那一层，所以它的判定规则必须钉死：

- 3 次才够格（2 次可能只是同一批工作的两半）
- 被纠正过一次，就要多攒 2 次
- 不同能力不能互相计数
"""
from core.skill_ledger import (
    PROMOTE_MIN_EVIDENCE,
    CompetenceLedger,
    normalize_key,
)


def test_normalize_key_accepts_snake_case():
    assert normalize_key("Short-Drama Video") == "short_drama_video"
    assert normalize_key("ecom_listing_batch") == "ecom_listing_batch"


def test_normalize_key_rejects_chinese_and_short():
    """中文 key 做索引/日志都不方便，而且模型给的英文 key 更稳定。"""
    assert normalize_key("竞品调研") == ""
    assert normalize_key("ab") == ""
    assert normalize_key("") == ""


def test_record_accumulates_evidence(tmp_path):
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    for i in range(3):
        ledger.record("ecom_listing_batch", user_input=f"第 {i} 次", tools=["a", "b"])
    comp = ledger.get("ecom_listing_batch")
    assert len(comp["evidence"]) == 3
    assert comp["tools"] == ["a", "b"]


def test_readiness_needs_three_observations(tmp_path):
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    # 带 method：真实沉淀里一定会有正文，否则价值闸门会先拦下（另有用例覆盖）
    kw = {"method": "若库存不足就先补货，避免超卖"}
    ledger.record("ecom_listing_batch", **kw)
    assert ledger.readiness(ledger.get("ecom_listing_batch"))[0] is False
    ledger.record("ecom_listing_batch", **kw)
    assert ledger.readiness(ledger.get("ecom_listing_batch"))[0] is False
    ledger.record("ecom_listing_batch", **kw)
    ready, why = ledger.readiness(ledger.get("ecom_listing_batch"))
    assert ready is True, why


def test_correction_raises_the_bar(tmp_path):
    """被纠正说明做法错过 —— 不能靠次数硬堆过去。

    1 次纠正 → 门槛从 3 次提到 3+2=5 次。
    """
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    kw = {"method": "若库存不足就先补货，避免超卖"}
    ledger.record("ecom_listing_batch", **kw)
    ledger.record("ecom_listing_batch", corrected=True, **kw)

    for _ in range(2):  # 共 4 次，还差一次
        ledger.record("ecom_listing_batch", **kw)
    ready, why = ledger.readiness(ledger.get("ecom_listing_batch"))
    assert ready is False
    assert "纠正" in why

    ledger.record("ecom_listing_batch", **kw)  # 第 5 次
    ready, _ = ledger.readiness(ledger.get("ecom_listing_batch"))
    assert ready is True
    assert PROMOTE_MIN_EVIDENCE == 3


def test_empty_method_does_not_overwrite(tmp_path):
    """判「和上次一样」时不带正文 —— 不能把已经沉淀好的那版冲掉。"""
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    ledger.record("ecom_listing_batch", method="先判断库存再上架")
    ledger.record("ecom_listing_batch", method="")
    assert ledger.get("ecom_listing_batch")["method"] == "先判断库存再上架"


def test_survives_reload(tmp_path):
    path = tmp_path / "ledger.json"
    CompetenceLedger(path).record("ecom_listing_batch", user_input="x")
    fresh = CompetenceLedger(path)
    assert len(fresh.get("ecom_listing_batch")["evidence"]) == 1


def test_forget_removes_competence(tmp_path):
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    ledger.record("ecom_listing_batch")
    assert ledger.forget("ecom_listing_batch") is True
    assert ledger.get("ecom_listing_batch") is None
    assert ledger.forget("ecom_listing_batch") is False


def test_bind_skill_then_bump_revision(tmp_path):
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    ledger.record("ecom_listing_batch")
    ledger.bind_skill("ecom_listing_batch", skill_id="s1", skill_name="批量上架", version="1.0.0")
    comp = ledger.get("ecom_listing_batch")
    assert comp["skill_id"] == "s1"
    assert ledger.bump_revision("ecom_listing_batch", version="1.1.0") == 1
    assert ledger.get("ecom_listing_batch")["version"] == "1.1.0"


# === VALUE JUDGMENT TESTS ===


def test_pure_repetition_never_promotes(tmp_path):
    """做了 10 次、但一次都没说出学到了什么 —— 不配成技能。

    这是"频繁 ≠ 有价值"的护栏：一个每天-pointless 重复的简单动作，
    光靠次数堆不出方法论。
    """
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    for _ in range(10):
        ledger.record("trivial_repeat")   # 无 method、无 insight
    ready, why = ledger.readiness(ledger.get("trivial_repeat"))
    assert ready is False
    assert "纯重复" in why


def test_insight_alone_qualifies(tmp_path):
    """攒够了次数，哪怕没有成段 method，只要留下 insight 也够格。"""
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    for i in range(3):
        ledger.record("with_insight", insight=f"第 {i + 1} 次：批量限额按店铺算而非账号" if i == 0 else "")
    ready, why = ledger.readiness(ledger.get("with_insight"))
    assert ready is True, why


def test_insights_are_deduped_and_capped(tmp_path):
    """重复的 insight 不重复攒 —— 档案层留的是'学到过哪些不同的点'。"""
    ledger = CompetenceLedger(tmp_path / "ledger.json")
    for _ in range(5):
        ledger.record("ecom_listing_batch", insight="批量限额按店铺算")
    comp = ledger.get("ecom_listing_batch")
    assert comp["insights"] == ["批量限额按店铺算"]
