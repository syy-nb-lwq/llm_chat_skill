<template>
  <div class="chat-area">
    <div class="messages" ref="messagesContainer">
      <div v-for="(msg, index) in messages" :key="index" class="message" :class="msg.role">
        <div class="message-content" v-html="renderMarkdown(msg.content)"></div>
        <!--
          产物下载:python_exec / write_script 生成的文件(报表、图片、视频…)。
          以前只落在后端工作区里,用户完全看不到。
        -->
        <div v-if="msg.artifacts && msg.artifacts.length" class="artifact-list">
          <div class="artifact-title">📎 生成的文件</div>
          <div v-for="a in msg.artifacts" :key="a.path" class="artifact-row">
            <a
              class="artifact-link"
              :href="artifactUrl(a)"
              target="_blank"
              rel="noopener"
              :title="a.path"
            >{{ a.name }}</a>
            <span class="artifact-size">{{ formatSize(a.size) }}</span>
            <a class="artifact-dl" :href="artifactUrl(a, true)" :download="a.name" title="下载">⬇</a>
          </div>
        </div>
        <div
          v-if="msg.clarification && msg.clarification.options && msg.clarification.options.length"
          class="clarify-options"
        >
          <button
            v-for="opt in msg.clarification.options"
            :key="opt"
            class="clarify-chip"
            :disabled="!connected"
            @click="sendMessage(opt)"
          >{{ opt }}</button>
        </div>
        <!--
          回答反馈:「👍 / 👎」是自演化最可靠的质量信号。
          关联靠 message_final 事件带的 execution_id。
        -->
        <div v-if="msg.role === 'assistant' && msg.execution_id" class="feedback-row">
          <button
            class="fb-btn"
            :class="{ active: msg.feedback === true }"
            :disabled="!connected"
            title="这个回答是对的"
            @click="sendFeedback(msg, true)"
          >👍</button>
          <button
            class="fb-btn"
            :class="{ active: msg.feedback === false }"
            :disabled="!connected"
            title="这个回答不对,记下来让系统改"
            @click="sendFeedback(msg, false)"
          >👎</button>
          <span v-if="msg.feedback !== undefined && msg.feedback !== null" class="fb-done">
            {{ msg.feedback ? '已标记为正确' : '已记为错误,系统会学习' }}
          </span>
        </div>
      </div>
    </div>
    <div class="input-area">
      <input
        v-model="inputMessage"
        @keyup.enter="sendMessage"
        placeholder="输入任务..."
        :disabled="!connected"
      />
      <button @click="sendMessage" :disabled="!connected || !inputMessage">发送</button>
      <!--
        「新对话」= 真的开一条新会话（左侧列表里会多一条），不是清空当前这条。
        以前这个位置是 ↺「重置」，做的是"同一个会话里让模型忘掉上文"——
        用户在列表里看不到任何变化，自然就问"新对话在哪儿建"。
        回答还在流的时候禁掉：切走会让上一条回答的增量飘进新会话。
      -->
      <button
        @click="$emit('new-session')"
        class="reset-btn"
        :disabled="busy"
        :title="busy ? '正在生成回答，等它说完再开新对话' : '新对话（当前这条会留在左侧列表里）'"
      >＋</button>
    </div>
  </div>
</template>

<script>
import MarkdownIt from 'markdown-it'
import { apiUrl } from '../api/client'
import { wsService } from '../websocket'

