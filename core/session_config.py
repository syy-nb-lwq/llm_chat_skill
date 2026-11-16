"""会话记忆的运行参数(集中在一处,便于调参与测试覆盖)。"""

# 恢复会话时载入的最近消息条数
SESSION_RESTORE_LIMIT = 8

# 触发滚动摘要的阈值:会话内"尚未被摘要覆盖"的消息数超过它就压一次
SESSION_ROLLUP_THRESHOLD = 24

# 滚动摘要时保留在上下文里的最近消息条数
SESSION_ROLLUP_KEEP_RECENT = 8


def session_restore_limit() -> int:
    return SESSION_RESTORE_LIMIT


def session_rollup_threshold() -> int:
    return SESSION_ROLLUP_THRESHOLD


def session_rollup_keep_recent() -> int:
    return SESSION_ROLLUP_KEEP_RECENT
