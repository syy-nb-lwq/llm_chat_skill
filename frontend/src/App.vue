<template>
  <div class="app-container">
    <header class="header">
      <h1>📚 Skill Agent</h1>
      <div class="header-info">
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
          <button :class="{ active: activeTab === 'patches' }" @click="activeTab = 'patches'">✨ 进化</button>
          <button :class="{ active: activeTab === 'dashboard' }" @click="activeTab = 'dashboard'">📈 仪表盘</button>
        </div>

        <div v-show="activeTab === 'tools'" class="sidebar-content">
          <ToolList :tools="tools" />
        </div>
        <div v-show="activeTab === 'skills'" class="sidebar-content">
          <SkillManager
            :skills="skills"
            :loading="loading"
            :recent-learned="recentLearned"
            @reload="loadData"
          />
        </div>
        <div v-show="activeTab === 'flow'" class="sidebar-content">
          <FlowPanel :turns="turns" />
        </div>
        <div v-show="activeTab === 'patches'" class="sidebar-content">
          <PatchReview />
        </div>
        <div v-show="activeTab === 'dashboard'" class="sidebar-content">
          <EvolutionDashboard />
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
import { API_BASE } from './config'  // M0-04
import ToolList from './components/ToolList.vue'
import SkillList from './components/SkillList.vue'
import SkillManager from './components/SkillManager.vue'
import FlowPanel from './components/FlowPanel.vue'
import ChatPanel from './components/ChatPanel.vue'
import PatchReview from './components/PatchReview.vue'
import EvolutionDashboard from './components/EvolutionDashboard.vue'

export default {
  name: 'App',
  components: { ToolList, SkillList, SkillManager, FlowPanel, ChatPanel, PatchReview, EvolutionDashboard },
  data() {
    return {
      isConnected: false,
      activeTab: 'flow',  // 默认显示流转面板
      tools: [],
      skills: [],
      messages: [],
      turns: [],
      currentTurn: null,
      pendingIndex: -1,
      loading: false,
      recentLearned: null,
    }
  },
  mounted() {
    this.initWebSocket()
    this.loadData()
  },
  beforeUnmount() {
    wsService.off('connected', this.handleConnected)
    wsService.off('disconnected', this.handleDisconnected)
    wsService.off('skill_learned', this.handleSkillLearned)
  },
  methods: {
    async loadData() {
      this.loading = true
      try {
        const [toolsRes, skillsRes] = await Promise.all([
          fetch(`${API_BASE}/api/tools`),
          fetch(`${API_BASE}/api/skills`)
        ])
        const toolsData = await toolsRes.json()
        const skillsData = await skillsRes.json()
        this.tools = toolsData.tools || []
        this.skills = skillsData.skills || []
      } catch (e) {
        console.error('Load data failed:', e)
      } finally {
        this.loading = false
      }
    },

    initWebSocket() {
      wsService.on('connected', () => { this.isConnected = true })
      wsService.on('disconnected', () => { this.isConnected = false })

      // 流转事件
      wsService.on('thinking', (payload) => this.handleThinking(payload))
      wsService.on('plan', (payload) => this.handlePlan(payload))
      wsService.on('tool_call', (payload) => this.handleToolCall(payload))
      wsService.on('tool_result', (payload) => this.handleToolResult(payload))
      wsService.on('tool_error', (payload) => this.handleToolError(payload))

      // 消息事件
      wsService.on('message_delta', (payload) => this.handleMessageDelta(payload))
      wsService.on('message_final', (payload) => this.handleMessageFinal(payload))

      // 技能学习事件
      wsService.on('skill_learned', (payload) => this.handleSkillLearned(payload))

      // 错误
      wsService.on('error', (payload) => this.handleError(payload))

      wsService.connect()
    },

    // 开始新的对话轮次
    startTurn(traceId) {
      this.currentTurn = {
        trace_id: traceId,
        index: this.turns.length + 1,
        steps: [],
        start: Date.now(),
      }
    },

    // 结束当前轮次
    endTurn() {
      if (this.currentTurn) {
        this.currentTurn.duration_ms = Date.now() - this.currentTurn.start
        this.turns.unshift(this.currentTurn)
        if (this.turns.length > 10) this.turns.pop()
        this.currentTurn = null
      }
    },

    // 添加步骤到当前轮次
    addStep(event, payload) {
      if (!this.currentTurn) {
        this.startTurn('')
      }
      this.currentTurn.steps.push({ event, payload })
    },

    // ===== 事件处理 =====
    handleThinking(payload) {
      this.addStep('thinking', payload)
    },

    handlePlan(payload) {
      this.addStep('plan', payload)
      // 如果有 trace_id，更新当前轮次
      if (payload.trace_id && this.currentTurn) {
        this.currentTurn.trace_id = payload.trace_id
      }
    },

    handleToolCall(payload) {
      this.addStep('tool_call', payload)
    },

    handleToolResult(payload) {
      this.addStep('tool_result', payload)
    },

    handleToolError(payload) {
      this.addStep('tool_error', payload)
    },

    handleMessageDelta(payload) {
      // 消息增量，添加到聊天区域
      if (this.pendingIndex < 0 || this.messages[this.pendingIndex]?.role !== 'assistant') {
        this.messages.push({ role: 'assistant', content: '' })
        this.pendingIndex = this.messages.length - 1
      }
      this.messages[this.pendingIndex].content += payload.delta || ''
    },

    handleMessageFinal(payload) {
      if (this.pendingIndex >= 0 && payload.content) {
        this.messages[this.pendingIndex].content = payload.content
      }
      this.pendingIndex = -1
      this.endTurn()
    },

    handleError(payload) {
      this.messages.push({ role: 'system', content: '错误: ' + (payload.message || '') })
      this.pendingIndex = -1
      this.endTurn()
    },

    // 技能学习完成:展示 toast + 自动刷新技能列表 + 切到 skills 标签
    async handleSkillLearned(payload) {
      this.recentLearned = payload
      // 自动重载技能列表
      try { await this.loadData() } catch (e) { console.error(e) }
      // 切到技能标签,让用户看到新技能
      this.activeTab = 'skills'
      // 4 秒后清掉 toast
      setTimeout(() => { this.recentLearned = null }, 4000)
    },

    onSend(text) {
      this.messages.push({ role: 'user', content: text })
      this.pendingIndex = -1
      this.startTurn('')
      wsService.chat(text)
    },

    onReset() {
      this.messages = []
      this.turns = []
      this.currentTurn = null
      wsService.reset()
    },
  },
}
</script>

<style>
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
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
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  background: rgba(255,255,255,0.05);
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
.status {
  padding: 4px 10px;
  border-radius: 12px;
  background: rgba(255,0,0,0.2);
}
.status.connected { background: rgba(0,255,0,0.2); }
.main-content { display: flex; gap: 16px; flex: 1; }
.sidebar {
  width: 320px;
  background: rgba(255,255,255,0.03);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
}
.sidebar-tabs {
  display: flex;
  border-bottom: 1px solid rgba(255,255,255,0.05);
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
</style>
