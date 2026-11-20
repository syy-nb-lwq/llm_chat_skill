"""异常降级策略 —— 让"有意吞掉"变成"留痕的降级"。

## 三类异常（ARCHITECTURE.md §8.6）

1. **可降级**：外部依赖 / 观测 / 缓存失败。用 `downgrade()` 记 debug 后走默认值。
   它不改变原行为（以前是 `except Exception: pass`），但至少留下"这里失败过"的痕迹。
2. **必须冒泡**：数据完整性 / schema 不一致 / 配置错误。捕获窄异常类型并 `raise`，
   或干脆不捕获 —— 让真实故障暴露，而不是变成一行 warning。
3. **必须重试**：网络 / 子进程 / 临时 I/O。交给具体的重试逻辑（如 `hub` 的 retry），
   不要在业务层用宽捕获假装成功。

## 用法

```python
from core.exception_policy import downgrade

try:
    observer.emit(...)
except Exception as exc:
    downgrade(exc, where="core/agent.py::emit")
```

**为什么记 debug 而不是 warning**：这些降级点是设计意图（观测失败不能拖垮业务），
用 warning 会淹没真正的问题。但完全静默（`pass`）会让"功能悄悄失效"无从排查 ——
debug 是两者的平衡：默认不吵，排查时能打开。
"""
from __future__ import annotations

import logging

_logger = logging.getLogger("skill_agent.degraded")


def downgrade(exc: BaseException, *, where: str = "", detail: str = "") -> None:
    """记录一次"有意降级"。

    Args:
        exc: 被吞掉的异常
        where: 降级点，惯例是 `模块::函数`
        detail: 可选的业务上下文（比如"画像读取失败，使用空串"）
    """
    _logger.debug(
        "[降级] %s: %s: %s%s",
        where or "unknown",
        type(exc).__name__,
        exc,
        f" | {detail}" if detail else "",
    )
