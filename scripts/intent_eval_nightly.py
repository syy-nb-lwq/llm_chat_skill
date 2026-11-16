"""意图评估 nightly 任务 —— 跑评估集、出混淆矩阵、落审计文件

为什么要单独一个脚本(而不是只用 pytest):
pytest 只回答"过/不过",而改提示词时需要知道**错在哪**。
混淆矩阵能直接看出"task 被误判成 manage"这类系统性问题。

跑法:
```bash
python scripts/intent_eval_nightly.py             # 只跑结构守卫 + 画像基线(离线)
python scripts/intent_eval_nightly.py --live      # 加跑 L1 LLM 通道(需真实 key)
python scripts/intent_eval_nightly.py --live --min-l1 0.92
```

退出码:
  0 = 全部达标
  1 = 有指标未达标(CI/nightly 会因此报警)
  2 = 环境问题(评估集缺失等)
"""
import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# 报告工具:日志刷屏会盖住混淆矩阵,默认只留警告
os.environ.setdefault("LOG_LEVEL", "WARNING")

from core.intent_detector import IntentDetector  # noqa: E402
from core.intent_labels import IntentLabel  # noqa: E402

EVAL_FILE = ROOT / "tests" / "data" / "intent_eval.jsonl"
HOLDOUT_FILES = [
    ROOT / "tests" / "data" / "intent_eval_holdout.jsonl",
    ROOT / "tests" / "data" / "intent_eval_holdout2.jsonl",
    ROOT / "tests" / "data" / "intent_eval_holdout3.jsonl",
    ROOT / "tests" / "data" / "intent_eval_holdout4.jsonl",
    ROOT / "tests" / "data" / "intent_eval_holdout5.jsonl",
    ROOT / "tests" / "data" / "intent_eval_sealed.jsonl",
]
DEFAULT_REPORT_DIR = ROOT / "memory" / "audit"

#: 易混对(历史上互相误判过的组合)
CONFUSABLE_PAIRS = {
    ("manage", "task"),
    ("task", "manage"),
    ("teach", "task"),
    ("task", "teach"),
}

#: 参与易混判定的标签集合。
#:
#: **口径**:按**期望标签**判断一条用例是否属于易混簇,再算这个簇上的准确率。
#: (之前按 (predicted, expected) 落在二元组里来计数,而那些二元组里的两个标签
#:  必然不同 —— 于是"判对"的用例一次都进不了分母,指标实际上退化成了
#:  "一次易混错都不能有"。)
CONFUSABLE_LABELS = {"manage", "task", "teach"}


def load_cases(path: Path = EVAL_FILE):
    cases = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cases.append(json.loads(line))
    return cases


# ===== 指标计算 =====


def confusion_matrix(pairs):
    """pairs: [(期望, 实际)] → {期望: {实际: 次数}}"""
    matrix = defaultdict(Counter)
    for expected, predicted in pairs:
        matrix[expected][predicted] += 1
    return matrix


def per_label_scores(matrix, labels):
    """每个标签的 precision / recall / f1"""
    scores = {}
    for label in labels:
        tp = matrix.get(label, Counter()).get(label, 0)
        predicted_total = sum(matrix[e][label] for e in labels)
        actual_total = sum(matrix.get(label, Counter()).values())
        precision = tp / predicted_total if predicted_total else 0.0
        recall = tp / actual_total if actual_total else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )
        scores[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": actual_total,
        }
    return scores


def render_matrix(matrix, labels) -> str:
    """把混淆矩阵画成对齐的文本表"""
    width = max(len(l) for l in labels) + 2
    header = "期望\\实际".ljust(width) + "".join(l.ljust(width) for l in labels)
    lines = [header, "-" * len(header)]
    for expected in labels:
        row = expected.ljust(width)
        for predicted in labels:
            count = matrix.get(expected, Counter()).get(predicted, 0)
            row += (str(count) if count else ".").ljust(width)
        lines.append(row)
    return "\n".join(lines)


def print_matrix(matrix, labels) -> None:
    print("\n混淆矩阵(行=期望,列=实际,`.` 表示 0):")
    print(render_matrix(matrix, labels))


# ===== 通道 =====


def run_guard_channel(cases):
    """结构守卫:只统计它能定案的用例(现在基本只有空输入)。

    守卫不判意图——意图全部交给 L1。
    这个通道的作用只有一个:**守卫判了的那几条不能判错**。
    """
    pairs = []
    undecided = []
    for case in cases:
        result = IntentDetector().detect(case["input"])
        if result.label is None:
            undecided.append(case)
            continue
        pairs.append((case["label"], result.label.value))
    return pairs, undecided


