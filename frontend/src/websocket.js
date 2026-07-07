// WebSocket 服务
class WsService {
  constructor() {
    this.ws = null
    this.callbacks = {}
    this.reconnectAttempts = 0
    this.maxReconnectAttempts = 5
    this.reconnectDelay = 3000
    this.url = ''
  }

  connect(url = 'ws://localhost:8000/ws/chat') {
    this.url = url
    
    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.url)
        
        this.ws.onopen = () => {
          console.log('WebSocket connected')
          this.reconnectAttempts = 0
          this.emit('connected', { message: '连接成功' })
          resolve()
        }
        
        this.ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data)
            console.log('Received:', data)
            this.emit(data.type, data)
          } catch (e) {
            console.error('Failed to parse message:', e)
          }
        }
        
        this.ws.onerror = (error) => {
          console.error('WebSocket error:', error)
          this.emit('error', { message: '连接错误' })
          reject(error)
        }
        
        this.ws.onclose = () => {
          console.log('WebSocket closed')
          this.emit('disconnected', { message: '连接已断开' })
          this.attemptReconnect()
        }
        
      } catch (error) {
        reject(error)
      }
    })
  }

  attemptReconnect() {
    if (this.reconnectAttempts < this.maxReconnectAttempts) {
      this.reconnectAttempts++
      console.log(`尝试重连 (${this.reconnectAttempts}/${this.maxReconnectAttempts})...`)
      setTimeout(() => this.connect(this.url), this.reconnectDelay)
    }
  }

  send(type, content) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, content }))
    } else {
      console.error('WebSocket not connected')
    }
  }

  chat(message) {
    this.send('chat', message)
  }

  reset() {
    this.send('reset', '')
  }

  disconnect() {
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
  }

  on(event, callback) {
    if (!this.callbacks[event]) {
      this.callbacks[event] = []
    }
    this.callbacks[event].push(callback)
  }

  off(event, callback) {
    if (this.callbacks[event]) {
      this.callbacks[event] = this.callbacks[event].filter(cb => cb !== callback)
    }
  }

  emit(event, data) {
    if (this.callbacks[event]) {
      this.callbacks[event].forEach(callback => callback(data))
    }
  }
}

// 导出单例
export const wsService = new WsService()
