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

    <div v-if="!hasToken" class="hint-box">
      <p>连接后即可查看系统记住的关于你的信息。</p>
      <p class="hint">长期记忆按用户隔离 —— 只有你能看到自己被记住的内容。</p>
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

      <div v-if="loading && !entries.length" class="empty">加载中...</div>

      <div v-else-if="!entries.length" class="empty">
        还没有记住关于你的任何信息。
        <p class="hint">你可以在对话里说「以后回答都用中文」这类偏好,系统会自动记住。</p>
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
import { API_BASE } from '../config'
import { wsService } from '../websocket'

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
    }
  },
  computed: {
    hasToken() {
      return !!(wsService.session && wsService.session.token)
    },
    visibleEntries() {
      if (this.filter === 'all') return this.entries
      return this.entries.filter((e) => e.type === this.filter)
    },
  },
  mounted() {
    if (this.hasToken) {
      this.refresh()
    } else {
      // 身份是 WS 连接后签发的 —— 连上再拉
      wsService.on('connected', this.refresh)
    }
  },
  beforeUnmount() {
    wsService.off('connected', this.refresh)
  },
  methods: {
    _authHeaders() {
      const token = (wsService.session && wsService.session.token) || ''
      return token ? { Authorization: `Bearer ${token}` } : {}
    },

    async refresh() {
      if (!this.hasToken) return
      this.loading = true
      this.error = ''
      try {
        const res = await fetch(`${API_BASE}/api/memory/profile`, {
          headers: this._authHeaders(),
        })
        if (!res.ok) throw new Error(await res.text())
        const data = await res.json()
        this.entries = data.entries || []
        this.injectedBlock = data.injected_block || ''
        this.injectedChars = data.injected_chars || 0
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
        const res = await fetch(`${API_BASE}/api/memory/profile/${encodeURIComponent(entry.id)}`, {
          method: 'DELETE',
          headers: this._authHeaders(),
        })
        if (!res.ok) throw new Error(await res.text())
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
        const res = await fetch(`${API_BASE}/api/memory/profile`, {
          method: 'DELETE',
          headers: this._authHeaders(),
        })
        if (!res.ok) throw new Error(await res.text())
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
</style>
