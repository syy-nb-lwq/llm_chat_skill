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
          <button :class="{ active: activeTab === 'sessions' }" @click="activeTab = 'sessions'">💬 对话</button>
          <button :class="{ active: activeTab === 'tools' }" @click="activeTab = 'tools'">🔧 工具</button>
          <button :class="{ active: activeTab === 'skills' }" @click="activeTab = 'skills'">📘 技能</button>
          <button :class="{ active: activeTab === 'flow' }" @click="activeTab = 'flow'">🔀 流转</button>
          <button :class="{ active: activeTab === 'memory' }" @click="activeTab = 'memory'">🧠 记忆</button>
          <button :class="{ active: activeTab === 'patches' }" @click="activeTab = 'patches'">✨ 进化</button>
          <button :class="{ active: activeTab === 'dashboard' }" @click="activeTab = 'dashboard'">📈 仪表盘</button>
        </div>

        <div v-show="activeTab === 'sessions'" class="sidebar-content">
          <SessionList
            :sessions="sessions"
            :active-id="activeSessionId"
            :busy="busy"
            :loading="sessionsLoading"
            @select="selectSession"
            @create="createSession"
            @remove="removeSession"
          />
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
        :busy="busy"
        @send="onSend"
        @new-session="createSession"
        @feedback="sendFeedback"
      />
    </main>
  </div>
</template>

<script>
import { wsService } from './websocket'
import { EV } from './events'
import { sessionApi, skillsApi, toolsApi } from './api/client'
import ToolList from './components/ToolList.vue'
import SkillList from './components/SkillList.vue'
import SkillManager from './components/SkillManager.vue'
import FlowPanel from './components/FlowPanel.vue'
import ChatPanel from './components/ChatPanel.vue'
import PatchReview from './components/PatchReview.vue'
import EvolutionDashboard from './components/EvolutionDashboard.vue'
import MemoryPanel from './components/MemoryPanel.vue'
import SessionList from './components/SessionList.vue'

