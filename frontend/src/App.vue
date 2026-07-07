<template>
  <div class="app-container">
    <header class="header">
      <h1>📚 Skill Agent</h1>
      <span class="status" :class="{ connected: isConnected }">
        {{ isConnected ? '已连接' : '未连接' }}
      </span>
    </header>
    
    <main class="main-content">
      <aside class="sidebar">
        <div class="sidebar-tabs">
          <button 
            :class="{ active: activeTab === 'tools' }" 
            @click="activeTab = 'tools'"
          >
            🔧 工具
          </button>
          <button 
            :class="{ active: activeTab === 'steps' }" 
            @click="activeTab = 'steps'"
          >
            📋 流程
          </button>
        </div>
        
        <!-- 工具列表 -->
        <div v-if="activeTab === 'tools'" class="sidebar-content">
          <h4>可用工具</h4>
          <div class="tools-list">
            <div v-for="tool in tools" :key="tool.name" class="tool-item">
              <span class="tool-icon">⚙️</span>
              <div class="tool-info">
                <strong>{{ tool.name }}</strong>
                <p>{{ tool.description }}</p>
              </div>
            </div>
            <div v-if="tools.length === 0" class="empty-state">
              加载中...
            </div>
          </div>
          
          <h4>已学技能</h4>
          <div class="skills-list">
            <div v-for="skill in skills" :key="skill.name" class="skill-item">
              <strong>{{ skill.name }}</strong>
              <span class="skill-tags">{{ skill.tags.join(', ') || '通用' }}</span>
            </div>
            <div v-if="skills.length === 0" class="empty-state">
              暂无已学技能
            </div>
          </div>
        </div>
        
        <!-- 流程步骤 -->
        <div v-if="activeTab === 'steps'" class="sidebar-content">
          <div class="steps-list">
            <div v-for="(step, index) in thinkingSteps" :key="index" 
                 class="step-item" :class="step.type">
              <span class="step-icon">{{ step.icon }}</span>
              <span class="step-text">{{ step.text }}</span>
            </div>
            <div v-if="thinkingSteps.length === 0" class="empty-state">
              等待输入...
            </div>
          </div>
        </div>
      </aside>
      
      <section class="chat-area">
        <div class="messages" ref="messagesContainer">
          <div v-for="(msg, index) in messages" :key="index" 
               class="message" :class="msg.role">
            <div class="message-content" v-html="renderMarkdown(msg.content)"></div>
          </div>
        </div>
        
        <div class="input-area">
          <input 
            v-model="inputMessage" 
            @keyup.enter="sendMessage"
            placeholder="输入任务..."
            :disabled="!isConnected"
          >
          <button @click="sendMessage" :disabled="!isConnected || !inputMessage">
            发送
          </button>
          <button @click="resetChat" class="reset-btn" title="重置对话">
            ↺
          </button>
        </div>
      </section>
    </main>
  </div>
</template>

<script>
import { wsService } from './websocket'
import MarkdownIt from 'markdown-it'

