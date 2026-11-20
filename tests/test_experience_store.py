"""`experience` 表（经验）—— 迁移、幂等、回滚、与画像同库关联。

## 为什么需要这组测试

`docs/19` §07 的问题是"长期记忆两套存储，失败记录到底存哪没有唯一答案"。
`docs/20` 的迁移方案把经验从 JSON 目录树搬到 SQLite，这只解决"载体"，
真正要锁住的是三件事：

1. **历史数据搬得过去**，且重复跑不会重复插入（幂等）；
2. **搬错了能退**（按 batch 精确回滚，不能靠删目录）；
3. **搬完真的能关联**（画像与经验在同一库文件里，一条 SQL 就能查）——
   这是当初决定迁移的动机，如果做不到，迁移就只是换了个存法。

第 3 条尤其重要：只测"写入成功"是测不出动机的。
"""

import json
import sqlite3

import pytest

from core.memory import MemoryStore, SkillPatch


@pytest.fixture
def store(tmp_path):
    return MemoryStore(base_path=tmp_path / "memory")


# ============================================================
# 1. 基本读写（载体换了，接口不能变）
# ============================================================

def test_roundtrip_failure(store):
    rec = store.record_failure(
        trace_id="t1", scenario="导出表格", intent="task", selected_skill="s",
        success_rate=0.2, fallback_count=1, latency_ms=12.0,
        diagnosis="列名猜错了", user_corrected=True,
    )
    got = store.get_recent_failures(top_k=5)
    assert [r.id for r in got] == [rec.id]
    assert got[0].diagnosis == "列名猜错了"
    assert got[0].user_corrected is True


def test_scenario_filter_is_substring(store):
    store.record_failure(
        trace_id="t1", scenario="导出 Excel 表格", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="a",
    )
    store.record_failure(
        trace_id="t2", scenario="画图", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="b",
    )
    assert [r.trace_id for r in store.get_recent_failures(scenario="Excel")] == ["t1"]
    assert len(store.get_recent_failures(scenario="不存在的场景")) == 0


def test_patch_status_transitions(store):
    store.add_pending_patch(SkillPatch(
        id="p1", trace_id="t1", timestamp="2026-01-01T00:00:00",
        target_skill="skills/x", patch_type="improve_skill",
        diagnosis="d", suggestion={"method": "m"},
    ))
    assert [p.id for p in store.get_pending_patches()] == ["p1"]

    assert store.approve_patch("p1", "human") is True
    assert store.get_pending_patches() == []
    assert store.get_patch("p1").status == "approved"
    assert store.get_patch("p1").reviewed_by == "human"

    # 再拒一次：状态可以改，但不会回到 pending
    assert store.reject_patch("p1", "human") is True
    assert store.get_patch("p1").status == "rejected"


def test_patch_missing_returns_none(store):
    assert store.get_patch("nope") is None
    assert store.approve_patch("nope") is False
    assert store.reject_patch("nope") is False


def test_remove_success_only_touches_success(store):
    store.record_success("t1", "x", "skills/a", 1.0)
    store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
    )
    assert store.remove_success("t1") is True
    assert store.get_recent_successes() == []
    # 同 trace_id 的失败记录不能被误删
    assert len(store.get_recent_failures(top_k=5)) == 1


def test_adoption_rate_counts_corrected(store):
    store.record_success("s1", "x", "skills/a", 1.0)
    store.record_failure(
        trace_id="f1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="e",
        user_corrected=True,
    )
    rate = store.get_adoption_rate()
    assert rate["evaluated"] == 2
    assert rate["corrected"] == 1
    assert rate["adoption_rate"] == 0.5


def test_enforce_capacity_keeps_high_success_rate(store):
    for i in range(10):
        store.record_failure(
            trace_id=f"t{i}", scenario="x", intent="task", selected_skill="s",
            success_rate=i / 10.0, fallback_count=0, latency_ms=1.0, diagnosis=f"d{i}",
        )
    store.enforce_capacity(max_per_month=5, keep_ratio=0.4)
    left = store.get_recent_failures(top_k=100)
    assert len(left) == 4  # int(10 * 0.4)
    # 保留的是 success_rate 最高的四条（t6~t9）
    assert {r.trace_id for r in left} == {"t6", "t7", "t8", "t9"}


# ============================================================
# 2. 历史 JSON 导入：幂等 + 回滚
# ============================================================

