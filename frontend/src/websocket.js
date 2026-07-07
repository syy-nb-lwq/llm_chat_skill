// WebSocket 服务 - 新版:统一 event 协议 / clientId 持久化 / 无限重连
class WsService {
  constructor() {
    this.ws = null
    this.handlers = new Map()
    this.outbox = []
    this.reconnectAttempts = 0
    this.maxReconnectAttempts = Infinity
    this.reconnectDelay = 3000
    this.url = ''
    this.clientId = this._loadOrCreateClientId()
    this.state = 'idle'
  }

  _loadOrCreateClientId() {
    let cid = localStorage.getItem('skill_agent_client_id')
    if (!cid) {
      // crypto.randomUUID 在现代浏览器/HTTPS 下可用,fallback 到 v4
      if (window.crypto && crypto.randomUUID) {
        cid = crypto.randomUUID()
      } else {
        cid = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
          const r = (Math.random() * 16) | 0
          const v = c === 'x' ? r : (r & 0x3) | 0x8
          return v.toString(16)
        })
      }
      localStorage.setItem('skill_agent_client_id', cid)
    }
    return cid
  }

  connect(url = 'ws://localhost:8000/ws/chat') {
    this.url = url
    this.state = 'connecting'
    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.url)
        this.ws.onopen = () => {
          this.state = 'connected'
          this.reconnectAttempts = 0
          // 立即发 init
          this._send({ type: 'init', client_id: this.clientId })
          // 刷掉 outbox
          while (this.outbox.length > 0) {
            const m = this.outbox.shift()
            this._send(m)
          }
          this._emit('connected', { client_id: this.clientId })
          resolve()
        }
        this.ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data)
            // 新协议:type=event, 派发按 event 字段
            if (data.type === 'event') {
              this._emit(data.event, data.payload || {}, data)
            } else {
              // 兼容老协议
              this._emit(data.type, data)
            }
          } catch (e) {
            console.error('Failed to parse message:', e)
          }
        }
        this.ws.onerror = (error) => {
          this._emit('error', { message: '连接错误' })
        }
        this.ws.onclose = () => {
          this.state = 'disconnected'
          this._emit('disconnected', { message: '连接已断开' })
          this._scheduleReconnect()
        }
      } catch (error) {
        reject(error)
      }
    })
  }

  _scheduleReconnect() {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++
      const delay = Math.min(this.reconnectDelay * Math.pow(1.5, this.reconnectAttempts - 1), 30000)
      setTimeout(() => {
        this.connect(this.url).catch(() => {})
      }, delay)
    }
  }

  _send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj))
    } else {
      this.outbox.push(obj)
    }
  }

  chat(message) { this._send({ type: 'chat', content: message }) }
  reset() { this._send({ type: 'reset' }) }
  ping() { this._send({ type: 'ping' }) }

  on(event, handler) {
    if (!this.handlers.has(event)) this.handlers.set(event, [])
    this.handlers.get(event).push(handler)
  }

  off(event, handler) {
    if (this.handlers.has(event)) {
      this.handlers.set(event, this.handlers.get(event).filter(cb => cb !== handler))
    }
  }

  _emit(event, data, raw) {
    const list = this.handlers.get(event) || []
    list.forEach(cb => { try { cb(data, raw) } catch (e) { console.error(e) } })
  }

  disconnect() {
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }
}

export const wsService = new WsService()