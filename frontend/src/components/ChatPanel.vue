<template>
  <div class="chat-area">
    <div class="messages" ref="messagesContainer">
      <div v-for="(msg, index) in rendered" :key="index" class="message" :class="msg.role">
        <div v-if="msg.html" class="message-content" v-html="msg.html"></div>
        <!--
          用户上传的文件:气泡下方一行 chip。
          正文里的「【已上传文件】」段被 parseMessage 拆出来了 ——
          那段是给 Agent 看的路径清单，直接摊在气泡里又长又难看。
        -->
        <div v-if="msg.files.length" class="message-files">
          <div v-for="f in msg.files" :key="f.path" class="msg-file" :title="f.path">
            <span class="attach-icon">{{ fileIcon(f.name) }}</span>
            <span class="msg-file-name">{{ f.name }}</span>
          </div>
        </div>
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
    <div
      class="composer"
      :class="{ dragging }"
      @dragover.prevent="onDragOver"
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <!-- 待发送的附件。上传是**选完立刻传**，不是等点发送才传：
           早传早报错（格式不支持 / 超限），不用等到 agent 那边才炸。 -->
      <div v-if="attachments.length || uploading || uploadError" class="attach-tray">
        <div v-for="(a, i) in attachments" :key="a.path" class="attach-chip">
          <span class="attach-icon">{{ fileIcon(a.name) }}</span>
          <span class="attach-name" :title="a.path">{{ a.name }}</span>
          <span class="attach-size">{{ formatSize(a.size) }}</span>
          <button class="attach-del" title="移除" @click="removeAttachment(i)">✕</button>
        </div>
        <div v-if="uploading" class="attach-chip attach-pending">
          <span class="attach-icon">⏳</span>上传中…
        </div>
        <div v-if="uploadError" class="attach-error">{{ uploadError }}</div>
        <div v-if="dragging" class="attach-hint">松开即可上传</div>
      </div>

      <div class="input-area">
        <!--
          文档工具（read_document）一直都在，但它吃的是后端磁盘上的路径。
          浏览器里的 File 对象对后端完全不可见 —— 没有这个入口，
          "能读 PDF / docx" 就等于没有。
        -->
        <button
          class="attach-btn"
          :disabled="!connected || uploading"
          :title="`上传文档（${ACCEPT_HINT}）`"
          @click="pickFiles"
        >📎</button>
        <input
          ref="fileInput"
          type="file"
          multiple
          hidden
          :accept="ACCEPT"
          @change="onFilesPicked"
        />
        <input
          v-model="inputMessage"
          @keyup.enter="sendMessage"
          :placeholder="attachments.length ? '说说要拿这些文件做什么…' : '输入任务...'"
          :disabled="!connected"
        />
        <button @click="sendMessage" :disabled="!connected || (!inputMessage.trim() && !attachments.length)">发送</button>
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
  </div>
</template>

<script>
import MarkdownIt from 'markdown-it'
import { apiUrl, uploadApi } from '../api/client'
import { wsService } from '../websocket'

//: 可上传的扩展名 —— 必须与后端 tools/document.py 的 SUPPORTED_SUFFIXES 对齐。
//: 多写一个只是让文件选择框更宽松，服务端仍会拒；少写一个用户就选不到。
const ACCEPT = [
  '.docx', '.xlsx', '.xlsm', '.pptx', '.pdf',
  '.md', '.markdown', '.txt', '.csv', '.html', '.htm', '.json', '.xml', '.log',
].join(',')

const ACCEPT_HINT = 'pdf / docx / xlsx / pptx / md / txt / csv…'

//: 附件清单在消息正文里的分节标记。
//:
//: 用纯文本而不是 WS 协议字段，是因为这份清单**必须跟消息一起被存下来** ——
//: 否则下次打开这条会话，用户看到"帮我总结这个文档"却不知道说的是哪个文件。
//: 代价是正文里多一段路径（渲染时由 parseMessage 拆出来变成 chip）。
const ATTACH_HEADER = '【已上传文件】'

const FILE_ICONS = {
  pdf: '📕', docx: '📄', xlsx: '📊', xlsm: '📊', pptx: '📽',
  md: '📝', markdown: '📝', txt: '📄', csv: '📊',
  json: '🧾', xml: '🧾', html: '🌐', htm: '🌐', log: '📄',
}

