"""会话记忆的运行参数(集中在一处,便于调参与测试覆盖)。"""

# 恢复会话时载入的最近消息条数
SESSION_RESTORE_LIMIT = 8

# 触发滚动摘要的阈值:会话内"尚未被摘要覆盖"的消息数超过它就压一次
SESSION_ROLLUP_THRESHOLD = 24

# 滚动摘要时保留在上下文里的最近消息条数
SESSION_ROLLUP_KEEP_RECENT = 8

# 长期记忆归纳的窗口:攒够几轮用户输入才归纳一次。
#
# 这是个**刻意的延迟**。单轮就抽的毛病是一句话足以生成一条长期结论
# （用户原话:记忆要综合来看才能沉淀）；窗口越大越不容易被单句带偏,
# 但聊得短就沉淀不下来。6 轮是"三轮反复要求短一点"能被看见的最小值。
PROFILE_DIGEST_WINDOW = 6


def session_restore_limit() -> int:
    return SESSION_RESTORE_LIMIT


def session_rollup_threshold() -> int:
    return SESSION_ROLLUP_THRESHOLD


def session_rollup_keep_recent() -> int:
    return SESSION_ROLLUP_KEEP_RECENT


def profile_digest_window() -> int:
    return PROFILE_DIGEST_WINDOW
