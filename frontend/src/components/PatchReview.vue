<template>
  <div class="patch-review">
    <div class="header-row">
      <h4>改进建议</h4>
      <div class="actions">
        <button class="reload" @click="loadPatches" :disabled="loading">↻ 刷新</button>
      </div>
    </div>

    <!-- 统计信息 -->
    <div v-if="stats" class="stats-bar">
      <span class="stat">
        <span class="stat-num">{{ stats.total_failures }}</span>
        <span class="stat-label">失败记录</span>
      </span>
      <span class="stat">
        <span class="stat-num">{{ stats.total_successes }}</span>
        <span class="stat-label">成功记录</span>
      </span>
      <span class="stat highlight">
        <span class="stat-num">{{ stats.pending_patches }}</span>
        <span class="stat-label">待审阅</span>
      </span>
    </div>

    <div v-if="loading" class="empty">加载中...</div>
    <div v-else-if="patches.length === 0" class="empty">暂无待审阅的改进建议</div>

    <div v-for="patch in patches" :key="patch.id" class="patch-card">
      <div class="patch-header">
        <span class="patch-target">{{ patch.target_skill || '通用' }}</span>
        <span class="patch-type" :class="patch.patch_type">{{ patch.patch_type }}</span>
        <span class="patch-confidence" :class="confidenceClass(patch.confidence)">
          {{ Math.round(patch.confidence * 100) }}%
        </span>
        <!-- M3-05:风险等级 -->
        <span class="patch-risk" :class="riskClass(patch.risk_level)">
          风险:{{ riskLabel(patch.risk_level) }}
        </span>
        <span class="patch-time">{{ formatTime(patch.timestamp) }}</span>
      </div>

      <div class="patch-diagnosis">
        <div class="label">诊断</div>
        <p>{{ patch.diagnosis }}</p>
      </div>

      <!-- M3-05:自动发布被拦下的原因 -->
      <div v-if="patch.blocked_reason" class="patch-blocked">
        <div class="label">自动发布已拦截</div>
        <p>{{ patch.blocked_reason }}</p>
      </div>

      <!-- M3-05:回归样例校验结果 -->
      <div v-if="patch.validation" class="patch-validation">
        <div class="label">
          回归校验 {{ patch.validation.passed }}/{{ patch.validation.total }}
          <span v-if="patch.validation.has_regression === false" class="no-regression">无回归样例</span>
        </div>
        <ul v-if="patch.validation.cases && patch.validation.cases.length">
          <li v-for="c in patch.validation.cases" :key="c.name" :class="c.passed ? 'ok' : 'fail'">
            {{ c.passed ? '✓' : '✕' }} {{ c.name }}
            <span v-if="!c.passed" class="reason">{{ c.reason }}</span>
          </li>
        </ul>
        <ul v-if="patch.validation.static_issues && patch.validation.static_issues.length" class="issues">
          <li v-for="(issue, i) in patch.validation.static_issues" :key="i">⚠ {{ issue }}</li>
        </ul>
      </div>

      <div v-if="patch.suggestion" class="patch-suggestion">
        <div class="label">建议</div>
        <pre>{{ formatSuggestion(patch.suggestion) }}</pre>
      </div>

      <div class="patch-actions">
        <button class="approve-btn" @click="approve(patch.id)">✓ 批准</button>
        <button class="reject-btn" @click="reject(patch.id)">✕ 拒绝</button>
      </div>
    </div>
  </div>
</template>

<script>
import { memoryApi, patchesApi } from '../api/client'  // C-01