export default {
  name: 'App',
  data() {
    return {
      inputMessage: '',
      messages: [],
      isConnected: false,
      thinkingSteps: [],
      tools: [],
      skills: [],
      activeTab: 'tools',
      md: null
    }
  },
  
  mounted() {
    this.md = new MarkdownIt({
      html: false,
      linkify: true,
      typographer: true,
      breaks: true
    })
    this.initWebSocket()
    this.loadTools()
    this.loadSkills()
  },
  
  beforeUnmount() {
    wsService.disconnect()
  },
  
  methods: {
    renderMarkdown(content) {
      if (!content) return ''
      return this.md.render(content)
    },
    
    addStep(type, icon, text) {
      this.thinkingSteps.push({ type, icon, text })
      this.scrollToBottom()
    },
    
    clearSteps() {
      this.thinkingSteps = []
    },
    
    async loadTools() {
      try {
        const res = await fetch('http://localhost:8000/api/tools')
        const data = await res.json()
        this.tools = data.tools || []
      } catch (error) {
        console.error('Failed to load tools:', error)
        this.tools = [
          { name: 'weather_query', description: '查询天气' },
          { name: 'web_search', description: '网络搜索' }
        ]
      }
    },
    
    async loadSkills() {
      try {
        const res = await fetch('http://localhost:8000/api/skills')
        const data = await res.json()
        this.skills = data.skills || []
      } catch (error) {
        console.error('Failed to load skills:', error)
      }
    },
    
    async initWebSocket() {
      wsService.on('connected', () => {
        this.isConnected = true
        this.addStep('info', '✓', '已连接到服务器')
      })
      
      wsService.on('disconnected', () => {
        this.isConnected = false
        this.addStep('error', '✗', '连接已断开')
      })
      
      wsService.on('step', (data) => {
        this.activeTab = 'steps'
        this.addStep(data.type || 'info', this.getStepIcon(data.type), data.message)
      })
      
      wsService.on('thinking', () => {
        this.clearSteps()
        this.addStep('thinking', '🔄', '正在处理...')
      })
      
      wsService.on('response', (data) => {
        this.clearSteps()
        this.messages.push({
          role: 'assistant',
          content: data.content
        })
        this.loadSkills()
        this.scrollToBottom()
      })
      
      wsService.on('error', (data) => {
        this.clearSteps()
        this.messages.push({
          role: 'system',
          content: '错误: ' + data.message
        })
      })
      
      wsService.on('reset', () => {
        this.messages = []
        this.clearSteps()
        this.addStep('info', '✓', '对话已重置')
      })
      
      try {
        await wsService.connect()
      } catch (error) {
        console.error('Failed to connect:', error)
      }
    },
    
    getStepIcon(type) {
      const icons = {
        'intent': '🎯',
        'task': '📋',
        'tool': '🔧',
        'success': '✓',
        'error': '✗',
        'info': 'ℹ',
        'thinking': '🔄',
        'weather': '🌤️',
        'search': '🔍',
        'plan': '✨'
      }
      return icons[type] || '•'
    },
    
    sendMessage() {
      if (!this.inputMessage.trim() || !this.isConnected) return
      
      const message = this.inputMessage.trim()
      this.messages.push({
        role: 'user',
        content: message
      })
      this.inputTab = 'steps'
      this.inputMessage = ''
      this.scrollToBottom()
      
      wsService.chat(message)
    },
    
    resetChat() {
      wsService.reset()
      this.messages = []
      this.clearSteps()
    },
    
    scrollToBottom() {
      this.$nextTick(() => {
        const container = this.$refs.messagesContainer
        if (container) {
          container.scrollTop = container.scrollHeight
        }
      })
    }
  }
}
</script>

<style>
.app-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  max-width: 1400px;
  margin: 0 auto;
  padding: 20px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  margin-bottom: 20px;
}

.header h1 {
  font-size: 1.8rem;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
}

.status {
  padding: 6px 16px;
  border-radius: 20px;
  font-size: 0.85rem;
  background: rgba(255, 100, 100, 0.2);
  color: #ff6b6b;
}

.status.connected {
  background: rgba(100, 255, 100, 0.2);
  color: #69db7c;
}

.main-content {
  display: flex;
  gap: 20px;
  flex: 1;
  min-height: 0;
}

.sidebar {
  width: 320px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
}

.sidebar-tabs {
  display: flex;
  gap: 8px;
  margin-bottom: 16px;
}

.sidebar-tabs button {
  flex: 1;
  padding: 10px 12px;
  border: none;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.05);
  color: #a0a0a0;
  cursor: pointer;
  font-size: 0.9rem;
  transition: all 0.2s;
}

.sidebar-tabs button:hover {
  background: rgba(255, 255, 255, 0.1);
}

