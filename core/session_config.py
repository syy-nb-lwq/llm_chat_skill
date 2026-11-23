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

# 归纳窗口的"陈旧兜底"：窗口没满，但距最后一轮超过这么多秒，
# 下次对话时先把攒的轮次归纳掉。
# 为什么需要：flush 只在用户主动 reset 时触发，聊 2~3 轮就走的短会话
# 永远攒不满 6 轮，轮次会烂在内存窗口里（实机：聊了出差报销什么都没沉淀）。
# 15 分钟能覆盖"聊完就走 / 隔一阵回来"，又不会把连续对话的正常停顿误判。
PROFILE_DIGEST_IDLE_S = 15 * 60


def profile_digest_idle_s() -> int:
    return PROFILE_DIGEST_IDLE_S


def session_restore_limit() -> int:
    return SESSION_RESTORE_LIMIT


def session_rollup_threshold() -> int:
    return SESSION_ROLLUP_THRESHOLD


def session_rollup_keep_recent() -> int:
    return SESSION_ROLLUP_KEEP_RECENT


def profile_digest_window() -> int:
    return PROFILE_DIGEST_WINDOW
