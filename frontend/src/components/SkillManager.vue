<template>
  <div class="skill-manager">
    <div class="header-row">
      <h4>技能库</h4>
      <div class="actions">
        <button class="reload" @click="reload" :disabled="loading">↻ 刷新</button>
      </div>
    </div>
    <div v-if="loading" class="empty">加载中...</div>
    <div v-else-if="grouped.length === 0" class="empty">暂无技能</div>
    <div v-for="grp in grouped" :key="grp.name" class="skill-group">
      <div class="skill-card" :class="{ expanded: expanded[grp.name] }">
        <div class="skill-card-header" @click="toggleCard(grp.name)">
          <span class="skill-name">{{ grp.name }}</span>
          <span class="version-badge">{{ grp.versions.length }} 版本</span>
          <span class="skill-source" :class="grp.latest.source">{{ grp.latest.source }}</span>
          <span class="caret">{{ expanded[grp.name] ? '▼' : '▶' }}</span>
        </div>
        <p v-if="grp.latest.capability" class="capability">{{ grp.latest.capability }}</p>
        <div v-if="(grp.latest.patterns || []).length" class="patterns">
          <span v-for="p in grp.latest.patterns" :key="p" class="pattern">{{ p }}</span>
        </div>
        <div v-if="expanded[grp.name]" class="detail">
          <!-- 版本切换 -->
          <div class="version-list">
            <div class="section-title">版本</div>
            <div
              v-for="v in grp.versions"
              :key="v.version"
              class="version-row"
              :class="{ active: v.version === grp.latest.version }"
            >
              <span class="version-no">v{{ v.version }}</span>
              <span v-if="v.version === grp.latest.version" class="latest-tag">最新</span>
              <span class="version-time">{{ formatTime(v.updated_at || v.created_at) }}</span>
              <span class="version-source" :class="v.source">{{ v.source }}</span>
              <button class="del-btn" @click.stop="confirmDelete(grp.name, v.version, $event)" title="删除该版本">✕</button>
            </div>
            <button class="del-all" @click.stop="confirmDelete(grp.name, null, $event)">删除全部版本</button>
          </div>

          <!-- 方法论 -->
          <div v-if="grp.latest.method" class="section">
            <div class="section-title">方法论</div>
            <pre>{{ grp.latest.method }}</pre>
          </div>

          <!-- 步骤 -->
          <div v-if="(grp.latest.steps || []).length" class="section">
            <div class="section-title">步骤 ({{ grp.latest.steps.length }})</div>
            <div v-for="st in grp.latest.steps" :key="st.id" class="step-item">
              <span class="step-id">{{ st.id }}</span>
              <span class="step-tool" v-if="st.tool">🔧 {{ st.tool }}</span>
              <span class="step-name">{{ st.name || st.id }}</span>
              <span v-if="(st.depends_on||[]).length" class="step-deps">
                ← {{ st.depends_on.join(', ') }}
              </span>
            </div>
          </div>

          <!-- 元信息 -->
          <div class="meta">
            <span v-if="grp.latest.author">作者: {{ grp.latest.author }}</span>
            <span v-if="grp.latest.created_at">创建: {{ formatTime(grp.latest.created_at) }}</span>
          </div>
        </div>
      </div>
    </div>
    <div v-if="recentLearned" class="toast">
      ✨ 已学会新技能: <strong>{{ recentLearned.name }}</strong> v{{ recentLearned.version }}
    </div>
  </div>
</template>

<script>
export default {
  name: 'SkillManager',
  props: {
    skills: { type: Array, default: () => [] },
    loading: { type: Boolean, default: false },
    recentLearned: { type: Object, default: null },
  },
  emits: ['reload'],
  data() {
    return { expanded: {} }
  },
  computed: {
    grouped() {
      const map = new Map()
      for (const s of (this.skills || [])) {
        if (!map.has(s.name)) map.set(s.name, [])
        map.get(s.name).push(s)
      }
      const out = []
      for (const [name, list] of map) {
        list.sort((a, b) => (b.version || '').localeCompare(a.version || ''))
        out.push({ name, versions: list, latest: list[0] })
      }
      out.sort((a, b) => a.name.localeCompare(b.name))
      return out
    },
  },
  methods: {
    reload() { this.$emit('reload') },
    toggleCard(name) { this.expanded[name] = !this.expanded[name] },
    formatTime(t) {
      if (!t) return ''
      try {
        const d = new Date(t)
        return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      } catch { return t }
    },
    async confirmDelete(name, version, ev) {
      ev.stopPropagation()
      const label = version ? `${name}@${version}` : `所有 ${name} 版本`
      if (!confirm(`确定删除 ${label} ?`)) return
      try {
        const url = version
          ? `http://localhost:8000/api/skills/${encodeURIComponent(name)}/${encodeURIComponent(version)}`
          : `http://localhost:8000/api/skills/${encodeURIComponent(name)}`
        const r = await fetch(url, { method: 'DELETE' })
        if (!r.ok) throw new Error(await r.text())
        this.reload()
      } catch (e) {
        alert('删除失败: ' + e.message)
      }
    },
  },
}
</script>

