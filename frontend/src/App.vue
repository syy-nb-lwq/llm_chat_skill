<template>
  <div class="app-container">
    <header class="header">
      <h1>📚 Skill Agent</h1>
      <div class="header-info">
        <span class="client-id">{{ clientIdShort }}</span>
        <span class="status" :class="{ connected: isConnected }">
          {{ isConnected ? '已连接' : '未连接' }}
        </span>
      </div>
    </header>

    <main class="main-content">
      <aside class="sidebar">
        <div class="sidebar-tabs">
          <button :class="{ active: activeTab === 'tools' }" @click="activeTab = 'tools'">🔧 工具</button>
          <button :class="{ active: activeTab === 'skills' }" @click="activeTab = 'skills'">📘 技能</button>
          <button :class="{ active: activeTab === 'flow' }" @click="activeTab = 'flow'">🔀 流转</button>
          <button :class="{ active: activeTab === 'manager' }" @click="activeTab = 'manager'">📚 管理</button>
        </div>

        <div v-show="activeTab === 'tools'" class="sidebar-content">
          <ToolList :tools="tools" />
        </div>
        <div v-show="activeTab === 'skills'" class="sidebar-content">
          <SkillList :skills="skills" />
        </div>
        <div v-show="activeTab === 'flow'" class="sidebar-content flow-content">
          <FlowPanel :turns="turns" />
        </div>
        <div v-show="activeTab === 'manager'" class="sidebar-content">
          <SkillManager
            :skills="skills"
            :loading="loadingSkills"
            :recent-learned="recentLearned"
            @reload="loadSkills"
          />
        </div>
      </aside>

      <ChatPanel
        :connected="isConnected"
        :messages="messages"
        @send="onSend"
        @reset="onReset"
      />
    </main>
  </div>
</template>

<script>
import { wsService } from './websocket'
import ToolList from './components/ToolList.vue'
import SkillList from './components/SkillList.vue'
import FlowPanel from './components/FlowPanel.vue'
import ChatPanel from './components/ChatPanel.vue'
import SkillManager from './components/SkillManager.vue'

