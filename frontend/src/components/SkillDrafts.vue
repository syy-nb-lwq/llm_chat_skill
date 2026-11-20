<template>
  <div class="skill-drafts">
    <div class="header-row">
      <h4>待批准 <span v-if="drafts.length" class="count">{{ drafts.length }}</span></h4>
      <div class="head-actions">
        <button
          v-if="riskyCount"
          class="purge"
          :disabled="loading || purging"
          @click="purgeRisky"
        >清理建议改写的 {{ riskyCount }}</button>
        <button class="reload" @click="load" :disabled="loading">↻ 刷新</button>
      </div>
    </div>

    <p class="hint">
      agent 做成一件事后会自己提炼方法论，但批准前不生效 ——
      注水的技能会污染匹配、白占提示词预算。
    </p>

    <div v-if="loading" class="empty">加载中...</div>
    <div v-else-if="drafts.length === 0" class="empty">暂无待批准草稿</div>

    <div v-for="d in drafts" :key="d.id" class="draft-card" :class="{ risky: hasIssues(d) }">
      <div class="draft-head" @click="toggle(d.id)">
        <span class="name">{{ d.name || '(未命名)' }}</span>
        <span v-if="hasIssues(d)" class="badge bad">建议改写</span>
        <span v-else-if="warnCount(d)" class="badge warn">{{ warnCount(d) }} 处提醒</span>
        <span v-else class="badge ok">可批准</span>
        <span class="caret">{{ expanded[d.id] ? '▼' : '▶' }}</span>
      </div>

      <p v-if="d.capability" class="capability">{{ d.capability }}</p>
      <div class="origin" v-if="d.origin">来自：{{ d.origin }}</div>

      <div v-if="expanded[d.id]" class="detail">
        <div class="section-title">方法论</div>
        <pre class="method">{{ d.method }}</pre>

        <template v-if="qualityOf(d).issues && qualityOf(d).issues.length">
          <div class="note bad-note">⛔ {{ qualityOf(d).issues.join('；') }}</div>
        </template>
        <div v-for="w in (qualityOf(d).warnings || [])" :key="w" class="note warn-note">⚠ {{ w }}</div>

        <div v-if="(d.patterns || []).length" class="section-title">触发词</div>
        <div class="patterns">
          <span v-for="p in d.patterns" :key="p" class="pattern">{{ p }}</span>
        </div>

        <div v-if="(d.steps || []).length" class="section-title">步骤（自动记录）</div>
        <div class="steps">
          <span v-for="s in d.steps" :key="s.tool + s.name" class="step">🔧 {{ s.tool }}</span>
        </div>

        <div class="actions">
          <button class="approve" :disabled="busy === d.id" @click="approve(d)">
            {{ hasIssues(d) ? '仍要批准' : '批准启用' }}
          </button>
          <button class="discard" :disabled="busy === d.id" @click="discard(d)">丢弃</button>
        </div>
      </div>
    </div>

    <div v-if="toast" class="toast">{{ toast }}</div>
  </div>
</template>

<script>
import { skillDraftsApi } from '../api/client'

export default {
  name: 'SkillDrafts',
  emits: ['approved'],
  data() {
    return {
      drafts: [],
      loading: false,
      busy: '',
      purging: false,
      expanded: {},
      toast: '',
    }
  },
  computed: {
    riskyCount() {
      return this.drafts.filter((d) => this.hasIssues(d)).length
    },
  },
  mounted() {
    this.load()
  },
  methods: {
    qualityOf(d) {
      return d.quality || {}
    },
    hasIssues(d) {
      const q = this.qualityOf(d)
      return !!(q.issues && q.issues.length)
    },
    warnCount(d) {
      const q = this.qualityOf(d)
      return (q.warnings || []).length
    },
    toggle(id) {
      this.expanded[id] = !this.expanded[id]
    },
    async load() {
      this.loading = true
      try {
        const data = await skillDraftsApi.list()
        this.drafts = data.drafts || []
      } catch (e) {
        this.flash('读取草稿失败: ' + e.message)
      } finally {
        this.loading = false
      }
    },
    async approve(d) {
      const warn = this.hasIssues(d)
        ? `这条正文点名了工具，更像执行日志而不是方法论。\n批准后它会参与技能匹配，确定继续？`
        : `批准「${d.name}」并启用？`
      if (!confirm(warn)) return
      this.busy = d.id
      try {
        await skillDraftsApi.approve(d.id)
        this.flash(`已启用「${d.name}」`)
        this.$emit('approved')
        await this.load()
      } catch (e) {
        this.flash('批准失败: ' + e.message)
      } finally {
        this.busy = ''
      }
    },
    async discard(d) {
      if (!confirm(`丢弃「${d.name}」？该方法论不会再被沉淀。`)) return
      this.busy = d.id
      try {
        await skillDraftsApi.discard(d.id)
        this.flash('已丢弃')
        await this.load()
      } catch (e) {
        this.flash('丢弃失败: ' + e.message)
      } finally {
        this.busy = ''
      }
    },
    async purgeRisky() {
      const targets = this.drafts.filter((d) => this.hasIssues(d))
      if (!targets.length) return
      if (!confirm(
        `丢弃这 ${targets.length} 条「正文在点名工具」的草稿？\n\n` +
        `它们的正文是执行顺序的复述，批准进库只会污染技能匹配。`
      )) return
      this.purging = true
      let ok = 0
      for (const d of targets) {
        try {
          await skillDraftsApi.discard(d.id)
          ok += 1
        } catch (e) {
          this.flash(`丢弃「${d.name}」失败: ${e.message}`)
        }
      }
      this.purging = false
      this.flash(`已丢弃 ${ok} 条`)
      await this.load()
    },
    flash(msg) {
      this.toast = msg
      clearTimeout(this._timer)
      this._timer = setTimeout(() => { this.toast = '' }, 2600)
    },
  },
}
</script>

