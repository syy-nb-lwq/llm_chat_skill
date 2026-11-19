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
          <button :class="{ active: activeTab === 'memory' }" @click="activeTab = 'memory'">🧠 记忆</button>
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
        <div v-show="activeTab === 'memory'" class="sidebar-content">
          <MemoryPanel />
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
        @feedback="sendFeedback"
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
import MemoryPanel from './components/MemoryPanel.vue'

export default {
  name: 'App',
  components: { ToolList, SkillList, SkillManager, FlowPanel, ChatPanel, PatchReview, EvolutionDashboard, MemoryPanel },
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

    // 恢复本会话的历史消息（刷新页面后不丢对话）
    //
    // 会话内容是全量保留在后端的（docs/13：短期记忆 = 会话内容，不按 TTL 清理），
    // 所以这里只是“把已有的读回来”，不是重新生成。
    async loadHistory() {
      const session = wsService.session
      if (!session || !session.session_id) return
      try {
        const sid = encodeURIComponent(session.session_id)
        const res = await fetch(`${API_BASE}/api/session/messages?session_id=${sid}`)
        if (!res.ok) {
          // 404/400 都只是“还没历史”，不打扰用户
          return
        }
        const data = await res.json()
        const restored = (data.messages || [])
          .filter((m) => m.role === 'user' || m.role === 'assistant')
          .map((m) => ({ role: m.role, content: m.content || '' }))
        // 只有当当前没有正在进行的对话时才覆盖 ——
        // 否则可能在“刚发出一条消息”和“历史返回”之间打架
        if (restored.length > 0 && this.messages.length === 0) {
          this.messages = restored
          this.pendingIndex = -1
        }
      } catch (e) {
        console.warn('[History] 恢复失败:', e)
      }
    },

    initWebSocket() {
      wsService.on('connected', () => {
        this.isConnected = true
        // 身份签发后立刻恢复历史 —— 否则一刷新页面，之前的对话就“消失”了。
        // 会话内容本来就是全量保留在 memory/sessions.db 里的，
        // 只是前端从来没去取过。
        this.loadHistory()
      })
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

      // 意图不明时的澄清请求(带候选项)
      wsService.on('clarification_request', (payload) => this.handleClarification(payload))

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
        // 带上 execution_id —— 前端靠它把 👍/👎 关联到具体那条回答
        this.messages[this.pendingIndex].execution_id = payload.execution_id || ''
        // 产物(文件/视频)—— 以前只在后端工作区里,用户看不到
        this.messages[this.pendingIndex].artifacts = payload.artifacts || []
      }
      this.pendingIndex = -1
      this.endTurn()
    },

    // 回答反馈(👍/👎):自演化最可靠的质量信号
    sendFeedback({ execution_id, accepted, comment }) {
      if (!execution_id) return
      wsService.feedback(execution_id, accepted, comment || '')
      // 本地回显:标上用户的选择
      const msg = this.messages.find(m => m.execution_id === execution_id)
      if (msg) {
        msg.feedback = accepted
        // Vue3 响应式:直接赋值即可(数组项是响应式的)
      }
    },

    handleError(payload) {
      this.messages.push({ role: 'system', content: '错误: ' + (payload.message || '') })
      this.pendingIndex = -1
      this.endTurn()
    },

    // 澄清请求:把候选项挂到当前助手气泡上(前端渲染成可点选的按钮)
    handleClarification(payload) {
      const options = (payload && payload.options) || []
      if (this.pendingIndex >= 0 && this.messages[this.pendingIndex]?.role === 'assistant') {
        this.messages[this.pendingIndex].clarification = {
          question: payload.question || '',
          options,
        }
      } else {
        this.messages.push({
          role: 'assistant',
          content: (payload && payload.question) || '',
          clarification: { question: (payload && payload.question) || '', options },
        })
      }
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
