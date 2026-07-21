// WebSocket PubSub 客户端 - 实现 fastapi-websocket-rpc 协议
// 协议格式：JSON over WebSocket
// 发送：{"request": {"method": "...", "arguments": {...}, "call_id": "..."}}
// 接收：{"response": {"result": ..., "result_type": ..., "call_id": "..."}}
//       {"request": {"method": "notify", ...}} (服务端推送)

import { WS_BASE } from './config'  // M0-04

class WsService {
  constructor() {
    this.ws = null
    this.handlers = {}
    this.clientId = this._loadOrCreateClientId()
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
          await this._call('publish', {
            topics: [`init/${this.clientId}`],
            data: { client_id: this.clientId },
            sync: false,
          })

          this._emit('connected', {})

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

        // 发送pending消息
        while (this.pendingMessages.length > 0) {
          const msg = this.pendingMessages.shift()
          this._publish(msg.topic, msg.data)
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

            if (topic.startsWith(`events/${this.clientId}`)) {
              const eventData = payload?.data || payload
              const eventName = eventData?.event
              const eventPayload = eventData?.payload || {}
              console.log('[WS] Event:', eventName, eventPayload)
              if (eventName) {
                this._emit(eventName, eventPayload, data)
              }
            } else if (topic.startsWith(`log/${this.clientId}`)) {
              this._emit('log', payload, data)
            }
          }
        } catch (e) {
          console.error('[WS] Parse error:', e)
        }
      }

      this.ws.onerror = (error) => {
        console.error('[WS] Error:', error)
        this._emit('error', { message: '连接错误' })
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
          this._emit('disconnected', {})
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

  reset() {
    this._publish(`reset/${this.clientId}`, {})
  }

  ping() {
    this._publish(`ping/${this.clientId}`, {})
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

// M0-08: 同时导出类(供单元测试构造独立实例,不污染全局单例)
export { WsService }

export const wsService = new WsService()