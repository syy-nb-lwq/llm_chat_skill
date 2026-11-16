<template>
  <div class="chat-area">
    <div class="messages" ref="messagesContainer">
      <div v-for="(msg, index) in messages" :key="index" class="message" :class="msg.role">
        <div class="message-content" v-html="renderMarkdown(msg.content)"></div>
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
      <button @click="resetChat" class="reset-btn" title="重置对话">↺</button>
    </div>
  </div>
</template>

<script>
import MarkdownIt from 'markdown-it'

export default {
  name: 'ChatPanel',
  props: {
    connected: Boolean,
    messages: Array,
  },
  emits: ['send', 'reset', 'feedback'],
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
    // sendMessage(prefill):prefill 是候选项文本(来自澄清按钮);
    // 回车触发时 prefill 是 KeyboardEvent,此时用输入框内容
    sendMessage(prefill) {
      const raw = typeof prefill === 'string' ? prefill : this.inputMessage
      const msg = raw.trim()
      if (!msg || !this.connected) return
      this.$emit('send', msg)
      this.inputMessage = ''
    },
    resetChat() { this.$emit('reset') },
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