//: markdown 渲染结果缓存（模块级，不进响应式）。
//:
//: 流式输出时 messages 每个 delta 都会变，computed 重算就会把**所有**历史消息
//: 重新 render 一遍。历史消息的 content 从不变化，缓存命中率很高；
//: 真正在变的只有最后一条。不缓存的话长会话流式输出会明显卡。
const HTML_CACHE = new Map()
const HTML_CACHE_MAX = 300

export default {
  name: 'ChatPanel',
  props: {
    connected: Boolean,
    messages: Array,
    /** 有一轮回答正在流式输出 */
    busy: Boolean,
    /** 当前会话 id —— 上传按会话分目录，将来删会话能整批清掉 */
    sessionId: { type: String, default: '' },
  },
  emits: ['send', 'new-session', 'feedback'],
  data() {
    return {
      inputMessage: '',
      md: new MarkdownIt({ html: false, linkify: true, typographer: true, breaks: true }),
      /** 已上传、等着随下一条消息发出去的附件 */
      attachments: [],
      uploading: false,
      uploadError: '',
      dragging: false,
      ACCEPT,
      ACCEPT_HINT,
    }
  },
  computed: {
    /**
     * 把每条消息拆成「正文 HTML + 附件 chips」。
     *
     * 放在 computed 而不是模板里调方法：v-for 每帧都会重渲染，
     * 在里面反复 markdown 渲染历史消息是纯浪费（长会话肉眼可见地卡）。
     */
    rendered() {
      return (this.messages || []).map((m) => {
        const parsed = this.parseMessage(m.content || '')
        return {
          ...m,
          html: this.renderMarkdown(parsed.text),
          files: parsed.files,
        }
      })
    },
  },
  watch: {
    messages: {
      deep: true,
      handler() { this.$nextTick(this.scrollToBottom) },
    },
    /**
     * 换会话就丢掉待发送的附件。
     *
     * 附件是挂在**一条会话**上的（上传目录按 session 分），切走之后还留着，
     * 下一个会话发出去就会带着上一条会话的文件 —— 路径虽然还有效，
     * 但语义上完全是错的（用户以为传给了新会话）。
     */
    sessionId() {
      this.attachments = []
      this.uploadError = ''
    },
  },
  methods: {
    renderMarkdown(content) {
      if (!content) return ''
      const cached = HTML_CACHE.get(content)
      if (cached !== undefined) return cached
      const html = this.md.render(content)
      // 简单的封顶：聊天内容本身不重复，超出就整个丢掉重建
      if (HTML_CACHE.size >= HTML_CACHE_MAX) HTML_CACHE.clear()
      HTML_CACHE.set(content, html)
      return html
    },

    // ===== 附件 =====

    fileIcon(name) {
      const ext = String(name || '').split('.').pop().toLowerCase()
      return FILE_ICONS[ext] || '📎'
    },

    /**
     * 把正文尾部的【已上传文件】段拆出来。
     *
     * 用 `lastIndexOf`：附件段永远在末尾，而用户正文里万一手打了这几个字
     * （比如贴一段日志），也不会被误当成分节标记。
     */
    parseMessage(content) {
      const idx = content.lastIndexOf(ATTACH_HEADER)
      if (idx < 0) return { text: content, files: [] }

      const files = []
      for (const line of content.slice(idx).split('\n')) {
        // 格式：`- 报告.docx → D:\...\uploads\s-xxx\ab12_报告.docx`
        const m = line.match(/^-\s+(.+?)\s*→\s*(.+)$/)
        if (m) files.push({ name: m[1].trim(), path: m[2].trim() })
      }
      return { text: content.slice(0, idx).trim(), files }
    },

    /** 用户正文 + 附件清单 —— 这就是真正发给后端的 content */
    composeContent(text, files) {
      if (!files.length) return text
      const lines = files.map((f) => `- ${f.name} → ${f.path}`)
      const block = [ATTACH_HEADER, ...lines].join('\n')
      return text ? `${text}\n\n${block}` : block
    },

    pickFiles() {
      const el = this.$refs.fileInput
      if (el) el.click()
    },

    onFilesPicked(event) {
      const files = Array.from(event.target.files || [])
      // 清空 value，否则连选两次同一个文件不会触发 change
      event.target.value = ''
      this.uploadFiles(files)
    },

    /**
     * 逐个上传。串行而不是 Promise.all：一次拖二十个文件时，
     * 并发上传只会让进度看起来更乱，也让后端的 30MB 校验更难给出准确提示。
     */
    async uploadFiles(files) {
      if (!files.length) return
      this.dragging = false
      this.uploadError = ''

      for (const file of files) {
        this.uploading = true
        try {
          const info = await uploadApi.send(file, this.sessionId)
          if (!info.readable) {
            // 能存但读不了（改了后缀的旧 .doc 之类）。这种文件附上去只会让
            // agent 白跑一次 read_document 再报错，直接挡在这一步。
            this.uploadError = `${info.name}：${info.hint || '这个格式读不了'}`
            continue
          }
          this.attachments.push({
            name: info.name,
            path: info.path,
            size: info.size,
            format: info.format,
          })
        } catch (err) {
          this.uploadError = `${file.name}：${err.message || err}`
        } finally {
          this.uploading = false
        }
      }
    },

    removeAttachment(index) {
      this.attachments.splice(index, 1)
      if (!this.attachments.length) this.uploadError = ''
    },

    onDragOver() {
      if (this.connected && !this.uploading) this.dragging = true
    },

    onDragLeave(event) {
      // 拖过子元素也会触发 dragleave，只认真正离开整个 composer 的那次
      if (event.currentTarget && event.relatedTarget
        && event.currentTarget.contains(event.relatedTarget)) return
      this.dragging = false
    },

    onDrop(event) {
      this.dragging = false
      if (!this.connected || this.uploading) return
      const files = Array.from((event.dataTransfer && event.dataTransfer.files) || [])
      this.uploadFiles(files)
    },

    // ===== 往返 =====

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
      const files = this.attachments
      // 只传文件不写字也允许发 —— "帮我看看"这种话本来就可以省
      if ((!msg && !files.length) || !this.connected || this.uploading) return
      this.$emit('send', this.composeContent(msg, files))
      this.inputMessage = ''
      this.attachments = []
      this.uploadError = ''
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
  /* 纵向 flex 子项默认 min-height:auto(=内容高度)，不会收缩。
     不加这行，.messages 的 overflow-y:auto 永远不生效，
     内容会把整页撑开，用户得拖网页滚动条才能看完全部对话。 */
  min-height: 0;
  background: rgba(255, 255, 255, 0.03);
  border-radius: 12px;
  overflow: hidden;
}
.messages {
  flex: 1;
  /* 同理：让它能收缩到容器高度以内，滚动条才起作用 */
  min-height: 0;
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
/* 上传的附件（气泡下方一行 chip） */
.message-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
  max-width: 75%;
}
.msg-file {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 260px;
  padding: 4px 9px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.12);
  font-size: 12px;
  color: #ddd;
}
.msg-file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attach-icon { flex-shrink: 0; }
.composer {
  border-top: 1px solid rgba(255, 255, 255, 0.05);
  transition: background 0.15s, box-shadow 0.15s;
}
/* 拖着文件悬停时给整条输入区一个明确的落点 */
.composer.dragging {
  background: rgba(102, 126, 234, 0.1);
  box-shadow: inset 0 0 0 2px rgba(102, 126, 234, 0.6);
}
.attach-tray {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 10px 12px 0;
}
.attach-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 300px;
  padding: 4px 8px;
  border-radius: 8px;
  background: rgba(102, 126, 234, 0.16);
  border: 1px solid rgba(102, 126, 234, 0.4);
  font-size: 12px;
  color: #cfd6ff;
}
.attach-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attach-size {
  flex-shrink: 0;
  color: rgba(255, 255, 255, 0.4);
}
.attach-del {
  padding: 0 2px;
  border: none;
  background: none;
  color: inherit;
  font-size: 11px;
  opacity: 0.6;
  cursor: pointer;
}
.attach-del:hover { opacity: 1; }
.attach-pending {
  background: rgba(255, 255, 255, 0.06);
  border-color: rgba(255, 255, 255, 0.12);
  color: rgba(255, 255, 255, 0.6);
}
/* 错误要占一整行 —— 文件名 + 原因往往比按钮还长 */
.attach-error {
  width: 100%;
  font-size: 11px;
  line-height: 1.5;
  color: #ff9a9a;
}
.attach-hint {
  width: 100%;
  font-size: 12px;
  color: #cfd6ff;
}
.input-area {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px;
}
/* 📎 是次要动作，不该跟「发送」抢视觉重量 */
.attach-btn {
  padding: 8px 10px !important;
  background: rgba(255, 255, 255, 0.08) !important;
  font-size: 15px;
  line-height: 1;
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