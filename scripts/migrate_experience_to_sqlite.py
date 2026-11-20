"""经验数据迁移：MemoryStore(JSON) → MemoryDB(SQLite)。

## 为什么需要它

长期记忆目前有两个载体（职责互斥，不是副本）：

- `MemoryStore`（JSON）：失败诊断 / 成功模式 / 待审补丁
- `MemoryDB`（SQLite）：用户画像

`docs/19` §07 要求"critic 与 API 读到同一张表"。这个脚本把经验数据
导入 SQLite 的 `experience` 表，并提供 dry-run / 备份 / 回滚。

## 安全设计

1. **默认 dry-run**：不加 `--apply` 只报告会迁什么，不写任何东西。
2. **先备份**：`--apply` 会先把 JSON 目录复制成 `.bak-<时间戳>`。
3. **幂等**：用 `source_path + content hash` 做唯一键，重复运行不会重复插入。
4. **可回滚**：`--rollback` 删除本次迁移写入的记录（按 batch_id 精确删除）。

## 用法

```bash
# 1. 看看会迁什么
python scripts/migrate_experience_to_sqlite.py --dry-run

# 2. 真正迁移（自动备份）
python scripts/migrate_experience_to_sqlite.py --apply

# 3. 出问题回滚
python scripts/migrate_experience_to_sqlite.py --rollback <batch_id>
```

**注意**：脚本只搬数据，不改调用方。切换 critic/API 到新表是后续步骤，
需要单独提交并跑全量回归 —— 不要在迁移当天同时改读写路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.memory_db import get_memory_db  # noqa: E402

EXPERIENCE_DIRS = ("failures", "successes", "skill_patches")


def _content_hash(payload: Dict[str, Any]) -> str:
    """用规范化 JSON 算 hash，作为幂等键。"""
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def collect_records(base: Path) -> List[Dict[str, Any]]:
    """扫描 JSON 目录，返回待迁移记录。"""
    records = []
    for kind in EXPERIENCE_DIRS:
        directory = base / kind
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            records.append({
                "kind": kind,
                "source_path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "content": json.dumps(payload, ensure_ascii=False, default=str),
                "content_hash": _content_hash(payload),
                "created_at": str(payload.get("timestamp") or ""),
            })
    return records


def ensure_table() -> None:
    """建经验表（幂等）。"""
    db = get_memory_db()
    conn = db._connect()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS experience (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                source_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(kind, content_hash)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_kind ON experience(kind)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_batch ON experience(batch_id)"
        )


def apply(records: List[Dict[str, Any]], base: Path) -> str:
    """真正写入，返回 batch_id。"""
    ensure_table()
    batch_id = f"mig-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"

    # 备份原始 JSON 目录
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = base.parent / f"{base.name}.bak-{stamp}"
    if base.exists():
        shutil.copytree(base, backup)

    db = get_memory_db()
    conn = db._connect()
    inserted = 0
    with conn:
        for r in records:
            cur = conn.execute(
                """INSERT OR IGNORE INTO experience
                   (id, kind, source_path, content_hash, content, batch_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"exp-{r['content_hash']}",
                    r["kind"],
                    r["source_path"],
                    r["content_hash"],
                    r["content"],
                    batch_id,
                    r["created_at"],
                ),
            )
            inserted += cur.rowcount

    print(f"batch_id: {batch_id}")
    print(f"备份位置: {backup}")
    print(f"新插入 {inserted} 条（跳过 {len(records) - inserted} 条重复）")
    return batch_id


def rollback(batch_id: str) -> None:
    """删除某个批次写入的记录。"""
    db = get_memory_db()
    conn = db._connect()
    with conn:
        cur = conn.execute("DELETE FROM experience WHERE batch_id = ?", (batch_id,))
    print(f"已回滚 {cur.rowcount} 条（batch_id={batch_id}）")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=str(ROOT / "memory"),
                        help="MemoryStore 的根目录（默认 memory/）")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入（默认行为）")
    parser.add_argument("--apply", action="store_true", help="真正迁移")
    parser.add_argument("--rollback", metavar="BATCH_ID", help="回滚某个批次")
    args = parser.parse_args()

    if args.rollback:
        rollback(args.rollback)
        return 0

    base = Path(args.base)
    records = collect_records(base)
    by_kind: Dict[str, int] = {}
    for r in records:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1

    print(f"扫描 {base}: 共 {len(records)} 条")
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind:16} {n}")

    if args.apply:
        apply(records, base)
    else:
        print("\n[dry-run] 未写入任何数据。确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
