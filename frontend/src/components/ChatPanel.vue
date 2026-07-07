<template>
  <div class="chat-area">
    <div class="messages" ref="messagesContainer">
      <div v-for="(msg, index) in messages" :key="index" class="message" :class="msg.role">
        <div class="message-content" v-html="renderMarkdown(msg.content)"></div>
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
  emits: ['send', 'reset'],
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
    sendMessage() {
      const msg = this.inputMessage.trim()
      if (!msg || !this.connected) return
      this.$emit('send', msg)
      this.inputMessage = ''
    },
    resetChat() { this.$emit('reset') },
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
}
.message.user { justify-content: flex-end; }
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