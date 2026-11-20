"""经验数据迁移：MemoryStore 的历史 JSON 目录树 → `experience` 表。

## 为什么需要它

`core/memory.py::MemoryStore` 已经改成 SQLite 载体（表 `experience`），
但老部署的机器上还留着 `memory/failures/*.json`、`memory/successes/*.jsonl`、
`memory/skill_patches/pending/*.json`。这个脚本把它们一次性导入新表。

新库**不会**自动扫描老目录 —— 自动迁移一旦猜错就没人知道发生了什么。
所以必须显式跑这个脚本，且默认 dry-run。

## 安全设计

1. **默认 dry-run**：不加 `--apply` 只报告会迁什么，不写任何东西。
2. **先备份**：`--apply` 会先把整个 `memory/` 复制成 `memory.bak-<时间戳>`。
3. **幂等**：按 `id` 或 `content_hash` 判重，重复运行不会重复插入。
4. **可回滚**：`--rollback <batch_id>` 按批次精确删除本次导入的记录。

## 用法

```bash
python scripts/migrate_experience_to_sqlite.py --dry-run    # 看会迁什么
python scripts/migrate_experience_to_sqlite.py --apply      # 迁移 + 备份
python scripts/migrate_experience_to_sqlite.py --rollback <batch_id>
```

## 迁移后

JSON 目录不再参与读写，可以留着当历史快照，也可以删掉（有备份）。
想重新导出一份人可读的原文，用 `MemoryStore.export_json()`。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.memory import MemoryStore  # noqa: E402


def _backup(base: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = base.parent / f"{base.name}.bak-{stamp}"
    if base.exists():
        shutil.copytree(base, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=str(ROOT / "memory"),
                        help="历史 JSON 的根目录（默认 memory/）")
    parser.add_argument("--db-base", default=None,
                        help="新库所在目录（默认与 --base 相同）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入（默认行为）")
    parser.add_argument("--apply", action="store_true", help="真正迁移")
    parser.add_argument("--rollback", metavar="BATCH_ID", help="回滚某个导入批次")
    args = parser.parse_args()

    base = Path(args.base)
    store = MemoryStore(base_path=Path(args.db_base) if args.db_base else base)

    if args.rollback:
        removed = store.rollback_batch(args.rollback)
        print(f"已回滚 {removed} 条（batch_id={args.rollback}）")
        return 0

    batch_id = f"import-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    if not args.apply:
        # dry-run：在临时库上跑同一套导入逻辑，只取报告，不碰真实库
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = MemoryStore(base_path=Path(tmp))
            report = probe.import_json(base, batch_id=batch_id)

        print(f"扫描 {base}: 共 {report['scanned']} 条待迁")
        for kind, n in report["by_kind"].items():
            print(f"  {kind:10} {n}")
        print("\n[dry-run] 未写入任何数据。确认无误后加 --apply。")
        return 0

    backup = _backup(base) if base.exists() else None
    report = store.import_json(base, batch_id=batch_id)

    print(f"batch_id: {batch_id}")
    if backup:
        print(f"备份位置: {backup}")
    print(f"扫描 {report['scanned']} 条，新插入 {report['inserted']} 条"
          f"（跳过 {report['skipped']} 条已存在）")
    for kind, n in report["by_kind"].items():
        print(f"  {kind:10} {n}")
    print(f"\n新库: {store.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