export default {
  name: 'ChatPanel',
  props: {
    connected: Boolean,
    messages: Array,
    /** 有一轮回答正在流式输出 */
    busy: Boolean,
  },
  emits: ['send', 'new-session', 'feedback'],
  data() {
    return {
      inputMessage: '',
      md: new MarkdownIt({ html: false, linkify: true, typographer: true, breaks: true }),
    }
  },
  watch: {
    messages: {
      deep: true,
      handler() { this.$nextTick(this.scrollToBottom) },
    },
  },
  methods: {
    renderMarkdown(content) {
      return content ? this.md.render(content) : ''
    },

    // 产物链接（本地单用户，无令牌）
    artifactUrl(artifact, download = false) {
      const base = artifact.url || `/api/artifacts/${artifact.path}`
      // 后端偶尔返回绝对地址（外部产物），直接用，不要再拼 base
      if (/^https?:\/\//i.test(base)) return base
      return apiUrl(base, download ? { download: true } : {})
    },

    formatSize(bytes) {
      const n = Number(bytes) || 0
      if (n < 1024) return `${n} B`
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
      return `${(n / 1024 / 1024).toFixed(1)} MB`
    },
    // sendMessage(prefill):prefill 是候选项文本(来自澄清按钮);
    // 回车触发时 prefill 是 KeyboardEvent,此时用输入框内容
    sendMessage(prefill) {
      const raw = typeof prefill === 'string' ? prefill : this.inputMessage
      const msg = raw.trim()
      if (!msg || !this.connected) return
      this.$emit('send', msg)
      this.inputMessage = ''
    },
    // 回答反馈:👍 / 👎
    sendFeedback(msg, accepted) {
      if (!msg || !msg.execution_id) return
      this.$emit('feedback', {
        execution_id: msg.execution_id,
        accepted,
        comment: accepted ? '' : '用户标记该回答不正确',
      })
    },
    scrollToBottom() {
      const c = this.$refs.messagesContainer
      if (c) c.scrollTop = c.scrollHeight
    },
  },
}
</script>

<style scoped>
.chat-area {
  display: flex;
  flex-direction: column;
  flex: 1;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 12px;
  overflow: hidden;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
}
.message {
  margin-bottom: 12px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
}
.message.user { justify-content: flex-end; align-items: flex-end; }
.message.assistant, .message.system { justify-content: flex-start; }
.message-content {
  max-width: 75%;
  padding: 10px 14px;
  border-radius: 12px;
  line-height: 1.5;
  word-break: break-word;
}
.message.user .message-content {
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: white;
}
.message.assistant .message-content {
  background: rgba(255, 255, 255, 0.08);
}
.message.system .message-content {
  background: rgba(255, 0, 0, 0.1);
  color: #c62828;
  font-size: 12px;
}
/* 澄清候选项:点一下就当用户回答了 */
.clarify-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.clarify-chip {
  padding: 5px 12px;
  border-radius: 14px;
  border: 1px solid rgba(102, 126, 234, 0.5);
  background: rgba(102, 126, 234, 0.12);
  color: #cfd6ff;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}
.clarify-chip:hover:not(:disabled) {
  background: rgba(102, 126, 234, 0.28);
  border-color: rgba(102, 126, 234, 0.9);
}
.clarify-chip:disabled { opacity: 0.45; cursor: not-allowed; }
/* 回答反馈按钮:自演化的质量信号入口 */
.feedback-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  margin-left: 4px;
}
.fb-btn {
  padding: 2px 8px;
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(255, 255, 255, 0.04);
  cursor: pointer;
  font-size: 13px;
  line-height: 1.6;
  opacity: 0.55;
  transition: opacity 0.15s, background 0.15s, border-color 0.15s;
}
.fb-btn:hover:not(:disabled) { opacity: 1; background: rgba(255, 255, 255, 0.12); }
.fb-btn.active {
  opacity: 1;
  border-color: rgba(102, 126, 234, 0.8);
  background: rgba(102, 126, 234, 0.22);
}
.fb-btn:disabled { cursor: not-allowed; }
.fb-done {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.45);
}
.input-area {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}
.input-area input {
  flex: 1;
  padding: 10px;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.05);
  color: inherit;
  font-size: 14px;
}
.input-area button {
  padding: 10px 16px;
  border-radius: 8px;
  border: none;
  background: linear-gradient(135deg, #667eea, #764ba2);
  color: white;
  cursor: pointer;
}
.input-area button:disabled { opacity: 0.5; cursor: not-allowed; }
.reset-btn { background: rgba(255, 255, 255, 0.1) !important; }
</style>