async def run_l1_channel(cases, pace: float = 0.0, unavailable_retries: int = 2):
    """L1 闭集判别层(整条链:先过 L0,未定案才问 L1)

    `pace`:两次调用之间的间隔(秒)。突发流量下对方会直接掐连接,
    基线跑到一半全是 `unavailable` —— 那不是模型判错,是网络抖。
    `unavailable_retries`:调用彻底失败后再试几次,仍失败才算这条没判出来。
    """
    from core.llm_judge import L1Classifier

    classifier = L1Classifier()
    pairs = []
    failures = []
    for case in cases:
        expected = case["label"]
        # 意图唯一裁决者就是 L1——不再先过一层规则。
        predicted = "unavailable"
        for _ in range(max(1, unavailable_retries + 1)):
            decision = await classifier.classify(case["input"])
            if decision is not None:
                predicted = decision.label.value
                break
            if pace:
                await asyncio.sleep(pace)
        if pace:
            await asyncio.sleep(pace)
        pairs.append((expected, predicted))
        if predicted != expected:
            failures.append(
                {
                    "input": case["input"],
                    "expected": expected,
                    "predicted": predicted,
                    "note": case.get("note", ""),
                }
            )
    return pairs, failures


async def run_l1_repeated(cases, repeat: int, pace: float = 0.0):
    """把 L1 通道跑 `repeat` 遍,返回每一遍的结果。

    **为什么要多遍**:LLM 是非确定性的 —— 同一条输入,两次可能给出不同标签。
    报一个单次数字会骗人:这次 100%、下次 95%,到底哪个是"基线"?
    真实基线是一个**分布**,所以这里跑多遍,报告 min/mean/max。
    """
    runs = []
    for i in range(max(1, repeat)):
        pairs, failures = await run_l1_channel(cases, pace=pace)
        runs.append({"pairs": pairs, "failures": failures})
        print(f"  第 {i + 1}/{repeat} 遍完成")
    return runs


def run_profile_baseline():
    """画像抽取基线(纯规则通道,不需要 LLM)。

    与意图评估放在一起跑,是因为它们回答的是同一个问题的两半:
    "现在这套规则到底有多准,改完是变好还是变坏"。
    """
    import profile_eval

    return profile_eval.evaluate(profile_eval.load_cases())


