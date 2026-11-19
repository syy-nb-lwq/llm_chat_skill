"""全局单例的懒初始化必须线程安全。

现状是 asyncio 单线程，`if x is None` 几乎不会争；但只要有人用
`run_in_executor` / 线程池（导出、压缩、批量评估都可能），无锁判断就会
造出两个实例 —— 两个 SQLite 连接同时写同一个文件，症状是随机
`database is locked` 或数据互相看不见。
"""
from __future__ import annotations

import importlib
import threading
import time

import pytest


CASES = [
    ("core.session_memory", "get_session_memory_store", "SessionMemoryStore", "reset_session_memory_store", "_session_store"),
    ("core.memory_db", "get_memory_db", "MemoryDB", "reset_memory_db", "_memory_db"),
    ("core.semantic_memory", "get_semantic_memory", "SemanticMemoryStore", "reset_semantic_memory", "_semantic_memory"),
    ("core.skill_quality", "get_skill_quality_store", "SkillQualityStore", "reset_skill_quality_store", "_store"),
]

#: 在**收集阶段**（conftest 的 autouse fixture 生效之前）把真实 getter 存下来。
#:
#: conftest 会把部分 getter 换成 `lambda: 预置实例`（为了隔离真实 DB），
#: 直接 `getattr(module, name)` 拿到的是 lambda —— 构造次数永远是 0，
#: 测不到懒初始化。这里留一份未替换的引用。
_REAL_GETTERS = {
    (module_name, getter_name): getattr(importlib.import_module(module_name), getter_name)
    for module_name, getter_name, *_ in CASES
}


@pytest.mark.parametrize("module_name,getter_name,cls_name,reset_name,var_name", CASES)
def test_getter_creates_exactly_one_instance(
    monkeypatch, module_name, getter_name, cls_name, reset_name, var_name
):
    mod = importlib.import_module(module_name)
    getter = _REAL_GETTERS[(module_name, getter_name)]
    original = getattr(mod, var_name)
    created = []

    class _Stub:
        def __init__(self):
            created.append(1)
            # 放大竞态窗口：没有锁时这里必然被多个线程穿过
            time.sleep(0.01)

    try:
        monkeypatch.setattr(mod, cls_name, _Stub)
        getattr(mod, reset_name)()

        results = []
        barrier = threading.Barrier(12)
        guard = threading.Lock()

        def worker():
            barrier.wait()
            inst = getter()
            with guard:
                results.append(inst)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 12
        assert len({id(r) for r in results}) == 1, "并发下拿到了多个实例"
        assert len(created) == 1, f"构造函数被调用了 {len(created)} 次"
    finally:
        # 还原成进入测试前的实例，避免污染后续测试
        setattr(mod, var_name, original)