export default {
  name: 'App',
  components: { ToolList, SkillList, FlowPanel, ChatPanel, SkillManager },
  data() {
    return {
      isConnected: false,
      activeTab: 'tools',
      tools: [],
      skills: [],
      loadingSkills: false,
      messages: [],
      turns: [],
      currentTurn: null,
      pendingAssistantIndex: -1,
      recentLearned: null,
      toastTimer: null,
    }
  },
  computed: {
    clientIdShort() {
      return wsService.clientId ? wsService.clientId.slice(0, 8) + '...' : '未初始化'
    },
  },
  mounted() {
    this.initWebSocket()
    this.loadTools()
    this.loadSkills()
  },
  beforeUnmount() {
    wsService.disconnect()
    if (this.toastTimer) clearTimeout(this.toastTimer)
  },
  methods: {
    async loadTools() {
      try {
        const r = await fetch('http://localhost:8000/api/tools')
        const d = await r.json()
        this.tools = d.tools || []
      } catch {
        this.tools = [{ name: 'weather_query', description: '查询天气' }, { name: 'web_search', description: '网络搜索' }]
      }
    },
    async loadSkills() {
      this.loadingSkills = true
      try {
        const r = await fetch('http://localhost:8000/api/skills')
        const d = await r.json()
        this.skills = d.skills || []
      } catch (e) {
        console.error(e)
      } finally {
        this.loadingSkills = false
      }
    },

    startTurn(traceId) {
      this.currentTurn = {
        trace_id: traceId,
        index: this.turns.length + 1,
        steps: [],
        duration_ms: 0,
        start_ts: Date.now(),
      }
    },
    pushStep(event, payload, raw) {
      if (!this.currentTurn) this.startTurn('local-' + Date.now())
      // 从后端消息里取 trace_id(若还没有)
      if (!this.currentTurn.trace_id || this.currentTurn.trace_id.startsWith('local-')) {
        if (raw && raw.trace_id) this.currentTurn.trace_id = raw.trace_id
      }
      this.currentTurn.steps.push({ event, payload, ts: Date.now() })
    },
    endTurn() {
      if (this.currentTurn) {
        this.currentTurn.duration_ms = Date.now() - this.currentTurn.start_ts
        this.turns.unshift(this.currentTurn)
        if (this.turns.length > 10) this.turns.length = 10
        this.currentTurn = null
      }
    },

    initWebSocket() {
      wsService.on('connected', () => { this.isConnected = true })

      wsService.on('disconnected', () => { this.isConnected = false })

      wsService.on('thinking', (p) => {
        if (['teaching_detect', 'planning', 'tools_running', 'synthesizing'].includes(p.stage)) {
          if (!this.currentTurn) this.startTurn('local-' + Date.now())
        }
        this.pushStep('thinking', p)
      })

      wsService.on('plan', (p) => this.pushStep('plan', p))

      wsService.on('tool_call', (p) => this.pushStep('tool_call', p))
      wsService.on('tool_result', (p) => this.pushStep('tool_result', p))
      wsService.on('tool_error', (p) => this.pushStep('tool_error', p))

      // 教导闭环:高亮 + 切到管理标签
      wsService.on('skill_learned', (p) => {
        this.recentLearned = { name: p.name, version: p.version, capability: p.capability }
        this.pushStep('skill_learned', p)
        this.activeTab = 'manager'
        if (this.toastTimer) clearTimeout(this.toastTimer)
        this.toastTimer = setTimeout(() => { this.recentLearned = null }, 5000)
        this.loadSkills()
      })

      // 流式输出
      wsService.on('message_delta', (p) => {
        if (this.pendingAssistantIndex < 0 || this.messages[this.pendingAssistantIndex]?.role !== 'assistant') {
          this.messages.push({ role: 'assistant', content: '' })
          this.pendingAssistantIndex = this.messages.length - 1
        }
        this.messages[this.pendingAssistantIndex].content += p.delta || ''
      })

      wsService.on('message_final', (p) => {
        if (this.pendingAssistantIndex >= 0 && p.content) {
          this.messages[this.pendingAssistantIndex].content = p.content
        }
        this.pushStep('message_final', { content_length: (p.content || '').length })
        this.pendingAssistantIndex = -1
        this.endTurn()
        this.loadSkills()
      })

      wsService.on('error', (p) => {
        this.messages.push({ role: 'system', content: '错误: ' + (p.message || '') })
        this.endTurn()
      })

      wsService.on('log', () => {})

      wsService.on('reset_ack', () => {
        this.messages = []
        this.turns = []
      })

      wsService.connect().catch((e) => console.error('connect error', e))
    },

    onSend(text) {
      this.messages.push({ role: 'user', content: text })
      this.pendingAssistantIndex = -1
      // 用 'pending-' 占位,后端首个事件到达后会被真实 trace_id 替换
      this.startTurn('pending-' + Date.now())
      wsService.chat(text)
    },

    onReset() {
      wsService.reset()
    },
  },
}
</script>

<style>
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  color: #eaeaea;
  min-height: 100vh;
}
.app-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  max-width: 1400px;
  margin: 0 auto;
  padding: 16px;
  box-sizing: border-box;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 12px;
  margin-bottom: 16px;
}
.header h1 {
  margin: 0;
  font-size: 1.6rem;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}
.header-info { display: flex; gap: 12px; align-items: center; font-size: 12px; color: #999; }
.client-id { font-family: monospace; }
.status { padding: 4px 10px; border-radius: 12px; background: rgba(255, 0, 0, 0.2); }
.status.connected { background: rgba(0, 255, 0, 0.2); }

.main-content {
  display: flex;
  gap: 16px;
  flex: 1;
  min-height: 0;
}
.sidebar {
  width: 320px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.sidebar-tabs {
  display: flex;
  border-bottom: 1px solid rgba(255, 255, 255, 0.05);
}
.sidebar-tabs button {
  flex: 1;
  padding: 10px 4px;
  background: none;
  border: none;
  color: #999;
  cursor: pointer;
  font-size: 11px;
  border-bottom: 2px solid transparent;
}
.sidebar-tabs button.active {
  color: #667eea;
  border-bottom-color: #667eea;
}
.sidebar-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
}
.flow-content { padding: 0; }
</style>