export default {
  name: 'App',
  components: { ToolList, SkillList, SkillManager, FlowPanel, ChatPanel, PatchReview, EvolutionDashboard, MemoryPanel, SessionList },
  data() {
    return {
      isConnected: false,
      activeTab: 'sessions',  // 默认显示会话列表（打开就想接着上次，或者开新的）
      tools: [],
      skills: [],
      messages: [],
      turns: [],
      currentTurn: null,
      pendingIndex: -1,
      loading: false,
      recentLearned: null,
      // 会话列表：库里有什么就显示什么（新→旧）
      sessions: [],
      sessionsLoading: false,
      // 当前会话 id。**必须放在 data 里**，不能算成 computed ——
      // wsService.session 是普通对象，改了不会触发 Vue 重新渲染
      activeSessionId: '',
    }
  },
  computed: {
    /** 有一轮回答正在流式输出（pendingIndex 指向那条空助手气泡） */
    busy() {
      return this.pendingIndex >= 0
    },
  },
  mounted() {
    this.initWebSocket()
    this.loadData()
  },
  beforeUnmount() {
    wsService.off(EV.CONNECTED, this.handleConnected)
    wsService.off(EV.DISCONNECTED, this.handleDisconnected)
    wsService.off(EV.SKILL_LEARNED, this.handleSkillLearned)
  },
  methods: {
    async loadData() {
      this.loading = true
      try {
        const [toolsData, skillsData] = await Promise.all([
          toolsApi.list(),
          skillsApi.list(),
        ])
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
        const data = await sessionApi.messages(session.session_id)
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
        // 404/400 都只是“还没历史”，不打扰用户
        if (e.status === 404 || e.status === 400) return
        console.warn('[History] 恢复失败:', e)
      }
    },

    initWebSocket() {
      wsService.on(EV.CONNECTED, (payload) => {
        this.isConnected = true
        // 身份签发后立刻恢复历史 —— 否则一刷新页面，之前的对话就“消失”了。
        // 会话内容本来就是全量保留在 memory/sessions.db 里的，
        // 只是前端从来没去取过。
        //
        // **切换会话也走这里**：`switchSession` 会让服务端重新签发身份并发
        // connected，所以 activeSessionId / 历史 / 列表三处一起刷新。
        this.activeSessionId = (payload && payload.session_id) || ''
        this.loadHistory()
        this.loadSessions()
      })
      wsService.on(EV.DISCONNECTED, () => { this.isConnected = false })

      // 流转事件
      wsService.on(EV.THINKING, (payload) => this.handleThinking(payload))
      wsService.on(EV.PLAN, (payload) => this.handlePlan(payload))
      wsService.on(EV.TOOL_CALL, (payload) => this.handleToolCall(payload))
      wsService.on(EV.TOOL_RESULT, (payload) => this.handleToolResult(payload))
      wsService.on(EV.TOOL_ERROR, (payload) => this.handleToolError(payload))

      // 消息事件
      wsService.on(EV.MESSAGE_DELTA, (payload) => this.handleMessageDelta(payload))
      wsService.on(EV.MESSAGE_FINAL, (payload) => this.handleMessageFinal(payload))

      // 技能学习事件
      wsService.on(EV.SKILL_LEARNED, (payload) => this.handleSkillLearned(payload))

      // 意图不明时的澄清请求(带候选项)
      wsService.on(EV.CLARIFICATION_REQUEST, (payload) => this.handleClarification(payload))

      // 错误
      wsService.on(EV.ERROR, (payload) => this.handleError(payload))

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

    // ===== 会话列表 =====

    /** 拉会话列表（新→旧）。每条带 title，标题由后端从首条用户消息生成 */
    async loadSessions() {
      this.sessionsLoading = true
      try {
        const data = await sessionApi.list()
        this.sessions = data.sessions || []
      } catch (e) {
        // 列表拉不到不影响聊天，只是侧栏空着
        console.warn('[Sessions] 列表加载失败:', e)
      } finally {
        this.sessionsLoading = false
      }
    },

    /** 清掉当前这屏对话（不动库里的数据） */
    clearConversation() {
      this.messages = []
      this.turns = []
      this.currentTurn = null
      this.pendingIndex = -1
    },

    /**
     * 切到另一条会话。
     *
     * 换会话 = 让服务端按新 session_id 重新签发身份（`wsService.switchSession`），
     * 回来后 connected 事件会把历史装上。所以这里只负责"先把这屏清干净"。
     *
     * `busy` 时直接拒绝：一轮回答跑到一半就换会话，旧那轮的 delta 会推给
     * 同一个连接（topic 是按连接走的），新会话界面里会冒出上一条回答的尾巴。
     */
    async selectSession(sessionId) {
      if (this.busy) return
      if (sessionId === this.activeSessionId) return
      this.clearConversation()
      // 乐观更新：不等服务端回包，列表高亮立刻跟着走
      this.activeSessionId = sessionId
      try {
        await wsService.switchSession(sessionId)
      } catch (e) {
        console.error('[Sessions] 切换失败:', e)
        this.messages.push({ role: 'system', content: '切换会话失败: ' + (e.message || e) })
      }
    },

    /** 开一条全新会话：不传 session_id，由服务端签发 */
    async createSession() {
      if (this.busy) return
      this.clearConversation()
      this.activeSessionId = ''
      try {
        await wsService.switchSession('')
      } catch (e) {
        console.error('[Sessions] 新建失败:', e)
        this.messages.push({ role: 'system', content: '新建对话失败: ' + (e.message || e) })
      }
    },

    async removeSession(sessionId) {
      if (this.busy) return
      const label = (this.sessions.find((s) => s.session_id === sessionId) || {}).title || '这条对话'
      if (!confirm(`删除「${label}」?\n\n对话内容会一起删掉,不可恢复。`)) return

      try {
        await sessionApi.remove(sessionId)
      } catch (e) {
        // 404 = 库里本来就没有（比如刚新建还没说话就删），当成已删除
        if (e.status !== 404) {
          this.messages.push({ role: 'system', content: '删除失败: ' + (e.message || e) })
          return
        }
      }

      const wasActive = sessionId === this.activeSessionId
      const remaining = this.sessions.filter((s) => s.session_id !== sessionId)
      this.sessions = remaining
      if (!wasActive) return
      // 删掉的正是当前这条：挪到剩下的第一条，一条都没了就开新的
      if (remaining.length) await this.selectSession(remaining[0].session_id)
      else await this.createSession()
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
  /* 7 个标签在 320px 里刚好排得下；再窄（小窗口/高 DPI）就横向滚，
     而不是换行把侧栏顶部顶掉一大块 */
  overflow-x: auto;
  scrollbar-width: none;
}
.sidebar-tabs::-webkit-scrollbar { display: none; }
.sidebar-tabs button {
  flex: 1 0 auto;
  padding: 10px 2px;
  background: none;
  border: none;
  color: #999;
  cursor: pointer;
  font-size: 10px;
  white-space: nowrap;
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