def _write_legacy(base):
    (base / "failures" / "2026-11").mkdir(parents=True, exist_ok=True)
    (base / "failures" / "2026-11" / "e1.json").write_text(json.dumps({
        "id": "fail_e1", "trace_id": "t1", "timestamp": "2026-11-01T10:00:00",
        "scenario": "导出", "intent": "task", "selected_skill": "skills/x",
        "success_rate": 0.1, "fallback_count": 1, "latency_ms": 9.0,
        "diagnosis": "列名错", "suggestion": None, "user_corrected": False,
        "execution_id": "e1", "session_id": "default", "turn_id": "1",
    }, ensure_ascii=False), encoding="utf-8")

    (base / "successes").mkdir(parents=True, exist_ok=True)
    (base / "successes" / "success_index.jsonl").write_text(
        json.dumps({
            "id": "succ_t2", "trace_id": "t2", "timestamp": "2026-11-02T10:00:00",
            "scenario": "画图", "matched_skill": "skills/y", "latency_ms": 3.0,
            "pattern": "先问再画",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    (base / "skill_patches" / "pending").mkdir(parents=True, exist_ok=True)
    (base / "skill_patches" / "pending" / "p1.json").write_text(json.dumps({
        "id": "p1", "trace_id": "t3", "timestamp": "2026-11-03T10:00:00",
        "target_skill": "skills/x", "patch_type": "improve_skill",
        "diagnosis": "d", "suggestion": {"method": "m"},
        "confidence": 0.7, "status": "pending", "reviewed_by": None,
        "validation": None, "risk_level": "unknown", "blocked_reason": "",
    }, ensure_ascii=False), encoding="utf-8")


def test_import_legacy_json(store):
    _write_legacy(store.base)
    report = store.import_json(store.base, batch_id="b1")
    assert report["scanned"] == 3
    assert report["inserted"] == 3
    assert store.get_stats()["total_failures"] == 1
    assert store.get_stats()["total_successes"] == 1
    assert [p.id for p in store.get_pending_patches()] == ["p1"]
    # 导入的记录要能正常走审批
    assert store.approve_patch("p1") is True


def test_import_is_idempotent(store):
    _write_legacy(store.base)
    first = store.import_json(store.base, batch_id="b1")
    second = store.import_json(store.base, batch_id="b2")
    assert first["inserted"] == 3
    assert second["inserted"] == 0
    assert second["skipped"] == 3
    assert store.get_stats()["total_failures"] == 1


def test_rollback_batch_removes_only_that_batch(store):
    _write_legacy(store.base)
    store.import_json(store.base, batch_id="b1")

    # 迁移之后运行期又写了一条，回滚不能把它带走
    store.record_failure(
        trace_id="live", scenario="新数据", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="live",
    )

    removed = store.rollback_batch("b1")
    assert removed == 3
    assert store.get_stats()["total_failures"] == 1
    assert [r.trace_id for r in store.get_recent_failures(top_k=5)] == ["live"]


def test_import_tolerates_broken_files(store):
    legacy = store.base / "failures" / "2026-11"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "bad.json").write_text("{ 这不是 json", encoding="utf-8")
    report = store.import_json(store.base, batch_id="b1")
    assert report["inserted"] == 0  # 坏文件当没有，不抛


# ============================================================
# 3. 动机验证：画像 ⋈ 经验 一条 SQL
# ============================================================

def test_same_database_allows_join_with_profile(store):
    """当初决定迁移的动机 —— 两条记忆能在 SQL 里关联，不是靠 Python 拼。"""
    from core.memory_db import MemoryDB

    db = MemoryDB(db_path=store.db_path)
    db.add("method", "导出 Excel 时先确认表头", metadata={"topic": "excel"})

    store.record_failure(
        trace_id="t1", scenario="导出 Excel", intent="task", selected_skill="skills/x",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="表头错",
    )

    conn = sqlite3.connect(str(store.db_path))
    try:
        rows = conn.execute(
            """SELECT m.content, json_extract(e.payload, '$.diagnosis')
               FROM memories m JOIN experience e
                 ON json_extract(m.metadata, '$.topic') = 'excel'
                AND e.scenario LIKE '%Excel%'
               WHERE m.type = 'method' AND e.kind = 'failure'"""
        ).fetchall()
    finally:
        conn.close()
    assert rows, "画像与经验应该能在同一个库里 JOIN 到"
    assert rows[0][1] == "表头错"


def test_store_shares_db_file_with_profile_by_default(tmp_path):
    """默认路径必须是同一个库文件，否则 JOIN 只是空谈。"""
    store = MemoryStore(base_path=tmp_path / "memory")
    assert store.db_path.name == "semantic_memory.db"


# ============================================================
# 4. 导出（单向出口）
# ============================================================

def test_export_json_writes_readable_files(store, tmp_path):
    store.record_failure(
        trace_id="t1", scenario="x", intent="task", selected_skill="s",
        success_rate=0.0, fallback_count=0, latency_ms=1.0, diagnosis="人可读的诊断",
    )
    dest = tmp_path / "export"
    counts = store.export_json(dest)
    assert counts["failure"] == 1
    files = list((dest / "failure").glob("*.json"))
    assert len(files) == 1
    assert "人可读的诊断" in files[0].read_text(encoding="utf-8")
