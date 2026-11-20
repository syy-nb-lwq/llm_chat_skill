"""把 `core/events.py` 的事件名生成为 `frontend/src/events.js`。

前端不能 import Python 常量，所以由脚本单向生成；生成结果提交进仓库，
`tests/test_event_contract.py` 用 `--check` 模式校验它没被手改过、也没过期。

用法：
    python scripts/gen_events_js.py           # 生成/覆盖
    python scripts/gen_events_js.py --check   # 只校验，不一致返回码 1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.events import AUDIT_EVENTS, EVENT_CONSTANTS, STREAM_EVENTS  # noqa: E402

TARGET = ROOT / "frontend" / "src" / "events.js"


def _js_key(name: str) -> str:
    """EV_MESSAGE_DELTA → MESSAGE_DELTA"""
    return name[len("EV_"):]


def render() -> str:
    lines = [
        "// 本文件由 scripts/gen_events_js.py 从 core/events.py 生成，请勿手改。",
        "// 改事件名请改 core/events.py，然后重新运行该脚本。",
        "",
        "export const EV = Object.freeze({",
    ]
    for name, value in EVENT_CONSTANTS.items():
        lines.append(f"  {_js_key(name)}: '{value}',")
    lines.append("})")
    lines.append("")

    def emit_set(var_name: str, values) -> None:
        lines.append(f"export const {var_name} = Object.freeze([")
        for value in sorted(values):
            lines.append(f"  '{value}',")
        lines.append("])")
        lines.append("")

    emit_set("STREAM_EVENTS", STREAM_EVENTS)
    emit_set("AUDIT_EVENTS", AUDIT_EVENTS)
    emit_set("ALL_EVENTS", STREAM_EVENTS | AUDIT_EVENTS)

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只校验不写入")
    args = parser.parse_args()

    expected = render()
    if args.check:
        if not TARGET.exists():
            print(f"缺少 {TARGET}", file=sys.stderr)
            return 1
        actual = TARGET.read_text(encoding="utf-8")
        if actual != expected:
            print(
                f"{TARGET} 与 core/events.py 不一致，请重新运行 scripts/gen_events_js.py",
                file=sys.stderr,
            )
            return 1
        print("events.js 与 core/events.py 一致")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(expected, encoding="utf-8", newline="\n")
    print(f"已生成 {TARGET}（{len(EVENT_CONSTANTS)} 个常量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
