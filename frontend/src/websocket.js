// WebSocket PubSub 客户端 - 实现 fastapi-websocket-rpc 协议
// 协议格式：JSON over WebSocket
// 发送：{"request": {"method": "...", "arguments": {...}, "call_id": "..."}}
// 接收：{"response": {"result": ..., "result_type": ..., "call_id": "..."}}
//       {"request": {"method": "notify", ...}} (服务端推送)

import { WS_BASE } from './config'  // M0-04
import { EV } from './events'

class WsService {
  constructor() {
    this.ws = null
    this.handlers = {}
    // connId: 客户端自带的 topic 句柄,只用于消息路由(C-01:身份由服务端签发)
    this.clientId = this._loadOrCreateClientId()
    // session: 服务端签发的 {client_id, session_id}
    this.session = null
    this.connected = false
    this.reconnectTimer = null
    this.reconnectAttempts = 0
    this.maxReconnectAttempts = 10
    this.pendingMessages = []
    this._isManualClose = false
    // RPC: call_id -> { resolve, reject }
    this._pendingCalls = new Map()
    // 订阅的 topic
    this._subscribedTopics = new Set()
  }

  _loadOrCreateClientId() {
    let cid = localStorage.getItem('skill_agent_client_id')
    if (!cid) {
      cid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = (Math.random() * 16) | 0
        const v = c === 'x' ? r : (r & 0x3) | 0x8
        return v.toString(16)
      })
      localStorage.setItem('skill_agent_client_id', cid)
    }
    return cid
  }

  // 上次用过的 session_id:重连时带上,服务端据此恢复会话
  _loadSessionId() {
    try {
      const raw = localStorage.getItem('skill_agent_session')
      if (!raw) return ''
      const parsed = JSON.parse(raw)
      return parsed?.session_id || ''
    } catch (e) {
      return ''
    }
  }

  _genUid() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = (Math.random() * 16) | 0
      const v = c === 'x' ? r : (r & 0x3) | 0x8
      return v.toString(16)
    })
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.CONNECTING || this.ws.readyState === WebSocket.OPEN)) {
      console.log('[WS] Already connecting or connected, skip')
      return
    }

    console.log('[WS] Connecting...')
    this._isManualClose = false

    try {
      this.ws = new WebSocket(`${WS_BASE}/pubsub`)

      this.ws.onopen = async () => {
        console.log('[WS] Connected!')
        this.connected = true
        this.reconnectAttempts = 0

        try {
          // 订阅 events topic
          await this._subscribe([`events/${this.clientId}`, `log/${this.clientId}`])

          // 触发 init topic (服务端订阅了 init/*)
          // 带上 session_id:服务端据此恢复上次的会话(会话内容全量保留)
          await this._init(this._loadSessionId())

          // 注意:这里不 emit('connected')。
          // 'connected' 事件等服务端签发身份后再发。
          // pending 消息同样等到身份到位后再冲刷(见 _flushPending)。

          // 通知 waitReady
          if (this._readyResolver) {
            this._readyResolver()
            this._readyResolver = null
            this._readyRejector = null
          }
        } catch (e) {
          console.error('[WS] Init failed:', e)
          if (this._readyRejector) {
            this._readyRejector(e)
            this._readyResolver = null
            this._readyRejector = null
          }
        }
      }

      this.ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)

          // 处理 RPC response
          if (data.response) {
            const { call_id, result } = data.response
            const pending = this._pendingCalls.get(call_id)
            if (pending) {
              this._pendingCalls.delete(call_id)
              pending.resolve(result)
            }
            return
          }

          // 处理 server push (notify)
          if (data.request && data.request.method === 'notify') {
            const { subscription, data: payload } = data.request.arguments || {}
            const topic = subscription?.topic
            if (!topic) return

            // **必须回 ACK** —— fastapi-websocket-rpc 是请求/响应式协议:
            // 服务端 publish() 会等客户端回响应才返回,不回就会把
            // 服务端那一轮的事件投递卡死(前端表现为"转圈不动")。
            this._ackNotify(data.request.call_id)

            if (topic.startsWith(`events/${this.clientId}`)) {
              const eventData = payload?.data || payload
              const eventName = eventData?.event
              const eventPayload = eventData?.payload || {}
              console.log('[WS] Event:', eventName, eventPayload)
              // 服务端签发的会话身份（本地单用户，无令牌）
              if (eventName === 'connected' && eventPayload?.session_id) {
                this.session = {
                  client_id: eventPayload.client_id,
                  session_id: eventPayload.session_id,
                }
                try {
                  localStorage.setItem('skill_agent_session', JSON.stringify(this.session))
                } catch (e) {
                  console.warn('[WS] 无法保存 session', e)
                }
                this._flushPending()
              }
              if (eventName) {
                this._emit(eventName, eventPayload, data)
              }
            } else if (topic.startsWith(`log/${this.clientId}`)) {
              this._emit(EV.LOG, payload, data)
            }
          }
        } catch (e) {
          console.error('[WS] Parse error:', e)
        }
      }

      this.ws.onerror = (error) => {
        console.error('[WS] Error:', error)
        this._emit(EV.ERROR, { message: '连接错误' })
      }

      this.ws.onclose = (event) => {
        console.log('[WS] Closed, code:', event.code)
        this.connected = false
        this._subscribedTopics.clear()
        this._pendingCalls.clear()

        // 通知 waitReady 调用方连接已断开
        if (this._readyRejector) {
          this._readyRejector(new Error('WebSocket closed'))
          this._readyResolver = null
          this._readyRejector = null
        }
        this._readyPromise = null

        if (this.reconnectTimer) {
          clearTimeout(this.reconnectTimer)
          this.reconnectTimer = null
        }

        if (!this._isManualClose) {
          this._emit(EV.DISCONNECTED, {})
          this._scheduleReconnect()
        }
      }
    } catch (error) {
      console.error('[WS] Connection failed:', error)
      if (!this._isManualClose) {
        this._scheduleReconnect()
      }
    }
  }

  _scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.log('[WS] Max reconnect attempts reached')
      return
    }
    if (this.reconnectTimer) return

    this.reconnectAttempts++
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts - 1), 30000)
    console.log(`[WS] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts}/${this.maxReconnectAttempts})`)

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  // 回复服务端 notify 的 ACK(不 ACK 会让服务端 publish 一直等待)
  _ackNotify(callId) {
    if (!callId || !this.ws || this.ws.readyState !== WebSocket.OPEN) return
    try {
      this.ws.send(JSON.stringify({
        response: {
          result: null,
          result_type: 'NoneType',
          call_id: callId,
        },
      }))
    } catch (e) {
      console.warn('[WS] ACK notify failed:', e)
    }
  }

  // RPC: 调用服务端方法
  _call(method, args = {}, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('WebSocket not connected'))
        return
      }
      const call_id = this._genUid()
      const timer = setTimeout(() => {
        this._pendingCalls.delete(call_id)
        reject(new Error(`RPC ${method} timeout`))
      }, timeoutMs)
      this._pendingCalls.set(call_id, {
        resolve: (v) => { clearTimeout(timer); resolve(v) },
        reject: (e) => { clearTimeout(timer); reject(e) },
      })
      const msg = {
        request: {
          method,
          arguments: args,
          call_id,
        },
      }
      try {
        this.ws.send(JSON.stringify(msg))
      } catch (e) {
        this._pendingCalls.delete(call_id)
        clearTimeout(timer)
        reject(e)
      }
    })
  }

  /**
   * 向服务端登记（或重新登记）身份。`sessionId` 为空时服务端新签发一个。
   *
   * 抽成方法是因为它现在有两个调用点：首次连接（onopen）和切换会话。
   */
  _init(sessionId = '') {
    return this._call('publish', {
      topics: [`init/${this.clientId}`],
      data: { client_id: this.clientId, session_id: sessionId },
      sync: false,
    })
  }

  // 订阅 topic
  async _subscribe(topics) {
    try {
      await this._call('subscribe', { topics })
      topics.forEach(t => this._subscribedTopics.add(t))
      console.log('[WS] Subscribed:', topics)
    } catch (e) {
      console.error('[WS] Subscribe failed:', e)
    }
  }

  // 发布到 topic
  _publish(topic, data) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      this.pendingMessages.push({ topic, data })
      return
    }
    this._call('publish', { topics: [topic], data, sync: false }).catch(e => {
      console.error('[WS] Publish failed:', e)
    })
  }

  // 冲刷排队消息(身份签发完成后调用)
  _flushPending() {
    while (this.pendingMessages.length > 0) {
      const msg = this.pendingMessages.shift()
      this._publish(msg.topic, msg.data)
    }
  }

  _emit(event, data, raw) {
    const handlers = this.handlers[event] || []
    handlers.forEach(cb => {
      try { cb(data, raw) } catch (e) { console.error(e) }
    })
  }

  on(event, handler) {
    if (!this.handlers[event]) this.handlers[event] = []
    this.handlers[event].push(handler)
  }

  off(event, handler) {
    if (this.handlers[event]) {
      this.handlers[event] = this.handlers[event].filter(cb => cb !== handler)
    }
  }

  // 公开 API
  chat(message) {
    this._publish(`chat/${this.clientId}`, { content: message })
  }

  /**
   * 切到另一个会话；`sessionId` 留空 = 开一个**新**会话。
   *
   * 服务端换会话的方式就是"用另一个 session_id 重新登记身份"：`issue_identity`
   * 会拿新 session_id 去 `get_or_create`（没跑过就新建 Agent），所以只要重发
   * 一次 init —— 不用断开重连，topic 还是同一个。
   *
   * 注意 **connected 事件是异步回来的**：这个方法 resolve 时 `this.session`
   * 还是旧值，新 session_id 要等 `connected` 事件（服务端签发后推送）才落地。
   * 调用方不要拿 resolve 时机当"切换完成"。
   */
  async switchSession(sessionId = '') {
    const sid = String(sessionId || '')
    try {
      if (sid) {
        this.session = { client_id: this.session?.client_id || '', session_id: sid }
        localStorage.setItem('skill_agent_session', JSON.stringify(this.session))
      } else {
        // 新会话必须先清掉本地记录：否则下次重连 `_loadSessionId` 又把旧会话
        // 带回来，用户会看到"新对话里怎么还有刚才那些话"
        this.session = null
        localStorage.removeItem('skill_agent_session')
      }
    } catch (e) {
      console.warn('[WS] 无法保存 session', e)
    }
    return this._init(sid)
  }


  // 回答反馈(👍/👎):自演化最可靠的质量信号。
  // execution_id 来自 message_final 事件,标识"对哪一条回答"的评价。
  feedback(executionId, accepted, comment = '') {
    if (!executionId) return
    this._publish(`feedback/${this.clientId}`, {
      execution_id: executionId,
      accepted: !!accepted,
      comment: comment || '',
    })
  }

  disconnect() {
    console.log('[WS] Manual disconnect')
    this._isManualClose = true
    this.reconnectAttempts = this.maxReconnectAttempts

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }

    if (this.ws) {
      this.ws.onclose = null
      this.ws.close()
      this.ws = null
    }

    this.connected = false
  }
}

export const wsService = new WsService()