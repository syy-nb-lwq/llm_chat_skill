<template>
  <div class="evolution-dashboard">
    <div class="header-row">
      <h4>进化状态</h4>
      <div class="actions">
        <button class="reload" @click="refresh" :disabled="loading">刷新</button>
        <button class="reflect-btn" @click="requestReflection" :disabled="loading || !selfEvolutionEnabled">
          立即复盘
        </button>
      </div>
    </div>

    <div class="feature-toggle">
      <label>
        <input type="checkbox" v-model="selfEvolutionEnabled" @change="toggleFeature" />
        <span>启用自我进化</span>
      </label>
      <p class="hint">开启后系统会记录失败经验并生成改进建议。</p>
    </div>

    <div v-if="error" class="error">{{ error }}</div>

    <div class="stats-grid">
      <div class="stat-card failures">
        <div class="stat-value">{{ stats?.total_failures || 0 }}</div>
        <div class="stat-label">失败记录</div>
      </div>
      <div class="stat-card successes">
        <div class="stat-value">{{ stats?.total_successes || 0 }}</div>
        <div class="stat-label">成功记录</div>
      </div>
      <div class="stat-card patches">
        <div class="stat-value">{{ stats?.pending_patches || 0 }}</div>
        <div class="stat-label">待审建议</div>
      </div>
      <div class="stat-card reflections">
        <div class="stat-value">{{ reflectionReports.length }}</div>
        <div class="stat-label">复盘报告</div>
      </div>
    </div>

    <div v-if="reflectionReports.length > 0" class="section">
      <h5>近期复盘报告</h5>
      <div v-for="report in reflectionReports" :key="report.id" class="report-card">
        <div class="report-header">
          <span class="report-id">{{ report.id }}</span>
          <span class="report-trigger">{{ formatTrigger(report.trigger_reason) }}</span>
          <span class="report-time">{{ formatTime(report.timestamp) }}</span>
        </div>

        <div v-if="report.high_freq_failures?.length" class="report-section">
          <div class="section-title">高频失败场景</div>
          <div v-for="item in report.high_freq_failures" :key="item.scenario" class="failure-item">
            <span class="scenario">{{ item.scenario }}</span>
            <span class="count">{{ item.count }} 次</span>
            <span class="diagnosis">{{ item.common_diagnosis }}</span>
          </div>
        </div>

        <div v-if="report.skill_suggestions?.length" class="report-section">
          <div class="section-title">技能优化建议</div>
          <div v-for="(item, idx) in report.skill_suggestions" :key="idx" class="suggestion-item">
            <span class="target">{{ item.target_skill }}</span>
            <span class="recommendation">{{ item.recommendation }}</span>
          </div>
        </div>
      </div>
    </div>

    <div v-else class="empty">暂无复盘报告</div>

    <div class="quick-actions">
      <h5>快速操作</h5>
      <div class="action-buttons">
        <button @click="exportData" class="action-btn">导出数据</button>
      </div>
    </div>
  </div>
</template>

<script>
const API_BASE = 'http://localhost:8000'

