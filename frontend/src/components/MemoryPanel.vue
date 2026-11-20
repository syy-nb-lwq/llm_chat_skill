<template>
  <div class="memory-panel">
    <div class="header-row">
      <h4>🧠 我记住了你什么</h4>
      <div class="actions">
        <button class="reload" @click="refresh" :disabled="loading">刷新</button>
        <button
          class="forget-all"
          @click="confirmForgetAll"
          :disabled="loading || !entries.length"
        >
          忘掉全部
        </button>
      </div>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div v-if="!sessionId" class="hint-box">
      <p>连接后即可查看系统记住的关于你的信息。</p>
    </div>

    <template v-else>
      <!-- 正在注入的内容:让用户看清"什么在影响我的回答" -->
      <div v-if="injectedBlock" class="injected-box">
        <div class="injected-title">此刻正在影响你回答的内容</div>
        <pre class="injected-content">{{ injectedBlock }}</pre>
        <div class="injected-meta">
          {{ injectedChars }} 字符 · 超出部分不会注入(docs/13 §2.8 预算)
        </div>
      </div>

      <!-- 记忆全景：让用户看清"系统实际记得什么" -->
      <div v-if="overview" class="overview-box">
        <div class="overview-title">记忆全景</div>
        <div class="overview-grid">
          <div class="ov-item">
            <span class="ov-num">{{ overview.short_term.count }}</span>
            <span class="ov-label">本会话消息</span>
          </div>
          <div class="ov-item">
            <span class="ov-num">{{ overview.profile.count }}</span>
            <span class="ov-label">长期画像</span>
          </div>
          <div class="ov-item">
            <span class="ov-num">{{ overview.lessons.count }}</span>
            <span class="ov-label">失败教训</span>
          </div>
          <div class="ov-item">
            <span class="ov-num">{{ overview.skill_drafts.count }}</span>
            <span class="ov-label">待批方法论</span>
          </div>
          <div class="ov-item">
            <span class="ov-num">{{ overview.patches.count }}</span>
            <span class="ov-label">待批补丁</span>
          </div>
        </div>
      </div>

      <div v-if="loading && !entries.length" class="empty">加载中...</div>

      <div v-else-if="!entries.length" class="empty">
        还没有抽取到长期画像。
        <p class="hint">
          画像由 LLM 从你的话里抽取，但只记长期稳定的东西：
          身份、长期偏好、硬性约束、长期目标。
        </p>
        <p class="hint">
          单纯下任务（比如「帮我生成一个视频」）不会产生画像 —— 这是刻意的：
          否则每一句指令都会变成"偏好"，污染之后所有的回答。
        </p>
        <p class="hint">
          想让它记住，直接说「以后…」「我一向…」「记住…」。
          抽到的内容会显示在上面的注入块里，你可以随时删掉。
        </p>
      </div>

      <template v-else>
        <div class="filter-row">
          <button
            v-for="t in typeFilters"
            :key="t.key"
            class="filter-chip"
            :class="{ active: filter === t.key }"
            @click="filter = t.key"
          >
            {{ t.label }}
            <span class="chip-count">{{ countByType(t.key) }}</span>
          </button>
        </div>

        <div class="entry-list">
          <div v-for="e in visibleEntries" :key="e.id" class="entry-card">
            <div class="entry-header">
              <span class="type-badge" :class="e.type">{{ e.label }}</span>
              <span class="entry-time">{{ formatTime(e.created_at) }}</span>
              <span class="confidence" :title="'置信度 ' + e.confidence">
                {{ Math.round(e.confidence * 100) }}%
              </span>
              <button
                class="forget-btn"
                @click="forgetOne(e)"
                :disabled="loading"
                title="让系统忘掉这一条"
              >
                忘掉
              </button>
            </div>
            <div class="entry-content">{{ e.content }}</div>
            <!-- 来源：一条"综合"出来的结论必须能说清它综合了什么，
                 否则用户没法判断它是真的反复出现，还是从一句话推出来的 -->
            <div v-if="e.evidence_turns && e.evidence_turns.length > 1" class="entry-source">
              综合自 {{ e.evidence_turns.length }} 轮（第 {{ e.evidence_turns.join('、') }} 轮）
            </div>
            <div v-else-if="e.applies_to" class="entry-source scope">
              仅适用于 {{ e.applies_to }}
            </div>
          </div>
        </div>

        <p v-if="visibleEntries.length < entries.length" class="hint">
          只显示前 {{ visibleEntries.length }} 条 —— 共 {{ entries.length }} 条。
        </p>
      </template>
    </template>
  </div>
