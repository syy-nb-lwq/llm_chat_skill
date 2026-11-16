"""nightly 评估脚本的单元测试 —— 指标算法与退出码

脚本本身是 `scripts/intent_eval_nightly.py`,这里锁住:
- 混淆矩阵 / per-label 指标的算法
- 矩阵渲染不炸
- 端到端跑一遍结构守卫 + 画像基线,退出码为 0
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    path = ROOT / "scripts" / "intent_eval_nightly.py"
    spec = importlib.util.spec_from_file_location("intent_eval_nightly", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def nightly():
    return _load_script()


def test_confusion_matrix_counts(nightly):
    pairs = [("task", "task"), ("task", "manage"), ("manage", "manage")]
    matrix = nightly.confusion_matrix(pairs)
    assert matrix["task"]["task"] == 1
    assert matrix["task"]["manage"] == 1
    assert matrix["manage"]["manage"] == 1


def test_per_label_scores(nightly):
    pairs = [
        ("task", "task"),
        ("task", "task"),
        ("task", "manage"),
        ("manage", "manage"),
    ]
    matrix = nightly.confusion_matrix(pairs)
    scores = nightly.per_label_scores(matrix, ["task", "manage"])

    # task: 2 正确 / 3 实际, 2 正确 / 2 预测
    assert scores["task"]["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert scores["task"]["precision"] == pytest.approx(1.0, abs=1e-3)
    assert scores["task"]["support"] == 3
    # manage: 1 正确 / 1 实际, 1 正确 / 2 预测
    assert scores["manage"]["recall"] == pytest.approx(1.0, abs=1e-3)
    assert scores["manage"]["precision"] == pytest.approx(0.5, abs=1e-3)


def test_summarize_accuracy_and_confusable(nightly):
    pairs = [
        ("task", "task"),
        ("manage", "task"),   # 易混对:期望 manage 判成 task
        ("teach", "teach"),
    ]
    summary = nightly.summarize(pairs, ["task", "manage", "teach"])
    assert summary["total"] == 3
    assert summary["correct"] == 2
    assert summary["accuracy"] == pytest.approx(2 / 3, abs=1e-3)
    # 易混口径:**按期望标签**判断是否属于 {manage, task, teach} 这个簇。
    # 三条用例的期望标签都在簇里 → 分母 3、判对 2。
    # (旧口径按 (predicted, expected) 二元组计数,而那两两必然不等,
    #  于是"判对"永远进不了分母,指标退化成"一次易混错都不能有"。)
    assert summary["confusable_total"] == 3
    assert summary["confusable_accuracy"] == pytest.approx(2 / 3, abs=1e-3)


def test_render_matrix_has_all_labels(nightly):
    matrix = nightly.confusion_matrix([("task", "task")])
    text = nightly.render_matrix(matrix, ["task", "manage"])
    assert "task" in text
    assert "manage" in text
    assert "期望" in text


def test_script_runs_and_passes(nightly, capsys):
    """端到端:结构守卫不得判错,退出码 0"""
    code = nightly.main(["--no-write"])
    assert code == 0
    out = capsys.readouterr().out
    assert "结构守卫" in out
    # 守卫现在一条都不判, 没有混淆矩阵可打; 画像基线还在
    assert "全部达标" in out


def test_script_fails_when_threshold_unmet(nightly):
    """阈值拉满到 1.01(不可能达到)→ 退出码 1,证明报警链路是通的"""
    # 守卫现在一条都不判(基数 0),所以阀门靠画像那边去证明
    code = nightly.main(["--no-write", "--min-profile-precision", "1.01"])
    assert code == 1


def test_script_writes_report(nightly, tmp_path):
    code = nightly.main(["--report-dir", str(tmp_path)])
    assert code == 0
    reports = list(tmp_path.glob("intent_eval_*.json"))
    assert len(reports) == 1

    import json

    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["case_count"] > 0
    assert data["guard"]["accuracy"] in (0.0, 1.0)
    assert data["passed"] is True
    assert "matrix" in data["guard"]
    assert "per_label" in data["guard"]