export default {
  name: 'EvolutionDashboard',
  data() {
    return {
      selfEvolutionEnabled: false,
      stats: null,
      reflectionReports: [],
      loading: false,
      error: '',
    }
  },
  mounted() {
    this.refresh()
  },
  methods: {
    async refresh() {
      this.loading = true
      this.error = ''
      try {
        const [featureRes, statsRes] = await Promise.all([
          fetch(`${API_BASE}/api/features`),
          fetch(`${API_BASE}/api/memory/stats`),
        ])

        if (!featureRes.ok) throw new Error(await featureRes.text())
        if (!statsRes.ok) throw new Error(await statsRes.text())

        const featureData = await featureRes.json()
        this.selfEvolutionEnabled = !!featureData.self_evolution_enabled
        this.stats = await statsRes.json()

        await this.loadReflectionReports()
      } catch (e) {
        this.error = `刷新失败: ${e.message}`
      } finally {
        this.loading = false
      }
    },

    async loadReflectionReports() {
      if (!this.selfEvolutionEnabled) {
        this.reflectionReports = []
        return
      }

      const res = await fetch(`${API_BASE}/api/reflections`)
      if (!res.ok) {
        if (res.status === 403) {
          this.reflectionReports = []
          return
        }
        throw new Error(await res.text())
      }

      const data = await res.json()
      this.reflectionReports = data.reflections || []
    },

    async toggleFeature() {
      this.loading = true
      this.error = ''
      try {
        const res = await fetch(`${API_BASE}/api/features/self-evolution`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: this.selfEvolutionEnabled, persist: true }),
        })
        if (!res.ok) throw new Error(await res.text())
        const data = await res.json()
        this.selfEvolutionEnabled = !!data.self_evolution_enabled
        await this.loadReflectionReports()
      } catch (e) {
        this.error = `更新开关失败: ${e.message}`
        this.selfEvolutionEnabled = !this.selfEvolutionEnabled
      } finally {
        this.loading = false
      }
    },

    async requestReflection() {
      this.loading = true
      this.error = ''
      try {
        const res = await fetch(`${API_BASE}/api/reflections/request`, { method: 'POST' })
        if (!res.ok) throw new Error(await res.text())
        await this.refresh()
      } catch (e) {
        this.error = `请求复盘失败: ${e.message}`
      } finally {
        this.loading = false
      }
    },

    async exportData() {
      try {
        const data = {
          stats: this.stats,
          reports: this.reflectionReports,
          exportTime: new Date().toISOString(),
        }
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `evolution-data-${new Date().toISOString().split('T')[0]}.json`
        a.click()
        URL.revokeObjectURL(url)
      } catch (e) {
        this.error = `导出失败: ${e.message}`
      }
    },

    formatTime(value) {
      if (!value) return ''
      try {
        return new Date(value).toLocaleString('zh-CN', {
          month: '2-digit',
          day: '2-digit',
          hour: '2-digit',
          minute: '2-digit',
        })
      } catch {
        return value
      }
    },

    formatTrigger(reason) {
      if (!reason) return ''
      if (reason.startsWith('same_scenario')) return '同场景重复失败'
      return {
        high_failure_count: '失败过多',
        user_request: '用户请求',
      }[reason] || reason
    },
  },
}
</script>

<style scoped>
.evolution-dashboard {
  padding: 8px;
}

.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.header-row h4 {
  margin: 0;
  font-size: 14px;
  color: #555;
}

.feature-toggle {
  background: rgba(102, 126, 234, 0.1);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}

.feature-toggle label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-weight: 500;
}

.hint {
  margin: 4px 0 0 26px;
  font-size: 11px;
  color: #888;
}

.error {
  margin-bottom: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(239, 83, 80, 0.12);
  color: #c62828;
  font-size: 12px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  margin-bottom: 16px;
}

.stat-card {
  background: rgba(255, 255, 255, 0.03);
  border-radius: 8px;
  padding: 12px;
  text-align: center;
  border: 1px solid rgba(255, 255, 255, 0.05);
}

.stat-value {
  font-size: 24px;
  font-weight: bold;
  color: #667eea;
}

.stat-label {
  font-size: 11px;
  color: #888;
}

.stat-card.failures .stat-value { color: #ef5350; }
.stat-card.successes .stat-value { color: #66bb6a; }
.stat-card.patches .stat-value { color: #ffa726; }
.stat-card.reflections .stat-value { color: #42a5f5; }

.section h5,
.quick-actions h5 {
  margin: 0 0 8px;
  font-size: 12px;
  color: #888;
  text-transform: uppercase;
}

.report-card {
  background: rgba(66, 165, 245, 0.05);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
  border-left: 3px solid #42a5f5;
}

.report-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 11px;
}

.report-id {
  font-family: monospace;
  color: #667eea;
}

.report-trigger {
  background: rgba(66, 165, 245, 0.2);
  color: #1976d2;
  padding: 2px 6px;
  border-radius: 4px;
}

.report-time {
  color: #888;
  margin-left: auto;
}

.section-title {
  font-size: 10px;
  color: #888;
  text-transform: uppercase;
  margin-bottom: 4px;
}

.failure-item,
.suggestion-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
}

.scenario,
.target {
  font-weight: 500;
  color: #ef5350;
}

.count {
  background: rgba(239, 83, 80, 0.2);
  color: #c62828;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 10px;
}

.diagnosis,
.recommendation {
  color: #666;
  font-size: 11px;
}

.empty {
  color: #999;
  padding: 12px 0;
  font-size: 12px;
}

.action-buttons {
  display: flex;
  gap: 8px;
}

.action-btn,
.reload,
.reflect-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: inherit;
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}

.reflect-btn {
  background: rgba(102, 126, 234, 0.2);
  border-color: rgba(102, 126, 234, 0.3);
  color: #667eea;
}

.actions {
  display: flex;
  gap: 4px;
}

.reload:disabled,
.reflect-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
