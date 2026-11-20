<template>
  <div class="session-panel">
    <!-- 「新对话」放在列表最上面：用户找它的时候眼睛先落在列表头部 -->
    <button
      class="new-btn"
      :disabled="busy"
      :title="busy ? '正在生成回答，等它说完再开新对话' : '开一条全新会话（当前这条会留在列表里）'"
      @click="$emit('create')"
    >＋ 新对话</button>

    <div v-if="loading" class="hint">加载中…</div>
    <div v-else-if="!items.length" class="hint">还没有对话</div>

    <div v-else class="session-list">
      <div
        v-for="s in items"
        :key="s.session_id"
        class="session-item"
        :class="{ active: s.session_id === activeId }"
        @click="select(s)"
      >
        <div class="item-main">
          <div class="item-title" :title="s.title || '新对话'">{{ s.title || '新对话' }}</div>
          <div class="item-meta">{{ meta(s) }}</div>
        </div>
        <button
          class="item-del"
          title="删除这条对话（不可恢复）"
          :disabled="busy"
          @click.stop="$emit('remove', s.session_id)"
        >✕</button>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'SessionList',
  props: {
    /** [{session_id, title, message_count, updated_at}]，父组件按新→旧给 */
    sessions: { type: Array, default: () => [] },
    /** 当前正在用的会话 id */
    activeId: { type: String, default: '' },
    /** 有一轮回答正在流式输出——此时切换/删除会串台，禁掉 */
    busy: Boolean,
    loading: Boolean,
  },
  emits: ['select', 'create', 'remove'],
  computed: {
    items() {
      const list = this.sessions || []
      const active = this.activeId
      if (!active || list.some((s) => s.session_id === active)) return list
      // 刚新建的会话在库里还**没有行**（Agent 落第一条消息时才 ensure_session），
      // 不补一条的话，用户点完「新对话」在列表里找不到自己刚开的这条。
      return [
        { session_id: active, title: '', message_count: 0, updated_at: '', draft: true },
        ...list,
      ]
    },
  },
  methods: {
    select(session) {
      if (session.session_id === this.activeId) return
      this.$emit('select', session.session_id)
    },

    meta(session) {
      if (session.draft) return '刚创建'
      const count = Number(session.message_count) || 0
      const time = this.formatTime(session.updated_at)
      return time ? `${count} 条 · ${time}` : `${count} 条`
    },

    formatTime(raw) {
      if (!raw) return ''
      const d = new Date(raw)
      if (Number.isNaN(d.getTime())) return ''
      const now = new Date()
      if (d.toDateString() === now.toDateString()) {
        const hh = String(d.getHours()).padStart(2, '0')
        const mm = String(d.getMinutes()).padStart(2, '0')
        return `${hh}:${mm}`
      }
      const yesterday = new Date(now)
      yesterday.setDate(now.getDate() - 1)
      if (d.toDateString() === yesterday.toDateString()) return '昨天'
      const md = `${d.getMonth() + 1}/${d.getDate()}`
      return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}/${md}`
    },
  },
}
</script>

<style scoped>
.session-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  height: 100%;
}
.new-btn {
  padding: 9px 12px;
  border: 1px dashed rgba(102, 126, 234, 0.55);
  border-radius: 8px;
  background: rgba(102, 126, 234, 0.12);
  color: #cfd6ff;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.new-btn:hover:not(:disabled) {
  background: rgba(102, 126, 234, 0.26);
  border-color: rgba(102, 126, 234, 0.9);
}
.new-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.hint {
  padding: 12px 4px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.4);
}
.session-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: 8px;
  border-left: 2px solid transparent;
  cursor: pointer;
  transition: background 0.15s;
}
.session-item:hover { background: rgba(255, 255, 255, 0.06); }
.session-item.active {
  background: rgba(102, 126, 234, 0.16);
  border-left-color: #667eea;
}
.item-main { flex: 1; min-width: 0; }
.item-title {
  font-size: 13px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-item.active .item-title { color: #cfd6ff; }
.item-meta {
  margin-top: 2px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.38);
}
/* 删除按钮平时隐藏：列表项都很窄，常驻的 ✕ 会跟标题抢注意力 */
.item-del {
  flex: 0 0 auto;
  padding: 0 4px;
  border: none;
  background: none;
  color: rgba(255, 255, 255, 0.35);
  font-size: 12px;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, color 0.15s;
}
.session-item:hover .item-del { opacity: 1; }
.item-del:hover:not(:disabled) { color: #ff8080; }
.item-del:disabled { cursor: not-allowed; }
</style>
