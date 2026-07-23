// M0-08: WebSocket service 最小回归测试
// 仅覆盖纯逻辑 (事件分发 / 客户端 ID 持久化 / RPC 生成),
// 不连接真实后端 —— WebSocket 构造函数通过 stub 替换。

import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

// jsdom 提供 localStorage
beforeEach(() => {
  localStorage.clear()
})

describe('wsService 纯逻辑', () => {
  it('on/off + _emit: 处理器按事件分发并接收数据', async () => {
    // 动态导入以避免模块顶层副作用(构造时读 localStorage)
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()

    const calls = []
    const h1 = (data) => calls.push(['h1', data])
    const h2 = (data) => calls.push(['h2', data])

    svc.on('connected', h1)
    svc.on('connected', h2)
    svc._emit('connected', { ok: true })

    expect(calls).toEqual([
      ['h1', { ok: true }],
      ['h2', { ok: true }],
    ])

    svc.off('connected', h1)
    svc._emit('connected', { again: 1 })
    expect(calls).toHaveLength(3)
    expect(calls[2]).toEqual(['h2', { again: 1 }])
  })

  it('_genUid: 生成 UUID v4 形状(36 字符,含 4 个 -)', async () => {
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()
    const id = svc._genUid()
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })

  it('_loadOrCreateClientId: 首次生成并持久化,二次复用', async () => {
    const mod = await import('../src/websocket.js')
    const svc1 = new mod.WsService()
    const id1 = svc1.clientId
    expect(id1).toBeTruthy()
    expect(localStorage.getItem('skill_agent_client_id')).toBe(id1)

    // 新实例应读取同一 id
    const svc2 = new mod.WsService()
    expect(svc2.clientId).toBe(id1)
  })

  it('disconnect: 标记手动关闭且不再重连', async () => {
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()

    // disconnect 在没有 ws 时也应安全
    expect(() => svc.disconnect()).not.toThrow()
    expect(svc._isManualClose).toBe(true)
    expect(svc.reconnectAttempts).toBe(svc.maxReconnectAttempts)
    expect(svc.connected).toBe(false)
  })
})

describe('wsService RPC 协议封装', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('_call: 未连接时 reject', async () => {
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()
    svc.ws = null
    await expect(svc._call('noop', {})).rejects.toThrow(/not connected/i)
  })

  it('_call: 通过 ws.send 发出 RPC 请求帧(call_id + method + args)', async () => {
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()

    const sent = []
    svc.ws = {
      readyState: 1, // OPEN
      send: (raw) => sent.push(JSON.parse(raw)),
    }

    const p = svc._call('subscribe', { topics: ['events/x'] }, 1000)
    // 给微任务一个 tick,确保 send 已被同步调用
    await Promise.resolve()
    expect(sent).toHaveLength(1)
    expect(sent[0].request.method).toBe('subscribe')
    expect(sent[0].request.arguments).toEqual({ topics: ['events/x'] })
    expect(sent[0].request.call_id).toMatch(/^[0-9a-f-]{36}$/)

    // 模拟服务端返回 response(直接 resolve pending call)
    const callId = sent[0].request.call_id
    svc._pendingCalls.get(callId)?.resolve({ ok: true })
    await expect(p).resolves.toEqual({ ok: true })
  })
})

describe('wsService 公开 API', () => {
  it('chat/reset/ping 通过 _publish 派发对应 topic', async () => {
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()
    svc.clientId = 'test-cid'

    const published = []
    svc._publish = (topic, data) => published.push({ topic, data })

    svc.chat('hello')
    svc.reset()
    svc.ping()

    expect(published).toEqual([
      { topic: 'chat/test-cid', data: { content: 'hello' } },
      { topic: 'reset/test-cid', data: {} },
      { topic: 'ping/test-cid', data: {} },
    ])
  })

  it('_publish: 未连接时压入 pendingMessages', async () => {
    const mod = await import('../src/websocket.js')
    const svc = new mod.WsService()
    svc.ws = null
    svc._publish('chat/x', { content: 'queued' })
    expect(svc.pendingMessages).toHaveLength(1)
    expect(svc.pendingMessages[0]).toEqual({ topic: 'chat/x', data: { content: 'queued' } })
  })
})