export default {
  name: 'PatchReview',
  data() {
    return {
      patches: [],
      stats: null,
      loading: false,
    }
  },
  mounted() {
    this.loadPatches()
    this.loadStats()
  },
  methods: {
    async loadPatches() {
      this.loading = true
      try {
        const data = await patchesApi.list()
        this.patches = data.patches || []
      } catch (e) {
        console.error('加载 patches 失败:', e)
      } finally {
        this.loading = false
      }
    },

    async loadStats() {
      try {
        this.stats = await memoryApi.stats()
      } catch (e) {
        console.error('加载统计失败:', e)
      }
    },

    async approve(patchId) {
      try {
        const data = await patchesApi.approve(patchId)
        this.loadPatches()
        this.loadStats()
        // 回归样例没过时给出提示(发布已执行,但风险偏高)
        const failed = (data.validation?.cases || []).filter(c => !c.passed)
        if (failed.length) {
          alert(
            '已发布,但有回归样例未通过:\n' +
            failed.map(c => `${c.name}: ${c.reason}`).join('\n')
          )
        }
      } catch (e) {
        // 后端会返回结构化的校验失败原因，优先展示 detail
        let msg = e.message
        if (e.body) {
          try {
            const parsed = JSON.parse(e.body)
            const detail = parsed.detail || {}
            msg = detail.message || (typeof detail === 'string' ? detail : e.message)
            if (detail.issues) msg += '\n' + detail.issues.join('\n')
          } catch (ignored) { /* 不是 JSON,用 e.message */ }
        }
        alert('批准失败: ' + msg)
      }
    },

    async reject(patchId) {
      try {
        await patchesApi.reject(patchId)
        this.loadPatches()
      } catch (e) {
        alert('拒绝失败: ' + e.message)
      }
    },

    formatTime(t) {
      if (!t) return ''
      try {
        const d = new Date(t)
        return d.toLocaleString('zh-CN', {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      } catch {
        return t
      }
    },

    formatSuggestion(suggestion) {
      if (typeof suggestion === 'string') return suggestion
      return JSON.stringify(suggestion, null, 2)
    },

    confidenceClass(confidence) {
      if (confidence >= 0.9) return 'high'
      if (confidence >= 0.7) return 'medium'
      return 'low'
    },

    // M3-05:风险等级
    riskLabel(risk) {
      if (risk === 'low') return '低'
      if (risk === 'medium') return '中'
      if (risk === 'high') return '高'
      return '未知'
    },

    riskClass(risk) {
      if (risk === 'low') return 'low'
      if (risk === 'medium') return 'medium'
      if (risk === 'high') return 'high'
      return 'unknown'
    },
  },
}
</script>

<style scoped>
.patch-review {
  padding: 8px;
}

.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.header-row h4 {
  margin: 0;
  font-size: 14px;
  color: #555;
}

.stats-bar {
  display: flex;
  gap: 12px;
  padding: 8px;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 8px;
  margin-bottom: 12px;
}

.stat {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.stat-num {
  font-size: 18px;
  font-weight: bold;
  color: #667eea;
}

.stat.highlight .stat-num {
  color: #f59e0b;
}

.stat-label {
  font-size: 10px;
  color: #888;
}

.empty {
  color: #999;
  padding: 16px;
  text-align: center;
  font-size: 13px;
}

.patch-card {
  background: rgba(245, 158, 11, 0.05);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
  border-left: 3px solid #f59e0b;
}

.patch-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.patch-target {
  font-weight: bold;
  font-size: 14px;
}

.patch-type {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 8px;
}

.patch-type.improve_skill {
  background: rgba(33, 150, 243, 0.2);
  color: #1565c0;
}

.patch-type.fix_method {
  background: rgba(156, 39, 176, 0.2);
  color: #7b1fa2;
}

.patch-type.new_skill {
  background: rgba(76, 175, 80, 0.2);
  color: #2e7d32;
}

.patch-confidence {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 8px;
  font-weight: 500;
}

.patch-confidence.high {
  background: rgba(76, 175, 80, 0.2);
  color: #2e7d32;
}

.patch-confidence.medium {
  background: rgba(245, 158, 11, 0.2);
  color: #b45309;
}

.patch-confidence.low {
  background: rgba(244, 67, 54, 0.2);
  color: #c62828;
}

.patch-time {
  font-size: 10px;
  color: #888;
  margin-left: auto;
}

/* M3-05:风险等级 */
.patch-risk {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 8px;
  font-weight: 500;
}

.patch-risk.low {
  background: rgba(76, 175, 80, 0.2);
  color: #2e7d32;
}

.patch-risk.medium {
  background: rgba(245, 158, 11, 0.2);
  color: #b45309;
}

.patch-risk.high {
  background: rgba(244, 67, 54, 0.2);
  color: #c62828;
}

.patch-risk.unknown {
  background: rgba(0, 0, 0, 0.08);
  color: #888;
}

/* M3-05:拦截原因与回归校验 */
.patch-blocked {
  margin-top: 6px;
  padding: 6px 8px;
  background: rgba(244, 67, 54, 0.08);
  border-radius: 6px;
}

.patch-blocked p {
  margin: 0;
  font-size: 11px;
  color: #c62828;
  line-height: 1.5;
}

.patch-validation {
  margin-top: 8px;
  padding: 6px 8px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: 6px;
}

.patch-validation ul {
  margin: 0;
  padding-left: 16px;
  list-style: none;
}

.patch-validation li {
  font-size: 11px;
  line-height: 1.6;
}

.patch-validation li.ok {
  color: #2e7d32;
}

.patch-validation li.fail {
  color: #c62828;
}

.patch-validation li.issues li,
.patch-validation ul.issues li {
  color: #b45309;
}

.patch-validation .reason {
  color: #888;
  margin-left: 4px;
}

.no-regression {
  margin-left: 6px;
  color: #c62828;
}

.label {
  font-size: 11px;
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}

.patch-diagnosis p {
  margin: 0;
  font-size: 12px;
  color: #555;
  line-height: 1.5;
}

.patch-suggestion {
  margin-top: 8px;
}

.patch-suggestion pre {
  margin: 0;
  padding: 6px;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 4px;
  font-size: 11px;
  white-space: pre-wrap;
  font-family: inherit;
  max-height: 120px;
  overflow-y: auto;
}

.patch-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  justify-content: flex-end;
}

.approve-btn,
.reject-btn {
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  border: none;
}

.approve-btn {
  background: rgba(76, 175, 80, 0.2);
  color: #2e7d32;
}

.approve-btn:hover {
  background: rgba(76, 175, 80, 0.35);
}

.reject-btn {
  background: rgba(244, 67, 54, 0.15);
  color: #c62828;
}

.reject-btn:hover {
  background: rgba(244, 67, 54, 0.3);
}

.reload {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: inherit;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}

.reload:disabled {
  opacity: 0.5;
}

.actions {
  display: flex;
  gap: 4px;
}
</style>
