<template>
  <div class="flow-panel">
    <div v-if="turns.length === 0" class="empty">等待输入...</div>
    <div v-for="turn in turns" :key="turn.trace_id" class="turn">
      <div class="turn-header">
        <span class="turn-no">#{{ turn.index }}</span>
        <span class="trace-id" :title="turn.trace_id">
          🔗 {{ turn.trace_id ? turn.trace_id.slice(0, 12) : 'local' }}
        </span>
        <span v-if="turn.duration_ms" class="duration">⏱ {{ turn.duration_ms }}ms</span>
        <span class="step-count">{{ turn.steps.length }} 步</span>
      </div>
      <div class="turn-steps">
        <div
          v-for="(step, idx) in turn.steps"
          :key="idx"
          class="step"
          :class="[step.event, step.status, { expanded: isExpanded(turn.trace_id, idx) }]"
          @click="toggle(turn.trace_id, idx, step)"
        >
          <span class="step-icon">{{ iconFor(step.event) }}</span>
          <span class="step-event">{{ step.event }}</span>
          <span class="step-text">{{ summaryOf(step) }}</span>
          <span v-if="hasDetail(step)" class="expand">{{ isExpanded(turn.trace_id, idx) ? '▼' : '▶' }}</span>
        </div>
        <div v-if="expandedPayload" class="step-detail">
          <div class="detail-header">
            <span class="detail-title">{{ expandedPayload.event }}</span>
            <button class="copy-btn" @click="copy(expandedPayload.payload)">📋 复制</button>
            <button class="close-btn" @click="expandedPayload = null">✕</button>
          </div>
          <pre>{{ formatJson(expandedPayload.payload) }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'FlowPanel',
  props: {
    turns: { type: Array, default: () => [] },
  },
  data() {
    return { expandedPayload: null }
  },
  methods: {
    iconFor(event) {
      const icons = {
        thinking: '🔄',
        plan: '📋',
        tool_call: '🔧',
        tool_result: '✓',
        tool_error: '✗',
        message_delta: '💬',
        message_final: '📝',
        error: '❌',
        skill_learned: '✨',
      }
      return icons[event] || '•'
    },
    summaryOf(step) {
      const event = step.event
      const p = step.payload || {}
      switch (event) {
        case 'thinking':
          return `阶段: ${p.stage || ''}`
        case 'plan':
          return `意图: ${p.intent || ''} | 技能: ${p.skill || '无'} | ${(p.tasks||[]).length} 个任务`
        case 'tool_call': {
          const params = JSON.stringify(p.params || {})
          const short = params.length > 40 ? params.slice(0, 40) + '…' : params
          return `[${p.task_id || '-'}] ${p.tool} ${short}`
        }
        case 'tool_result':
          return `[${p.task_id || '-'}] ${p.tool} ✓`
        case 'tool_error':
          return `[${p.task_id || '-'}] ${p.tool} 失败: ${p.error || ''}`
        case 'message_delta':
          return `+${(p.delta || '').length} 字符`
        case 'message_final':
          return `完成 ${(p.content || '').length} 字符`
        case 'error':
          return p.message || ''
        case 'skill_learned':
          return `${p.name} v${p.version} | 步骤 ${p.step_count} | ${(p.patterns||[]).join(', ')}`
        default:
          return ''
      }
    },
    hasDetail(step) {
      if (!step) return false
      // 这些事件内容太碎,不展开
      const noDetail = new Set(['thinking', 'message_delta'])
      return !noDetail.has(step.event)
    },
    expandedKey(traceId, idx) { return `${traceId}#${idx}` },
    isExpanded(traceId, idx) {
      return this.expandedPayload && this.expandedPayload._key === this.expandedKey(traceId, idx)
    },
    toggle(traceId, idx, step) {
      if (!this.hasDetail(step)) return
      const key = this.expandedKey(traceId, idx)
      if (this.expandedPayload && this.expandedPayload._key === key) {
        this.expandedPayload = null
        return
      }
      this.expandedPayload = {
        _key: key,
        event: step.event,
        payload: step.payload || {},
      }
    },
    formatJson(obj) {
      try { return JSON.stringify(obj, null, 2) } catch { return String(obj) }
    },
    async copy(text) {
      try {
        await navigator.clipboard.writeText(this.formatJson(text))
      } catch {}
    },
  },
}
</script>

<style scoped>
.flow-panel {
  padding: 8px;
  font-size: 13px;
}
.empty { color: #999; padding: 16px; text-align: center; }

.turn {
  border-left: 2px solid #667eea;
  padding-left: 10px;
  margin-bottom: 14px;
}
.turn-header {
  display: flex;
  gap: 8px;
  font-size: 11px;
  color: #888;
  margin-bottom: 4px;
  align-items: center;
}
.turn-no { font-weight: bold; color: #667eea; }
.trace-id { font-family: monospace; }
.duration { color: #6a4c93; }
.step-count {
  margin-left: auto;
  font-size: 10px;
  background: rgba(255,255,255,0.05);
  padding: 1px 6px;
  border-radius: 8px;
}

.step {
  display: flex;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.15s;
  align-items: center;
}
.step:hover { background: rgba(102, 126, 234, 0.08); }
.step.expanded { background: rgba(102, 126, 234, 0.15); }
.step.tool_result, .step.message_final { color: #2e7d32; }
.step.tool_error, .step.error { color: #c62828; }
.step.skill_learned { color: #ef6c00; }

.step-icon { width: 18px; flex-shrink: 0; }
.step-event {
  font-weight: 500;
  min-width: 110px;
  color: #555;
  flex-shrink: 0;
}
.step-text { color: #333; flex: 1; word-break: break-all; font-size: 12px; }
.expand { color: #999; }

.step-detail {
  margin: 4px 0 8px 24px;
  padding: 8px;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 4px;
  font-family: monospace;
  font-size: 11px;
  max-height: 300px;
  overflow: auto;
}
.detail-header {
  display: flex;
  gap: 8px;
  margin-bottom: 6px;
  padding-bottom: 4px;
  border-bottom: 1px dashed rgba(0,0,0,0.1);
}
.detail-title {
  font-weight: bold;
  color: #667eea;
  font-family: inherit;
}
.copy-btn, .close-btn {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  color: inherit;
  padding: 2px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 10px;
  font-family: inherit;
}
.close-btn { margin-left: auto; }
.step-detail pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>