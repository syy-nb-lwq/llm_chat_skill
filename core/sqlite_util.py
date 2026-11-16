"""SQLite 连接工厂 —— 统一并发设置

之前 `memory_db.py` / `session_memory.py` 里有 15 处裸 `sqlite3.connect()`,
全部用默认参数:
- **回滚日志模式**(journal_mode=DELETE):写操作锁住整库,读也要等
- **默认 5 秒超时**:并发写会直接抛 `database is locked`
- 每次都新建连接,不复用

单机单人跑没事,**多用户并发写就会间歇性炸**。产品化必须统一。

这里只做三件事:
1. `journal_mode=WAL` —— 读写不互相阻塞
2. `busy_timeout` —— 遇到锁**等**而不是立刻报错
3. 统一的 row_factory / 事务边界
"""
import sqlite3
from pathlib import Path
from typing import Optional


#: 遇到写锁时最多等多久(毫秒)。默认 5000 太短,并发写会直接抛错。
BUSY_TIMEOUT_MS = 10000


def connect(
    db_path: Path,
    *,
    row_factory: Optional[type] = None,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """打开一个配好并发参数的连接。

    Args:
        db_path: 数据库文件路径
        row_factory: 行工厂(如 `sqlite3.Row`)
        busy_timeout_ms: 锁等待超时

    Returns:
        sqlite3.Connection
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=busy_timeout_ms / 1000.0,
        check_same_thread=False,
    )
    # WAL:读写不互相阻塞(对多用户并发是必须的)
    # 注意:内存库(:memory:)不支持 WAL,失败不致命
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    # 遇到锁时等待而不是立刻抛 database is locked
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    # NORMAL 在 WAL 下是安全且快的(崩溃最多丢最后一个事务)
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn
