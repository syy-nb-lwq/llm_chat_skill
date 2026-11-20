"""给存量草稿补算 value 标记（只加字段、不改正文，先备份）。

新价值判据上线前攒下的 15 条草稿没有 value 字段，界面显示不出判定。
这个脚本只读重算、幂等回写；跑完界面刷新即可看到每条的"有价值/无价值"。

用法：python scripts/backfill_draft_value.py
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.skill_harvest import assess_value  # noqa: E402

DRAFTS = Path(__file__).resolve().parent.parent / "memory" / "skills" / "drafts"


def main():
    files = sorted(DRAFTS.glob("*.json"))
    if not files:
        print("没有草稿，退出")
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DRAFTS.parent / f"drafts.bak-{stamp}"
    shutil.copytree(DRAFTS, backup)
    print(f"已备份 → {backup}")

    changed = 0
    valuable = 0
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        if "value" in d:  # 幂等：已有判定不动
            continue
        method = (d.get("skill") or {}).get("method") or ""
        v = assess_value(method)
        d["value"] = v
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        changed += 1
        valuable += 1 if v["valuable"] else 0

    print(f"补算 {changed} 条（有价值 {valuable} / 无价值 {changed - valuable}）")


if __name__ == "__main__":
    main()
