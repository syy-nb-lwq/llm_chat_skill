"""归纳窗口的陈旧兜底 —— 短会话不该烂在窗口里。

实机问题：聊了 3 轮出差报销就走了，窗口只攒到 3/6，用户也没点 reset，
这 3 轮永远不被归纳 —— 于是"聊了出差报销什么都没沉淀"。

修法：距最后一轮超过空闲阈值，下次对话时先把攒的轮次归纳掉。
"""
from core.digest_window import DigestWindow


def test_window_still_accumulates_when_fresh():
    """连续对话（未陈旧）仍按 size 攒批，不提前归纳。"""
    w = DigestWindow(size=6, keep=2)
    assert w.add("第一轮内容") is None
    assert w.add("第二轮内容") is None
    # 刚 add 过，不算陈旧
    assert w.take_stale(max_idle_s=9999) is None
    assert w.pending() == 2


def test_take_stale_returns_batch_when_idle():
    """陈旧后交出整批并清空（不留尾，语义同 flush）。"""
    w = DigestWindow(size=6, keep=2)
    w.add("聊出差报销第一轮")
    w.add("加一行第二轮")

    # 直接把最后 add 时间往前拨，模拟"隔了很久"（不 patch 全局时间，
    # 否则会干扰 pytest 自身计时，让整个测试跑几分钟）
    w._last_add_at -= 100000

    batch = w.take_stale(max_idle_s=60)
    assert batch is not None
    assert len(batch) == 2
    assert batch[0]["user"] == "聊出差报销第一轮"
    assert w.pending() == 0
    # 再调一次没有重复归纳
    assert w.take_stale(max_idle_s=60) is None


def test_take_stale_ignores_short_turns():
    """短轮次（"好""嗯"）不进窗口，也不影响陈旧判定。"""
    w = DigestWindow(size=6, keep=2)
    assert w.add("好") is None
    assert w.pending() == 0


def test_full_window_takes_normally():
    """攒满 size 仍按原逻辑触发（带 keep 尾）。"""
    w = DigestWindow(size=3, keep=1)
    assert w.add("第一轮内容") is None
    assert w.add("第二轮内容") is None
    batch = w.add("第三轮内容")
    assert batch is not None and len(batch) == 3
    assert w.pending() == 1  # 保留尾部一轮
