"""汇总每轮的分阶段耗时 —— 回答"到底慢在哪"

用法:
    python scripts/trace_report.py                # 汇总全部
    python scripts/trace_report.py --last 50      # 只看最近 50 轮
    python scripts/trace_report.py --by-layer     # 按意图层分组
"""
import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.trace import get_trace_recorder  # noqa: E402


STAGES = ["plan", "tools", "generate", "first_token", "total"]


def _pct(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * q))))
    return s[idx]


def _stats(values: List[float]) -> Dict[str, float]:
    if not values:
        return {"n": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "avg": round(statistics.mean(values), 1),
        "p50": round(_pct(values, 0.50), 1),
        "p95": round(_pct(values, 0.95), 1),
        "max": round(max(values), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总每轮分阶段耗时")
    ap.add_argument("--last", type=int, default=0, help="只看最近 N 轮(0=全部)")
    ap.add_argument("--by-layer", action="store_true", help="按意图层分组统计")
    ap.add_argument("--path", type=str, default="", help="指定 trace 文件路径")
    args = ap.parse_args()

    recorder = get_trace_recorder()
    if args.path:
        recorder.path = Path(args.path)
    rows = recorder.read(limit=args.last)
    if not rows:
        print(f"没有 trace 记录: {recorder.path}")
        print("提示: 跑几轮对话后重试(trace 默认写到 memory/audit/traces.jsonl)")
        return 0

    print(f"共 {len(rows)} 轮  ({recorder.path})")

    if args.by_layer:
        groups = defaultdict(list)
        for r in rows:
            groups[r.get("intent_layer") or "?"].append(r)
        print("\n按意图层:")
        for layer, items in sorted(groups.items()):
            totals = [i.get("stage_ms", {}).get("total", 0) for i in items]
            firsts = [i.get("stage_ms", {}).get("first_token", 0) for i in items]
            st = _stats(totals)
            fs = _stats(firsts)
            print(
                f"  {layer:8s} n={st['n']:<4d} "
                f"total p50={st['p50']:>7.1f}ms p95={st['p95']:>7.1f}ms | "
                f"first_token p50={fs['p50']:>7.1f}ms"
            )
        return 0

    print("\n分阶段耗时(ms):")
    print(f"  {'阶段':<12}{'n':>5}{'avg':>10}{'p50':>10}{'p95':>10}{'max':>10}")
    for stage in STAGES:
        vals = [r.get("stage_ms", {}).get(stage, 0) for r in rows]
        vals = [v for v in vals if v > 0]
        st = _stats(vals)
        if st["n"] == 0:
            continue
        print(
            f"  {stage:<12}{st['n']:>5}{st['avg']:>10.1f}{st['p50']:>10.1f}"
            f"{st['p95']:>10.1f}{st['max']:>10.1f}"
        )

    errors = [r for r in rows if r.get("error")]
    if errors:
        print(f"\n错误 {len(errors)} 轮:")
        for e in errors[-5:]:
            print(f"  {e.get('trace_id', '?')}: {e.get('error', '')[:80]}")

    replies = [r.get("reply_chars", 0) for r in rows if r.get("reply_chars")]
    if replies:
        print(f"\n回答长度: 平均 {statistics.mean(replies):.0f} 字符")
    profiles = [r.get("profile_chars", 0) for r in rows]
    with_profile = sum(1 for p in profiles if p > 0)
    if with_profile:
        print(f"注入画像的轮次: {with_profile}/{len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