def summarize(pairs, labels) -> dict:
    total = len(pairs)
    correct = sum(1 for e, p in pairs if e == p)
    confusable_total = sum(1 for e, p in pairs if e in CONFUSABLE_LABELS)
    confusable_correct = sum(
        1 for e, p in pairs if e in CONFUSABLE_LABELS and e == p
    )
    matrix = confusion_matrix(pairs)
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "confusable_total": confusable_total,
        "confusable_accuracy": (
            round(confusable_correct / confusable_total, 4)
            if confusable_total
            else None
        ),
        "per_label": per_label_scores(matrix, labels),
        "matrix": {k: dict(v) for k, v in matrix.items()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="意图评估 nightly")
    parser.add_argument("--live", action="store_true", help="跑 L1 LLM 通道(需真实 key)")
    parser.add_argument("--min-guard", type=float, default=1.0, help="结构守卫最低准确率(判了就必须对)")
    parser.add_argument("--min-l1", type=float, default=0.90, help="L1 最低准确率")
    parser.add_argument(
        "--repeat", type=int, default=3, help="L1 通道跑几遍(LLM 非确定性,基线是分布)"
    )
    parser.add_argument(
        "--pace", type=float, default=0.4, help="两次 LLM 调用之间的间隔秒数(防突发被掐)"
    )
    parser.add_argument(
        "--min-holdout", type=float, default=0.80, help="留出集最低准确率(过拟合闸门)"
    )
    parser.add_argument(
        "--max-unavailable",
        type=float,
        default=0.10,
        help="允许的调用失败比例;超过就判定通道不可用、本次基线无效",
    )
    parser.add_argument(
        "--min-confusable", type=float, default=0.95, help="易混对最低准确率"
    )
    parser.add_argument(
        "--min-profile-precision", type=float, default=0.95, help="画像抽取最低精确率"
    )
    parser.add_argument(
        "--min-profile-recall", type=float, default=0.80, help="画像抽取最低召回率"
    )
    parser.add_argument(
        "--report-dir", type=Path, default=DEFAULT_REPORT_DIR, help="审计文件目录"
    )
    parser.add_argument("--no-write", action="store_true", help="只打印,不落文件")
    args = parser.parse_args(argv)

    if not EVAL_FILE.exists():
        print(f"[FAIL] 评估集不存在: {EVAL_FILE}")
        return 2

    cases = load_cases()
    if not cases:
        print("[FAIL] 评估集为空")
        return 2

    labels = sorted(l.value for l in IntentLabel)
    report = {
        "generated_at": datetime.now().isoformat(),
        "eval_file": str(EVAL_FILE),
        "case_count": len(cases),
        "live": args.live,
    }
    failed = []

    # ---- 结构守卫(不判意图,只判空输入这类结构事实) ----
    guard_pairs, undecided = run_guard_channel(cases)
    guard_summary = summarize(guard_pairs, labels)
    report["guard"] = {
        **guard_summary,
        "decided": len(guard_pairs),
        "undecided": len(undecided),
    }
    print("\n=== 结构守卫 ===")
    if guard_pairs:
        print(f"定案 {len(guard_pairs)}/{len(cases)} 条,准确率 {guard_summary['accuracy']:.1%}"
              f"(守卫只判结构事实,意图全部交给 L1)")
        print_matrix(confusion_matrix(guard_pairs), labels)
    else:
        print(f"定案 0/{len(cases)} 条 —— 全量交给 L1(这是预期行为:守卫只判空输入)")
    # 守卫判了就必须对 —— 但它一条都没判时不该算失败(判 0 条 = 没有判错的机会)
    if guard_pairs and guard_summary["accuracy"] < args.min_guard:
        failed.append(
            f"结构守卫准确率 {guard_summary['accuracy']:.1%} < {args.min_guard:.0%}"
        )

    # ---- L1 LLM 通道 ----
    if args.live:
        runs = asyncio.run(run_l1_repeated(cases, args.repeat, args.pace))

        # **先验通道可用性,再谈准确率。**
        # 网络/API 抖的时候,拿不到的用例会被记成 "unavailable" 从而显示成"判错",
        # 于是基线看起来突然从 90% 掉到 20% —— 那是假信号,会误导人删掉本来对的提示词。
        # 一条基线只有在通道真的可用时才有意义,否则就明说"本次无效"。
        unavailable_counts = [
            sum(1 for _, p in r["pairs"] if p == "unavailable") for r in runs
        ]
        worst_unavailable = max(unavailable_counts) if unavailable_counts else 0
        ratio = worst_unavailable / len(cases) if cases else 0.0
        report["l1_channel_available"] = ratio <= args.max_unavailable
        if ratio > args.max_unavailable:
            print(
                f"\n=== L1 通道不可用 —— 本次基线无效 ===\n"
                f"{worst_unavailable}/{len(cases)} 条用例拿不到模型输出 "
                f"({ratio:.0%} > {args.max_unavailable:.0%})。\n"
                f"这不是模型判错,是网络/API 不可用。修好通道再重跑,"
                f"不要按这个结果去改提示词。"
            )
            failed.append(
                f"L1 通道不可用({worst_unavailable}/{len(cases)} 条拿不到输出),基线无效"
            )
            runs = []

    if args.live and runs:
        summaries = [summarize(r["pairs"], labels) for r in runs]
        accs = [s["accuracy"] for s in summaries]
        l1_summary = summaries[0]
        # 用**最差**的那一遍做闸门:基线不能靠运气
        worst_idx = min(range(len(summaries)), key=lambda i: summaries[i]["accuracy"])
        worst = summaries[worst_idx]
        worst_failures = runs[worst_idx]["failures"]
        report["l1"] = {
            **worst,
            "repeat": len(runs),
            "accuracy_min": round(min(accs), 4),
            "accuracy_mean": round(sum(accs) / len(accs), 4),
            "accuracy_max": round(max(accs), 4),
            "per_run_accuracy": [round(a, 4) for a in accs],
            "failures": worst_failures,
        }
        print(
            f"\n=== L1 通道(整条链)x{len(runs)} ===\n"
            f"准确率 min {min(accs):.1%} / 平均 {sum(accs) / len(accs):.1%} / max {max(accs):.1%}"
            f"  (每遍: {', '.join(f'{a:.1%}' for a in accs)})"
        )
        print_matrix(confusion_matrix(runs[worst_idx]["pairs"]), labels)
        for f in worst_failures:
            print(
                f"  错: {f['input']!r} 期望 {f['expected']} 实际 {f['predicted']}"
            )
        if worst["accuracy"] < args.min_l1:
            failed.append(
                f"L1 准确率 {worst['accuracy']:.1%} < {args.min_l1:.0%}({len(runs)} 遍里最差的一遍)"
            )
        conf = worst["confusable_accuracy"]
        if conf is not None and conf < args.min_confusable:
            failed.append(f"易混对准确率 {conf:.1%} < {args.min_confusable:.0%}")

        # ---- 留出集:改完提示词之后才写的用例,用来检验泛化 ----
        holdout_all = []
        for hf in HOLDOUT_FILES:
            if hf.exists():
                holdout_all.extend(load_cases(hf))
        if holdout_all:
            h_runs = asyncio.run(run_l1_repeated(holdout_all, args.repeat, args.pace))
            h_unavail = max(
                sum(1 for _, p in r["pairs"] if p == "unavailable") for r in h_runs
            )
            h_ratio = h_unavail / len(holdout_all) if holdout_all else 0.0
            report["holdout_channel_available"] = h_ratio <= args.max_unavailable
            if h_ratio > args.max_unavailable:
                print(
                    f"\n=== L1 留出集通道不可用 —— 本次基线无效 ===\n"
                    f"{h_unavail}/{len(holdout_all)} 条拿不到模型输出 ({h_ratio:.0%})。"
                )
                failed.append(
                    f"留出集通道不可用({h_unavail}/{len(holdout_all)}),基线无效"
                )
                h_runs = []
        if holdout_all and h_runs:
            h_summaries = [summarize(r["pairs"], labels) for r in h_runs]
            h_accs = [s["accuracy"] for s in h_summaries]
            h_worst_idx = min(
                range(len(h_summaries)), key=lambda i: h_summaries[i]["accuracy"]
            )
            h_worst = h_summaries[h_worst_idx]
            report["holdout"] = {
                **h_worst,
                "case_count": len(holdout_all),
                "repeat": len(h_runs),
                "accuracy_min": round(min(h_accs), 4),
                "accuracy_mean": round(sum(h_accs) / len(h_accs), 4),
                "accuracy_max": round(max(h_accs), 4),
                "per_run_accuracy": [round(a, 4) for a in h_accs],
            }
            print(
                f"\n=== L1 留出集 x{len(h_runs)}({len(holdout_all)} 条)===\n"
                f"准确率 min {min(h_accs):.1%} / 平均 {sum(h_accs) / len(h_accs):.1%} / max {max(h_accs):.1%}"
                f"  (每遍: {', '.join(f'{a:.1%}' for a in h_accs)})"
            )
            for f in h_runs[h_worst_idx]["failures"]:
                print(
                    f"  错: {f['input']!r} 期望 {f['expected']} 实际 {f['predicted']}"
                )
            if min(h_accs) < args.min_holdout:
                failed.append(
                    f"L1 留出集准确率 {min(h_accs):.1%} < {args.min_holdout:.0%}(过拟合闸门)"
                )
    else:
        print("\n(跳过 L1 通道:加 --live 才会调用真实模型)")

    # ---- 画像抽取基线(纯规则,始终可跑) ----
    # 画像抽取不依赖 LLM,所以这条基线**现在就能建**,
    # 不像 L1 那样要等真实 key(docs/14 §4.1)。
    profile_report = run_profile_baseline()
    report["profile"] = profile_report
    print(
        f"\n=== 画像抽取基线(规则通道) ===\n"
        f"精确率 {profile_report['precision']:.1%}  "
        f"召回率 {profile_report['recall']:.1%}  "
        f"F1 {profile_report['f1']:.1%}  "
        f"({profile_report['total_cases']} 条用例)"
    )
    for t, v in profile_report["per_type"].items():
        p = f"{v['precision']:.1%}" if v["precision"] is not None else "-"
        r = f"{v['recall']:.1%}" if v["recall"] is not None else "-"
        print(f"  {t:<12} 精确率 {p:>8}  召回率 {r:>8}")
    if profile_report["precision"] < args.min_profile_precision:
        failed.append(
            f"画像精确率 {profile_report['precision']:.1%} < {args.min_profile_precision:.0%}"
        )
    if profile_report["recall"] < args.min_profile_recall:
        failed.append(
            f"画像召回率 {profile_report['recall']:.1%} < {args.min_profile_recall:.0%}"
        )
    for f in profile_report.get("failures", []):
        print(f"  错: {f['input']!r} 期望 {f['expected']} 实际 {f['extracted']}")

    # ---- 落审计文件 ----
    report["passed"] = not failed
    report["failures"] = failed
    if not args.no_write:
        args.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = args.report_dir / f"intent_eval_{stamp}.json"
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n报告已写入: {out}")

    if failed:
        print("\n[FAIL] 未达标:")
        for f in failed:
            print("  -", f)
        return 1
    print("\n[OK] 全部达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