.sidebar-tabs button.active {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

.sidebar-content {
  flex: 1;
  overflow-y: auto;
}

.sidebar-content h4 {
  font-size: 0.85rem;
  color: #888;
  margin: 12px 0 8px;
  font-weight: normal;
}

.tools-list, .skills-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.tool-item {
  display: flex;
  gap: 10px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 10px;
}

.tool-icon {
  font-size: 1.2rem;
  flex-shrink: 0;
}

.tool-info strong {
  font-size: 0.85rem;
  color: #e0e0e0;
  font-family: monospace;
}

.tool-info p {
  font-size: 0.75rem;
  color: #888;
  margin: 4px 0 0;
}

.skill-item {
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 10px;
}

.skill-item strong {
  display: block;
  font-size: 0.9rem;
  color: #e0e0e0;
}

.skill-tags {
  font-size: 0.75rem;
  color: #667eea;
}

.steps-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.step-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 10px;
  font-size: 0.85rem;
  animation: fadeIn 0.3s ease;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(-10px); }
  to { opacity: 1; transform: translateY(0); }
}

.step-icon {
  flex-shrink: 0;
  font-size: 1rem;
}

.step-text {
  color: #c0c0c0;
  line-height: 1.4;
}

.step-item.thinking { border-left: 3px solid #667eea; }
.step-item.intent { border-left: 3px solid #f093fb; }
.step-item.task { border-left: 3px solid #4facfe; }
.step-item.tool { border-left: 3px solid #43e97b; }
.step-item.weather { border-left: 3px solid #fa709a; }
.step-item.search { border-left: 3px solid #fee140; }
.step-item.plan { border-left: 3px solid #a8edea; }
.step-item.success { border-left: 3px solid #69db7c; }
.step-item.error { border-left: 3px solid #ff6b6b; color: #ff6b6b; }

.empty-state {
  color: #666;
  text-align: center;
  padding: 20px;
  font-size: 0.85rem;
}

.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 16px;
  overflow: hidden;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.message {
  max-width: 85%;
  padding: 14px 18px;
  border-radius: 16px;
  line-height: 1.6;
}

.message.user {
  align-self: flex-end;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-bottom-right-radius: 4px;
}

.message.assistant {
  align-self: flex-start;
  background: rgba(255, 255, 255, 0.1);
  color: #e0e0e0;
  border-bottom-left-radius: 4px;
}

.message.system {
  align-self: center;
  background: transparent;
  color: #ff6b6b;
  font-size: 0.9rem;
}

.message-content h1, .message-content h2, .message-content h3 {
  margin: 1em 0 0.5em;
  color: #fff;
}

.message-content h1 { font-size: 1.4em; }
.message-content h2 { font-size: 1.2em; }
.message-content h3 { font-size: 1.1em; }

.message-content p { margin: 0.5em 0; }
.message-content ul, .message-content ol { margin: 0.5em 0; padding-left: 1.5em; }
.message-content li { margin: 0.3em 0; }
.message-content code {
  background: rgba(0, 0, 0, 0.3);
  padding: 2px 6px;
  border-radius: 4px;
  font-family: 'Fira Code', monospace;
  font-size: 0.9em;
}

.message-content pre {
  background: rgba(0, 0, 0, 0.3);
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
  margin: 0.5em 0;
}

.message-content pre code { background: none; padding: 0; }
.message-content strong { color: #667eea; }
.message-content a { color: #4facfe; }
.message-content blockquote {
  border-left: 3px solid #667eea;
  margin: 0.5em 0;
  padding-left: 1em;
  color: #a0a0a0;
}

.input-area {
  display: flex;
  gap: 12px;
  padding: 20px;
  background: rgba(0, 0, 0, 0.2);
}

.input-area input {
  flex: 1;
  padding: 14px 20px;
  border: none;
  border-radius: 30px;
  background: rgba(255, 255, 255, 0.1);
  color: #e0e0e0;
  font-size: 1rem;
  outline: none;
}

.input-area input:focus { background: rgba(255, 255, 255, 0.15); }
.input-area input::placeholder { color: #666; }
.input-area input:disabled { opacity: 0.5; }

.input-area button {
  padding: 14px 28px;
  border: none;
  border-radius: 30px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  font-size: 1rem;
  cursor: pointer;
  transition: opacity 0.2s;
}

.input-area button:hover:not(:disabled) { opacity: 0.9; }
.input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
.input-area .reset-btn {
  padding: 14px 20px;
  background: rgba(255, 255, 255, 0.1);
  font-size: 1.2rem;
}
</style>
