"""价值判断：凭什么说一条方法论值得成为 skill。

用户的核心质疑：skill 是做事的方法，不是做过的事的流水账。
这条测试钉住 assess_value 的判据 —— 它回答的是"有没有可迁移的判断"，
和"写得像不像方法论"（quality）、"做了几次"（readiness）是三件不同的事。

判据只有一条：
    删掉这条 method，下次遇到同类任务会不会做错/走弯路？
    会 → 有价值；不会 → 纯顺序叙述，planner 每次都能自己推，固化它只会添乱。
"""
from core.skill_harvest import assess_value


# ---------------------------------------------------------------------------
# 有价值的：含 planner 推不出来的判断
# ---------------------------------------------------------------------------

def test_pitfall_is_valuable():
    """踩过的坑 —— 典型的可迁移判断。"""
    out = assess_value("发布失败常见坑是相对路径读不到，动手前先确认工作区根目录，否则一路报错")
    assert out["valuable"] is True
    assert any("坑" in s for s in out["signals"])


def test_counterintuitive_is_valuable():
    """反直觉判断 —— 看起来该 A，其实该 B。"""
    out = assess_value("看起来该直接重试，其实配额耗尽时重试无用，反而要先去查额度再决定")
    assert out["valuable"] is True
    assert any("反直觉" in s for s in out["signals"])


def test_branch_is_valuable():
    """按信号分支处理，不是一条道走到黑。"""
    out = assess_value("如果产物能被读回就判定偶发失败直接重试，若读不回则回到内容本身修改")
    assert out["valuable"] is True
    assert any("分支" in s for s in out["signals"])


def test_done_criterion_is_valuable():
    """显式的完成标准 —— 区别于'看起来做完了'。"""
    out = assess_value("产物必须能被重新读回并核对内容才算完成，缺任何一项都不算做完")
    assert out["valuable"] is True
    assert any("完成标准" in s for s in out["signals"])


def test_explicit_insight_rescues_plain_text():
    """规则没抓到硬信号，但模型说清了一句具体洞察 —— 也算有价值。"""
    out = assess_value(
        "先把数据整理好再处理",
        insight="这个平台的批量限额是按店铺算、不是按账号算，换店铺要重新探一次边界",
    )
    assert out["valuable"] is True
    assert out["insight_used"] is True
    assert any("洞察" in s for s in out["signals"])


# ---------------------------------------------------------------------------
# 没价值的：纯顺序叙述 / 空话
# ---------------------------------------------------------------------------

def test_pure_sequence_is_worthless():
    """先做 A 再做 B —— planner 每次都能推，不该固化。"""
    out = assess_value("第一步打开文件，然后读取数据，接着处理内容，最后保存结果")
    assert out["valuable"] is False
    assert "操作顺序" in out["reason"]


def test_vague_insight_does_not_count():
    """'要注意质量'这种谁都会说的空话，不该被当成洞察。"""
    out = assess_value("把数据处理好", insight="要注意质量，多检查")
    assert out["valuable"] is False


def test_empty_method_is_worthless():
    assert assess_value("")["valuable"] is False


# ---------------------------------------------------------------------------
# 可解释性：判定必须说得出理由
# ---------------------------------------------------------------------------

def test_reason_is_always_present():
    for text in ("", "第一步做 A 然后做 B", "坑是要确认根目录否则读不到"):
        out = assess_value(text)
        assert out["reason"]


def test_signals_are_exposed_for_ui():
    """界面要能展示'它凭什么算有价值'，所以信号列表必须是可读中文。"""
    out = assess_value("如果读不回就是内容问题，必须改内容；否则直接重试")
    assert out["signals"]
    assert all(isinstance(s, str) for s in out["signals"])
