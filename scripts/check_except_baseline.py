"""异常处理棘轮：`except Exception` 只能减少，不能增加。

## 为什么不是"一次清完"

仓库里有 370+ 处 `except Exception`。它们不是同一个问题：
有的是"观测失败不能拖垮业务"（合理），有的是"把真实故障吞成 warning"（有害）。
逐个判断需要业务上下文，一次性大改必然改错。

所以先上**棘轮**：把当前数量记成基线，CI 断言不超过它。
新增一处裸捕获就会红 —— 逼着人写"捕获什么、为什么"。清理一处时把基线调下来。

用法：
    python scripts/check_except_baseline.py           # 校验
    python scripts/check_except_baseline.py --update  # 清理后下调基线
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".except_baseline"

#: 统计范围：业务代码。测试里大量 fixture 用宽捕获，不纳入。
DIRS = ["core", "agents", "tools", "backend", "infra"]

#: 只统计裸 `except Exception`（含 `as e`），不统计 `except (A, B)`
NEEDLE = "except Exception"


def count_occurrences() -> tuple[int, list[tuple[str, int]]]:
    """按 AST 数真正的 `except Exception`，不数字符串/文档里的示例文本。

    早期版本用文本匹配，结果 `core/exception_policy.py` 的文档里写了一句
    `except Exception: pass` 作为反例，就被算成新增两处 —— 假阳性。
    """
    import ast

    total = 0
    per_file: list[tuple[str, int]] = []
    for d in DIRS:
        for path in (ROOT / d).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            n = 0
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                exc = node.type
                # bare except / except (A, B) 不算；只数 except Exception
                if isinstance(exc, ast.Name) and exc.id == "Exception":
                    n += 1
            if n:
                total += n
                per_file.append((path.relative_to(ROOT).as_posix(), n))
    return total, sorted(per_file, key=lambda x: -x[1])


def read_baseline() -> int:
    if not BASELINE_FILE.exists():
        return -1
    raw = BASELINE_FILE.read_text(encoding="utf-8").strip()
    return int(raw) if raw.isdigit() else -1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="把当前数量写为基线")
    args = parser.parse_args()

    total, per_file = count_occurrences()
    baseline = read_baseline()

    if args.update:
        BASELINE_FILE.write_text(f"{total}\n", encoding="utf-8")
        print(f"基线已更新：{total}")
        return 0

    if baseline < 0:
        print(f"缺少基线文件 {BASELINE_FILE.name}，先运行 --update", file=sys.stderr)
        return 1

    print(f"当前 {total} 处 except Exception，基线 {baseline}")
    if total > baseline:
        top = "\n".join(f"  {n:4d}  {f}" for f, n in per_file[:10])
        added = total - baseline
        print(
            f"\n新增了 {added} 处裸捕获。请改成捕获具体异常，"
            f"或在注释里写明为什么必须宽捕获：\n{top}",
            file=sys.stderr,
        )
        return 1

    if total < baseline:
        print(f"比基线少了 {baseline - total} 处 —— 记得运行 --update 下调基线，防止回弹。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