<style scoped>
.skill-manager { padding: 8px; }
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.header-row h4 { margin: 0; font-size: 14px; color: #555; }
.actions { display: flex; gap: 4px; }
.reload {
  background: rgba(255,255,255,0.05);
  border: 1px solid rgba(255,255,255,0.1);
  color: inherit;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.reload:disabled { opacity: 0.5; }
.empty { color: #999; padding: 16px; text-align: center; font-size: 13px; }

.skill-card {
  background: rgba(102,126,234,0.05);
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
  border-left: 3px solid #667eea;
}
.skill-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}
.skill-name { font-weight: bold; font-size: 14px; }
.version-badge {
  font-size: 10px;
  padding: 1px 6px;
  background: rgba(102,126,234,0.15);
  border-radius: 8px;
  color: #667eea;
}
.skill-source {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
}
.skill-source.builtin { background: rgba(76,175,80,0.2); color: #2e7d32; }
.skill-source.taught { background: rgba(255,152,0,0.2); color: #ef6c00; }
.skill-source.imported { background: rgba(33,150,243,0.2); color: #1565c0; }
.caret { margin-left: auto; color: #667eea; }

.capability { margin: 6px 0 4px; font-size: 12px; color: #555; }
.patterns { display: flex; flex-wrap: wrap; gap: 4px; margin: 4px 0; }
.pattern {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(118,75,162,0.15);
  border-radius: 8px;
  color: #6a4c93;
}

.detail {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed rgba(255,255,255,0.1);
  font-size: 12px;
}
.section { margin: 8px 0; }
.section-title {
  font-size: 11px;
  color: #888;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 4px;
}
.section pre {
  margin: 0;
  padding: 6px;
  background: rgba(0,0,0,0.05);
  border-radius: 4px;
  font-size: 11px;
  white-space: pre-wrap;
  font-family: inherit;
}

.version-list { margin: 8px 0; }
.version-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: 4px;
  font-size: 12px;
}
.version-row.active { background: rgba(102,126,234,0.1); }
.version-no { font-weight: 500; font-family: monospace; }
.latest-tag {
  font-size: 9px;
  padding: 1px 5px;
  background: rgba(76,175,80,0.3);
  border-radius: 8px;
  color: #2e7d32;
}
.version-time { font-size: 10px; color: #888; margin-left: auto; }
.version-source {
  font-size: 9px;
  padding: 1px 5px;
  border-radius: 8px;
}
.version-source.builtin { background: rgba(76,175,80,0.2); color: #2e7d32; }
.version-source.taught { background: rgba(255,152,0,0.2); color: #ef6c00; }
.del-btn {
  background: rgba(244,67,54,0.15);
  color: #c62828;
  border: none;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  cursor: pointer;
  font-size: 11px;
}
.del-btn:hover { background: rgba(244,67,54,0.35); }
.del-all {
  margin-top: 6px;
  background: rgba(244,67,54,0.15);
  color: #c62828;
  border: 1px solid rgba(244,67,54,0.3);
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 11px;
}
.del-all:hover { background: rgba(244,67,54,0.3); }

.step-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: 12px;
}
.step-id { font-family: monospace; color: #667eea; }
.step-tool { font-size: 10px; padding: 1px 5px; background: rgba(33,150,243,0.2); border-radius: 8px; color: #1565c0; }
.step-name { color: #555; }
.step-deps { font-size: 10px; color: #888; margin-left: auto; }

.meta { font-size: 10px; color: #888; display: flex; gap: 12px; margin-top: 6px; }

.toast {
  position: fixed;
  bottom: 80px;
  left: 50%;
  transform: translateX(-50%);
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: white;
  padding: 10px 20px;
  border-radius: 20px;
  font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  animation: slideUp 0.3s ease;
  z-index: 100;
}
@keyframes slideUp {
  from { transform: translate(-50%, 20px); opacity: 0; }
  to   { transform: translate(-50%, 0); opacity: 1; }
}
</style>