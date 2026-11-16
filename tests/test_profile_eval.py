"""画像抽取基线的 CI 闸门 —— 改规则表必须先过这一关。

对应 `docs/14` §4.1「没有真实基线」这一条:画像抽取是**纯规则**的,
所以基线可以现在就建,不用等真实 API key。

基线数值(2026-07-22 建立):
| 指标 | 修复前 | 现在 | 闸门 |
|---|---|---|---|
| 精确率 | 86.7% | 100% | ≥ 95% |
| 召回率 | 100% | 100% | ≥ 80% |

**精确率是生死线**:一条错误的画像会持续影响此后每一轮
(docs/13 §2.8「宁缺毋滥」)。召回率低一点可以接受 ——
规则本来就有天花板(docs/13 D14)。

改 `core/memory_writer.py` 的规则表之前,先跑
`python scripts/profile_eval.py`,看是精确率掉了还是召回率掉了。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "tests" / "data" / "profile_eval.jsonl"
SCRIPT = ROOT / "scripts" / "profile_eval.py"

PROFILE_TYPES = {"preference", "habit", "method", "constraint", "fact"}


def load_cases():
    cases = []
    with EVAL_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cases.append(json.loads(line))
    return cases


CASES = load_cases()


# ===== 评估集本身的合理性 =====

def test_eval_set_is_sane():
    assert len(CASES) >= 30, "用例太少,基线不可信"
    for case in CASES:
        assert case["input"].strip()
        for exp in case.get("expect", []):
            assert exp["type"] in PROFILE_TYPES, f"非法类型: {exp}"
            assert exp["contains"].strip()


def test_eval_set_covers_all_profile_types():
    types = {e["type"] for c in CASES for e in c.get("expect", [])}
    missing = PROFILE_TYPES - types
    assert not missing, f"评估集没覆盖类型: {missing}"


def test_eval_set_has_negatives():
    """必须有"不该抽"的用例 —— 只有正例的评估集测不出精确率。"""
    negatives = [c for c in CASES if not c.get("expect")]
    assert len(negatives) >= 10, f"负例只有 {len(negatives)} 条"


# ===== 基线指标 =====

def _evaluate():
    sys.path.insert(0, str(ROOT / "scripts"))
    import profile_eval

    return profile_eval.evaluate(CASES)


def test_profile_extraction_baseline_meets_targets():
    report = _evaluate()
    failures = []
    if report["precision"] < 0.95:
        failures.append(
            f"精确率 {report['precision']:.1%} < 95%:\n"
            + "\n".join(
                f"  - {f['input']!r} 多抽出 {f['extracted']}"
                for f in report["failures"]
                if f["extra"]
            )
        )
    if report["recall"] < 0.80:
        failures.append(
            f"召回率 {report['recall']:.1%} < 80%:\n"
            + "\n".join(
                f"  - {f['input']!r} 期望 {f['expected']}"
                for f in report["failures"]
                if f["missing"]
            )
        )
    assert not failures, "\n".join(failures)


def test_profile_eval_script_runs_and_gates():
    """脚本本身能跑,且在达标时退出码为 0。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--no-write"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, f"退出码 {proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "精确率" in proc.stdout
    assert "全部达标" in proc.stdout


# ===== 具体回归(改规则表时最容易踩的坑) =====

def test_near_duplicate_method_is_stored_once():
    """「正确做法是先备份再迁移」不该同时存 explicit_method 和 step_order 两条。

    两条几乎一样,既浪费注入预算(画像 ≤400 字符),
    又会让同一件事在系统提示词里出现两遍。
    """
    from core.memory_writer import extract_candidates

    cands = extract_candidates("正确做法是先备份数据库再执行迁移")
    methods = [c for c in cands if c.type == "method"]
    assert len(methods) == 1, f"存了 {len(methods)} 条 method: {[c.content for c in methods]}"
    assert "正确做法" in methods[0].content


def test_users_own_arrangement_is_not_a_preference():
    """「以后我们开会要提前十分钟」是用户自己的安排,不是「希望助手怎么做」。

    记成 preference 的话,系统提示词里会长期挂着一条
    "用户偏好:以后我们开会要提前十分钟",助手可能真的照着调整 —— 典型的长期污染。
    """
    from core.memory_writer import extract_candidates

    assert extract_candidates("以后我们开会要提前十分钟") == []
    assert extract_candidates("以后我每天早起半小时") == []


def test_transient_reaction_is_not_a_durable_preference():
    """「我喜欢这个方案」是对当前这一轮的反应,不是长期偏好。"""
    from core.memory_writer import extract_candidates

    assert extract_candidates("我喜欢这个方案") == []
    # 但真正的长期偏好要留下
    cands = extract_candidates("我喜欢简洁的结论")
    assert len(cands) == 1 and cands[0].type == "preference"


@pytest.mark.parametrize(
    "text",
    [
        "以后回答都用中文",
        "以后写日报都按三段式来",
        "以后做日报就按三段式来",
        "以后每次发布前都要先跑一遍回归",
        "从现在起回答请简短一些",
    ],
)
def test_real_output_preferences_are_not_lost(text):
    """给"以后…"加守卫是为了挡用户的个人安排,不能把真正的产出约定也挡掉。"""
    from core.memory_writer import extract_candidates

    cands = extract_candidates(text)
    assert any(c.type == "preference" for c in cands), f"漏掉了真实偏好: {text}"
