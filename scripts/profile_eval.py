"""画像抽取基线 —— 规则通道的质量度量(不需要 LLM)

## 为什么要有这个

`docs/14` §4.1 指出:项目缺一个**真实基线**,导致"改提示词/改规则到底是变好
还是变坏"说不清。L1 意图那条线需要真实 key 才能建基线,
但**画像抽取是纯规则**的 —— 现在就能建基线,不用等 key。

有了基线,以后改 `core/memory_writer.py` 的规则表,就能立刻回答:
"精确率掉了还是召回率掉了?掉的那条是我有意放的吗?"

## 指标

- **精确率** = 抽出的条目里"确实该抽"的比例 —— **这是生死线**。
  一条错误的画像会持续影响此后每一轮(docs/13 §2.8「宁缺毋滥」)。
- **召回率** = 该抽的条目里"真的抽到了"的比例 —— 规则有天花板,
  低一点可以接受(docs/13 D14 已经说明)。

## 跑法

```bash
python scripts/profile_eval.py                    # 打印报告
python scripts/profile_eval.py --min-precision 0.9 --min-recall 0.7
python scripts/profile_eval.py --write            # 落审计文件
```

退出码:0 = 达标,1 = 有指标未达标,2 = 环境问题
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 报告工具:日志刷屏会盖住表格,默认只留警告
os.environ.setdefault("LOG_LEVEL", "WARNING")

from core.memory_writer import extract_candidates  # noqa: E402

EVAL_FILE = ROOT / "tests" / "data" / "profile_eval.jsonl"
DEFAULT_REPORT_DIR = ROOT / "memory" / "audit"

#: 画像类型
PROFILE_TYPES = ("preference", "habit", "method", "constraint", "fact")


def load_cases(path: Path = EVAL_FILE):
    cases = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cases.append(json.loads(line))
    return cases


def _matches(candidate, expected) -> bool:
    """一条候选是否命中一个期望项(类型相同 + 包含语义锚点)"""
    if candidate.type != expected["type"]:
        return False
    return expected["contains"] in candidate.content


def evaluate_case(case):
    """返回 (命中数, 期望数, 抽出的条目, 多余的条目)"""
    expected = case.get("expect") or []
    candidates = extract_candidates(case["input"])

    matched_expected = set()
    matched_candidates = set()
    for ci, cand in enumerate(candidates):
        for ei, exp in enumerate(expected):
            if ei in matched_expected:
                continue
            if _matches(cand, exp):
                matched_expected.add(ei)
                matched_candidates.add(ci)

    extra = [c for i, c in enumerate(candidates) if i not in matched_candidates]
    return len(matched_expected), len(expected), candidates, extra


def evaluate(cases):
    tp = 0          # 命中的期望项数
    total_expected = 0
    total_candidates = 0
    matched_candidates = 0
    per_type = defaultdict(lambda: {"tp": 0, "expected": 0, "candidates": 0, "matched": 0})
    failures = []

    for case in cases:
        hit, exp_total, candidates, extra = evaluate_case(case)
        tp += hit
        total_expected += exp_total
        total_candidates += len(candidates)
        matched_candidates += len(candidates) - len(extra)

        for exp in (case.get("expect") or []):
            per_type[exp["type"]]["expected"] += 1
        for cand in candidates:
            per_type[cand.type]["candidates"] += 1
        for exp in (case.get("expect") or []):
            # 该类型至少有一条候选命中才算 recall 命中
            if any(_matches(c, exp) for c in candidates):
                per_type[exp["type"]]["tp"] += 1
        for cand in candidates:
            if any(_matches(cand, exp) for exp in (case.get("expect") or [])):
                per_type[cand.type]["matched"] += 1

        if hit < exp_total or extra:
            failures.append(
                {
                    "input": case["input"],
                    "expected": [f"{e['type']}:{e['contains']}" for e in (case.get("expect") or [])],
                    "extracted": [f"{c.type}:{c.content}" for c in candidates],
                    "missing": exp_total - hit,
                    "extra": len(extra),
                    "note": case.get("note", ""),
                }
            )

    precision = matched_candidates / total_candidates if total_candidates else 1.0
    recall = tp / total_expected if total_expected else 1.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "total_cases": len(cases),
        "total_expected": total_expected,
        "total_candidates": total_candidates,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "per_type": {
            t: {
                "precision": round(
                    v["matched"] / v["candidates"], 4
                ) if v["candidates"] else None,
                "recall": round(v["tp"] / v["expected"], 4) if v["expected"] else None,
                "expected": v["expected"],
                "extracted": v["candidates"],
            }
            for t, v in sorted(per_type.items())
        },
        "failures": failures,
    }


def render_report(report) -> str:
    lines = []
    lines.append("画像抽取基线(规则通道)")
    lines.append(f"用例数: {report['total_cases']}  "
                 f"期望条目: {report['total_expected']}  "
                 f"实际抽出: {report['total_candidates']}")
    lines.append("")
    lines.append(f"精确率 precision: {report['precision']:.1%}   "
                 f"召回率 recall: {report['recall']:.1%}   "
                 f"F1: {report['f1']:.1%}")
    lines.append("")
    lines.append("分类型:")
    header = f"{'类型':<12}{'精确率':>10}{'召回率':>10}{'期望':>8}{'抽出':>8}"
    lines.append(header)
    lines.append("-" * len(header.encode("utf-8").decode("utf-8")))
    for t, v in report["per_type"].items():
        p = f"{v['precision']:.1%}" if v["precision"] is not None else "-"
        r = f"{v['recall']:.1%}" if v["recall"] is not None else "-"
        lines.append(f"{t:<12}{p:>10}{r:>10}{v['expected']:>8}{v['extracted']:>8}")
    if report["failures"]:
        lines.append("")
        lines.append(f"未达标用例({len(report['failures'])} 条):")
        for f in report["failures"]:
            tag = []
            if f["missing"]:
                tag.append(f"漏 {f['missing']}")
            if f["extra"]:
                tag.append(f"多 {f['extra']}")
            lines.append(f"  - {f['input']!r} [{','.join(tag)}]")
            lines.append(f"      期望: {f['expected']}")
            lines.append(f"      实际: {f['extracted']}")
            if f["note"]:
                lines.append(f"      备注: {f['note']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="画像抽取基线")
    parser.add_argument("--eval-file", default=str(EVAL_FILE))
    parser.add_argument("--min-precision", type=float, default=0.95)
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--write", action="store_true", help="落审计文件")
    parser.add_argument("--no-write", action="store_true", help="不落审计文件")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    args = parser.parse_args(argv)

    path = Path(args.eval_file)
    if not path.exists():
        print(f"[ERROR] 评估集不存在: {path}", file=sys.stderr)
        return 2

    cases = load_cases(path)
    if not cases:
        print("[ERROR] 评估集是空的", file=sys.stderr)
        return 2

    report = evaluate(cases)
    print(render_report(report))

    failed = []
    if report["precision"] < args.min_precision:
        failed.append(f"精确率 {report['precision']:.1%} < {args.min_precision:.0%}")
    if report["recall"] < args.min_recall:
        failed.append(f"召回率 {report['recall']:.1%} < {args.min_recall:.0%}")

    if not args.no_write:
        out_dir = Path(args.report_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = out_dir / f"profile_eval_{stamp}.json"
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
