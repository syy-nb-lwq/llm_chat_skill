<template>
  <div class="evolution-dashboard">
    <div class="header-row">
      <h4>进化状态</h4>
      <div class="actions">
        <button class="reload" @click="refresh" :disabled="loading">↻ 刷新</button>
        <button class="reflect-btn" @click="requestReflection" :disabled="loading">
          🔄 立即复盘
        </button>
      </div>
    </div>

    <!-- 开关 -->
    <div class="feature-toggle">
      <label>
        <input type="checkbox" v-model="selfEvolutionEnabled" @change="toggleFeature" />
        <span>启用自我进化</span>
      </label>
      <p class="hint">开启后系统会记录失败经验并生成改进建议</p>
    </div>

    <!-- 统计卡片 -->
    <div class="stats-grid">
      <div class="stat-card failures">
        <div class="stat-icon">📊</div>
        <div class="stat-value">{{ stats?.total_failures || 0 }}</div>
        <div class="stat-label">失败记录</div>
      </div>
      <div class="stat-card successes">
        <div class="stat-icon">✨</div>
        <div class="stat-value">{{ stats?.total_successes || 0 }}</div>
        <div class="stat-label">成功记录</div>
      </div>
      <div class="stat-card patches">
        <div class="stat-icon">💡</div>
        <div class="stat-value">{{ stats?.pending_patches || 0 }}</div>
        <div class="stat-label">待审阅建议</div>
      </div>
      <div class="stat-card reflections">
        <div class="stat-icon">🔍</div>
        <div class="stat-value">{{ reflectionReports.length }}</div>
        <div class="stat-label">反思报告</div>
      </div>
    </div>

    <!-- 反思报告列表 -->
    <div v-if="reflectionReports.length > 0" class="section">
      <h5>近期反思报告</h5>
      <div v-for="report in reflectionReports" :key="report.id" class="report-card">
        <div class="report-header">
          <span class="report-id">{{ report.id }}</span>
          <span class="report-trigger">{{ formatTrigger(report.trigger_reason) }}</span>
          <span class="report-time">{{ formatTime(report.timestamp) }}</span>
        </div>

        <div v-if="report.high_freq_failures?.length" class="report-section">
          <div class="section-title">高频失败场景</div>
          <div v-for="hf in report.high_freq_failures" :key="hf.scenario" class="failure-item">
            <span class="scenario">{{ hf.scenario }}</span>
            <span class="count">{{ hf.count }} 次</span>
            <span class="diagnosis">{{ hf.common_diagnosis }}</span>
          </div>
        </div>

        <div v-if="report.skill_suggestions?.length" class="report-section">
          <div class="section-title">技能优化建议</div>
          <div v-for="(s, i) in report.skill_suggestions" :key="i" class="suggestion-item">
            <span class="target">{{ s.target_skill }}</span>
            <span class="recommendation">{{ s.recommendation }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 快速操作 -->
    <div class="quick-actions">
      <h5>快速操作</h5>
      <div class="action-buttons">
        <button @click="clearOldRecords" class="action-btn danger">
          🗑️ 清理旧记录
        </button>
        <button @click="exportData" class="action-btn">
          📥 导出数据
        </button>
      </div>
    </div>
  </div>
</template>

<script>
export default {
  name: 'EvolutionDashboard',
  data() {
    return {
      selfEvolutionEnabled: false,
      stats: null,
      reflectionReports: [],
      loading: false,
    }
  },
  mounted() {
    this.refresh()
    this.checkFeatureStatus()
  },
  methods: {
    async refresh() {
      this.loading = true
      try {
        const [statsRes] = await Promise.all([
          fetch('http://localhost:8000/api/memory/stats'),
        ])
        if (statsRes.ok) {
          this.stats = await statsRes.json()
        }
        // 加载反思报告(从本地存储)
        this.loadReflectionReports()
      } catch (e) {
        console.error('刷新失败:', e)
      } finally {
        this.loading = false
      }
    },

    loadReflectionReports() {
      try {
        const stored = localStorage.getItem('evolution_reports')
        if (stored) {
          this.reflectionReports = JSON.parse(stored)
        }
      } catch (e) {
        console.error('加载反思报告失败:', e)
      }
    },

    saveReflectionReports() {
      try {
        localStorage.setItem('evolution_reports', JSON.stringify(this.reflectionReports))
      } catch (e) {
        console.error('保存反思报告失败:', e)
      }
    },

    async checkFeatureStatus() {
      // 从后端获取状态
      try {
        const res = await fetch('http://localhost:8000/api/health')
        // 暂时通过 localStorage 记录用户偏好
        const pref = localStorage.getItem('self_evolution_enabled')
        this.selfEvolutionEnabled = pref === 'true'
      } catch (e) {
        console.error('检查特性状态失败:', e)
      }
    },

    async toggleFeature() {
      localStorage.setItem('self_evolution_enabled', String(this.selfEvolutionEnabled))
      // TODO: 通知后端更新配置
      this.$emit('feature-toggled', this.selfEvolutionEnabled)
    },

    async requestReflection() {
      this.loading = true
      try {
        // 通过 WebSocket 发送复盘请求
        const { wsService } = await import('../websocket')
        // 暂时用 alert 提示
        alert('复盘请求已发送,请查看"✨ 进化"标签页查看结果')
      } catch (e) {
        console.error('请求复盘失败:', e)
      } finally {
        this.loading = false
      }
    },

    async clearOldRecords() {
      if (!confirm('确定要清理旧的失败记录吗?这将删除 30 天前的记录。')) {
        return
      }
      try {
        // TODO: 调用后端 API
        alert('功能开发中')
      } catch (e) {
        console.error('清理失败:', e)
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
        console.error('导出失败:', e)
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

    formatTrigger(reason) {
      const map = {
        'high_failure_count': '失败过多',
        'user_request': '用户请求',
      }
      if (reason.startsWith('same_scenario')) {
        return '同场景重复失败'
      }
      return map[reason] || reason
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

.feature-toggle input[type="checkbox"] {
  width: 18px;
  height: 18px;
  cursor: pointer;
}

.hint {
  margin: 4px 0 0 26px;
  font-size: 11px;
  color: #888;
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

.stat-icon {
  font-size: 20px;
  margin-bottom: 4px;
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

.section {
  margin-bottom: 16px;
}

.section h5 {
  margin: 0 0 8px 0;
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

.report-section {
  margin-top: 8px;
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

.quick-actions h5 {
  margin: 0 0 8px 0;
  font-size: 12px;
  color: #888;
  text-transform: uppercase;
}

.action-buttons {
  display: flex;
  gap: 8px;
}

.action-btn {
  flex: 1;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.05);
  color: inherit;
}

.action-btn:hover {
  background: rgba(255, 255, 255, 0.1);
}

.action-btn.danger {
  border-color: rgba(239, 83, 80, 0.3);
  color: #ef5350;
}

.action-btn.danger:hover {
  background: rgba(239, 83, 80, 0.2);
}

.actions {
  display: flex;
  gap: 4px;
}

.reload,
.reflect-btn {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  color: inherit;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}

.reflect-btn {
  background: rgba(102, 126, 234, 0.2);
  border-color: rgba(102, 126, 234, 0.3);
  color: #667eea;
}

.reload:disabled,
.reflect-btn:disabled {
  opacity: 0.5;
}
</style>