</template>

<script>
import { memoryApi } from '../api/client'
import { wsService } from '../websocket'
import { EV } from '../events'

export default {
  name: 'MemoryPanel',
  data() {
    return {
      entries: [],
      injectedBlock: '',
      injectedChars: 0,
      filter: 'all',
      loading: false,
      error: '',
      typeFilters: [
        { key: 'all', label: '全部' },
        { key: 'preference', label: '偏好' },
        { key: 'habit', label: '习惯' },
        { key: 'method', label: '方法' },
        { key: 'constraint', label: '雷区' },
        { key: 'fact', label: '事实' },
      ],
      // 记忆全景：短期/长期/经验各层到底有多少东西
      // 只展示画像时，用户会以为"系统什么都没记" —— 其实别的层有数据
      overview: null,
    }
  },
  computed: {
    sessionId() {
      return (wsService.session && wsService.session.session_id) || ''
    },
    visibleEntries() {
      if (this.filter === 'all') return this.entries
      return this.entries.filter((e) => e.type === this.filter)
    },
  },
  mounted() {
    if (this.sessionId) {
      this.refresh()
    } else {
      // 身份是 WS 连接后签发的 —— 连上再拉
      wsService.on(EV.CONNECTED, this.refresh)
    }
  },
  beforeUnmount() {
    wsService.off(EV.CONNECTED, this.refresh)
  },
  methods: {
    async refresh() {
      if (!this.sessionId) return
      this.loading = true
      this.error = ''
      try {
        const data = await memoryApi.profile(this.sessionId)
        this.entries = data.entries || []
        this.injectedBlock = data.injected_block || ''
        this.injectedChars = data.injected_chars || 0

        // 全景是"锦上添花"：拿不到不影响画像列表
        try {
          this.overview = await memoryApi.overview(this.sessionId)
        } catch (e) {
          this.overview = null
        }
      } catch (e) {
        this.error = `加载失败: ${e.message}`
      } finally {
        this.loading = false
      }
    },

    countByType(key) {
      if (key === 'all') return this.entries.length
      return this.entries.filter((e) => e.type === key).length
    },

    async forgetOne(entry) {
      if (!confirm(`确定让系统忘掉这条吗?\n\n「${entry.content}」`)) return
      this.loading = true
      this.error = ''
      try {
        await memoryApi.forget(this.sessionId, entry.id)
        // 本地先移除,再刷新 —— 界面立刻响应
        this.entries = this.entries.filter((e) => e.id !== entry.id)
        await this.refresh()
      } catch (e) {
        this.error = `删除失败: ${e.message}`
      } finally {
        this.loading = false
      }
    },

    async confirmForgetAll() {
      if (!confirm(`确定忘掉关于你的一切吗?\n\n共 ${this.entries.length} 条,删了就找不回来了。`)) return
      this.loading = true
      this.error = ''
      try {
        await memoryApi.forgetAll(this.sessionId)
        this.entries = []
        await this.refresh()
      } catch (e) {
        this.error = `清空失败: ${e.message}`
      } finally {
        this.loading = false
      }
    },

    formatTime(t) {
      if (!t) return ''
      try {
        const d = new Date(t)
        if (Number.isNaN(d.getTime())) return ''
        return d.toLocaleString('zh-CN', {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      } catch (e) {
        return ''
      }
    },
  },
}
</script>

<style scoped>
.memory-panel {
  padding: 8px;
  font-size: 13px;
}
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.header-row h4 {
  margin: 0;
  font-size: 14px;
}
.actions {
  display: flex;
  gap: 6px;
}
.reload,
.forget-all {
  font-size: 11px;
  padding: 3px 8px;
  border: 1px solid #ddd;
  border-radius: 4px;
  background: #fff;
  cursor: pointer;
}
.reload:hover:not(:disabled) {
  background: #f5f5f5;
}
.forget-all {
  color: #c62828;
  border-color: #ef9a9a;
}
.forget-all:hover:not(:disabled) {
  background: #ffebee;
}
.reload:disabled,
.forget-all:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 正在注入的内容 */
.injected-box {
  background: #e8f5e9;
  border-left: 3px solid #66bb6a;
  padding: 8px 10px;
  border-radius: 4px;
  margin-bottom: 12px;
}
.injected-title {
  font-size: 11px;
  font-weight: bold;
  color: #2e7d32;
  margin-bottom: 4px;
}
.injected-content {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: #333;
  font-family: inherit;
}
.injected-meta {
  font-size: 10px;
  color: #66bb6a;
  margin-top: 4px;
}

/* 过滤 */
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}
.filter-chip {
  font-size: 11px;
  padding: 2px 8px;
  border: 1px solid #ddd;
  border-radius: 10px;
  background: #fff;
  cursor: pointer;
}
.filter-chip.active {
  background: #667eea;
  color: #fff;
  border-color: #667eea;
}
.chip-count {
  opacity: 0.7;
  margin-left: 3px;
}

/* 条目 */
.entry-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.entry-card {
  border: 1px solid #e0e0e0;
  border-radius: 5px;
  padding: 7px 9px;
  background: #fff;
}
.entry-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.type-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 3px;
  background: #eef;
  color: #5566cc;
}
.type-badge.preference { background: #e3f2fd; color: #1565c0; }
.type-badge.habit { background: #f3e5f5; color: #7b1fa2; }
.type-badge.method { background: #e8f5e9; color: #2e7d32; }
.type-badge.constraint { background: #ffebee; color: #c62828; }
.type-badge.fact { background: #fff8e1; color: #f57f17; }
.entry-time {
  font-size: 10px;
  color: #999;
}
.confidence {
  font-size: 10px;
  color: #bbb;
}
.forget-btn {
  margin-left: auto;
  font-size: 10px;
  padding: 1px 6px;
  border: 1px solid #ef9a9a;
  border-radius: 3px;
  background: #fff;
  color: #c62828;
  cursor: pointer;
}
.forget-btn:hover:not(:disabled) {
  background: #ffebee;
}
.forget-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.entry-content {
  font-size: 12px;
  line-height: 1.5;
  color: #333;
  word-break: break-word;
}

/* 来源标注：比正文更轻，但比"看不见"重要 —— 它是这条记忆的可信度凭据 */
.entry-source {
  margin-top: 4px;
  font-size: 11px;
  color: #8a8f98;
}
.entry-source.scope {
  color: #b07b2a;
}

.empty {
  text-align: center;
  color: #999;
  padding: 16px 8px;
  font-size: 12px;
}
.hint {
  font-size: 11px;
  color: #999;
  margin: 4px 0 0;
  line-height: 1.5;
}
.hint-box {
  background: #f5f5f5;
  padding: 10px;
  border-radius: 4px;
  font-size: 12px;
  color: #666;
}
.hint-box p {
  margin: 0 0 4px;
}
.error {
  background: #ffebee;
  color: #c62828;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
  margin-bottom: 8px;
}
.overview-box {
  margin: 10px 0 14px;
  padding: 10px 12px;
  border: 1px solid rgba(120, 120, 120, 0.25);
  border-radius: 8px;
  background: rgba(120, 120, 120, 0.06);
}
.overview-title {
  font-size: 12px;
  opacity: 0.75;
  margin-bottom: 8px;
}
.overview-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.ov-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 62px;
}
.ov-num {
  font-size: 18px;
  font-weight: 600;
  line-height: 1.2;
}
.ov-label {
  font-size: 11px;
  opacity: 0.7;
}
</style>