<style scoped>
.skill-drafts { padding: 8px; }
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.header-row h4 { margin: 0; font-size: 14px; color: #dfe4f2; }
.count {
  font-size: 10px;
  padding: 1px 6px;
  margin-left: 4px;
  border-radius: 8px;
  background: rgba(255,183,77,0.22);
  color: #ffb74d;
}
.head-actions { display: flex; gap: 6px; align-items: center; }
.purge {
  background: rgba(239,83,80,0.16);
  border: 1px solid rgba(239,83,80,0.32);
  color: #ef9a9a;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 11px;
}
.purge:hover { background: rgba(239,83,80,0.3); }
.purge:disabled { opacity: 0.5; cursor: default; }
.reload {
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.14);
  color: #dfe4f2;
  padding: 4px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.reload:disabled { opacity: 0.5; }
.hint {
  margin: 0 0 10px;
  font-size: 11px;
  line-height: 1.5;
  color: #8d97b5;
}
.empty { color: #8d97b5; padding: 14px; text-align: center; font-size: 13px; }

.draft-card {
  background: rgba(255,255,255,0.05);
  border-left: 3px solid #ffb74d;
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 8px;
}
.draft-card.risky { border-left-color: #ef5350; }
.draft-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
}
.name { font-weight: 600; font-size: 13px; color: #eef1f8; }
.badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
}
.badge.ok { background: rgba(102,187,106,0.2); color: #81c784; }
.badge.warn { background: rgba(255,183,77,0.2); color: #ffb74d; }
.badge.bad { background: rgba(239,83,80,0.22); color: #ef9a9a; }
.caret { margin-left: auto; color: #8d97b5; font-size: 10px; }

.capability { margin: 6px 0 4px; font-size: 12px; color: #c3cbe0; }
.origin {
  font-size: 11px;
  color: #7f89a8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.detail {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed rgba(255,255,255,0.12);
}
.section-title {
  font-size: 10px;
  color: #7f89a8;
  letter-spacing: 0.5px;
  margin: 8px 0 4px;
}
.method {
  margin: 0;
  padding: 8px;
  background: rgba(0,0,0,0.28);
  border-radius: 6px;
  font-size: 11.5px;
  line-height: 1.65;
  color: #d6dcf0;
  white-space: pre-wrap;
  font-family: inherit;
}
.note {
  margin-top: 6px;
  padding: 5px 8px;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.5;
}
.bad-note { background: rgba(239,83,80,0.14); color: #ef9a9a; }
.warn-note { background: rgba(255,183,77,0.12); color: #ffcc80; }

.patterns { display: flex; flex-wrap: wrap; gap: 4px; }
.pattern {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(118,75,162,0.28);
  border-radius: 8px;
  color: #c9b3e6;
}
.steps { display: flex; flex-wrap: wrap; gap: 4px; }
.step {
  font-size: 10px;
  padding: 2px 6px;
  background: rgba(33,150,243,0.18);
  border-radius: 8px;
  color: #90caf9;
}

.actions { display: flex; gap: 8px; margin-top: 10px; }
.approve {
  background: rgba(102,187,106,0.2);
  color: #a5d6a7;
  border: 1px solid rgba(102,187,106,0.35);
  padding: 5px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.approve:hover { background: rgba(102,187,106,0.32); }
.discard {
  background: rgba(239,83,80,0.14);
  color: #ef9a9a;
  border: 1px solid rgba(239,83,80,0.3);
  padding: 5px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12px;
}
.discard:hover { background: rgba(239,83,80,0.28); }
.actions button:disabled { opacity: 0.5; cursor: default; }

.toast {
  position: fixed;
  bottom: 80px;
  left: 50%;
  transform: translateX(-50%);
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: #fff;
  padding: 9px 18px;
  border-radius: 18px;
  font-size: 12px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.4);
  z-index: 100;
}
</style>
