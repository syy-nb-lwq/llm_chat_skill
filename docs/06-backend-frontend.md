# 06 — Backend / Frontend 通信与可视化设计

> 本文档描述 FastAPI 后端 + Vue 前端的通信协议、改进后的事件模型、以及前端"流转可视化"的设计。

---

## 1. 现状

### 1.1 后端 ([backend/main.py](../backend/main.py))

- FastAPI + CORS(允许所有 origin)
- WebSocket 路由 `/ws/chat`
- `client_id = str(id(websocket))` —— 用对象 id 作标识
- 日志订阅 `try` 加 / `finally` 删(但 `except` 路径只 `disconnect`,未 `unsubscribe`)
- 通过 `ThreadPoolExecutor` + `loop.run_in_executor` 跑同步的 `agent.chat`
- 推送的消息类型:`connected` / `thinking` / `step` / `log` / `guidance_request` / `response` / `error` / `reset` / `logs`

### 1.2 前端 ([frontend/src/App.vue](../frontend/src/App.vue))

- 简单聊天 + 侧栏(工具列表 / 技能列表 / 流程步骤)
- `WsService` 自己实现,有 5 次重连
- 通过 `ws.on(type, ...)` 注册回调
- 流程步骤是**字符串列表**,无法看到 DAG 结构、工具耗时、参数等

### 1.3 主要问题

| # | 问题 | 后果 |
|---|---|---|
| B1 | `step` 和 `log` 消息结构不一致 | 前端两套解析 |
| B2 | `client_id` 用对象 id | 重启即失效,无法恢复 Session |
| B3 | `logger.subscribe` 在 `except` 分支未 `unsubscribe` | 内存泄漏 |
| B4 | `sync_send_step` 用 `asyncio.ensure_future` 嵌套 | 易丢消息 |
| B5 | LLM 同步调用,前端无流式体验 | 等待时间长 |
| B6 | 流程面板只显示"思考中..."等字符串 | 看不出 DAG / 工具调用 / 耗时 |
| B7 | REST `/api/skills` 后端 `reset_skill_store()` 每次都重置 | 不必要的全局副作用 |
| B8 | 无重连后的"恢复上下文"机制 | 断网 = 失忆 |

---

## 2. 改进后的 WebSocket 协议

### 2.1 统一消息格式

**所有消息都长这样**:

```jsonc
{
  "type":     "event",                  // 固定,前端据此判断协议版本
  "event":    "<event_name>",           // 见下表
  "trace_id": "trace-20260707-...",     // 用于串联一整次请求
  "turn_id":  "turn-...",               // 同一请求内的多次步骤
  "ts":       "2026-07-07T10:30:00.123",
  "payload":  { ... }                   // 事件相关数据
}
```

### 2.2 事件清单

| `event` | 触发时机 | payload 关键字段 |
|---|---|---|
| `connected` | WebSocket 握手成功 | `client_id` |
| `user_message` | 收到用户消息 | `content` |
| `thinking` | 阶段开始 | `stage`: `direct_answer` / `planning` / `synthesizing` / `tool_<id>` |
| `plan` | Manager 完成规划 | `intent`, `skill`(name+version), `tasks[]`(id, tool, params) |
| `skill_learned` | SkillTrainer 沉淀技能 | `name`, `version`, `path` |
| `capability_gap` | CapabilityAnalyzer 报告缺口 | `needed`, `gap`, `suggestions` |
| `tool_call` | 工具开始 | `task_id`, `tool`, `params` |
| `tool_result` | 工具成功 | `task_id`, `tool`, `data`, `meta`(耗时/来源) |
| `tool_error` | 工具失败 | `task_id`, `tool`, `error`, `attempt` |
| `tool_retry` | 工具重试 | `task_id`, `attempt`, `reason` |
| `message_delta` | LLM 流式输出 | `delta` |
| `message_final` | 一轮回答结束 | `content`, `usage`(可选) |
| `error` | 整次请求失败 | `message`, `code` |
| `log` | 日志条目(用于调试面板) | `LogEntry` |
| `reset_ack` | 重置确认 | `client_id` |

### 2.3 客户端 → 服务端

```jsonc
// 建立连接后,客户端立即发:
{ "type": "init", "client_id": "uuid4-..." }

// 聊天
{ "type": "chat", "content": "厦门明天怎么玩" }

// 重置(可选)
{ "type": "reset" }

// 获取技能列表(走 REST 即可,不需要 WS)
```

**`init` 是关键**:服务端用 `client_id` 查/建 Session,实现断线恢复。

---

## 3. 后端改进

### 3.1 Session 管理

```python
# backend/session.py
from dataclasses import dataclass
from core.agent import Agent

@dataclass
class Session:
    client_id: str
    agent: Agent
    created_at: float
    last_active: float

class SessionManager:
    def __init__(self, ttl_s: int = 3600):
        self._sessions: Dict[str, Session] = {}
        self.ttl_s = ttl_s

    def get_or_create(self, client_id: str) -> Session:
        sess = self._sessions.get(client_id)
        if sess:
            sess.last_active = time.time()
            return sess
        sess = Session(
            client_id=client_id,
            agent=Agent(session_id=client_id),
            created_at=time.time(),
            last_active=time.time(),
        )
        self._sessions[client_id] = sess
        return sess

    def gc(self):
        """后台任务定期清理超时会话。"""
        now = time.time()
        expired = [cid for cid, s in self._sessions.items()
                   if now - s.last_active > self.ttl_s]
        for cid in expired:
            self._sessions.pop(cid, None)

sessions = SessionManager()
```

### 3.2 WebSocket 路由重写

```python
# backend/main.py
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    client_id: Optional[str] = None
    session: Optional[Session] = None
    push_queue: asyncio.Queue = asyncio.Queue()

    async def push(event: str, payload: dict):
        await push_queue.put({
            "type": "event",
            "event": event,
            "trace_id": session.agent.trace.trace_id if session else "",
            "ts": datetime.now().isoformat(),
            "payload": payload,
        })

    async def sender():
        while True:
            msg = await push_queue.get()
            try:
                await ws.send_json(msg)
            except Exception:
                break

    sender_task = asyncio.create_task(sender())

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "init":
                client_id = msg["client_id"]
                session = sessions.get_or_create(client_id)
                await push("connected", {"client_id": client_id})

            elif msg.get("type") == "chat":
                if not session:
                    await push("error", {"message": "请先 init"})
                    continue
                await session.agent.handle(msg["content"], push)

            elif msg.get("type") == "reset":
                if session:
                    session.agent.context.clear()
                await push("reset_ack", {"client_id": client_id})

    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        # Session 不删除,允许重连恢复
```

### 3.3 流式 LLM 调用

`Agent.handle` 内部用 `async for delta in orchestrator.stream(...)` 逐 token 推送 `message_delta`,通过 `push_queue` 入队。`sender` 协程负责把队列消息发出。

---

## 4. 前端改进

### 4.1 重构后的页面布局

```
┌────────────────────────────────────────────────────────────────────┐
│  📚 Skill Agent                            🟢 已连接  client_id:.. │
├──────────────┬─────────────────────────────────────┬───────────────┤
│              │                                     │               │
│   工具列表   │            聊天主区域               │  流转可视化   │
│  ──────────  │  ──────────────────────────────    │  ───────────  │
│  🔧 tools    │   user:  厦门明天怎么玩             │  ●─ 直接回答?  │
│              │                                     │  │            │
│  已学技能    │   assistant:                        │  ●─ 规划       │
│  ──────────  │     [流式输出]                      │  │ ├ 天气 ✗    │
│  📘 travel   │                                     │  │ └ 搜索 ✓   │
│  📘 summary  │                                     │  ●─ 整合       │
│              │                                     │  └ 完成 ✓     │
├──────────────┴─────────────────────────────────────┴───────────────┤
│  [输入框........................] [发送] [↺ 重置] [设置]            │
└────────────────────────────────────────────────────────────────────┘
```

### 4.2 流转可视化面板(`FlowPanel.vue`)

把后端推来的事件流聚合成**树形 / 时间轴**:

```js
// 前端状态
state = {
  turns: [
    {
      trace_id,
      steps: [
        { stage: 'thinking', text: '...', at: ts },
        { stage: 'plan', skill: 'travel_plan', tasks: [...] },
        { stage: 'tool_call', tool: 'weather_query', params: {...}, status: 'running' },
        { stage: 'tool_result', tool: 'weather_query', data: {...}, duration_ms: 230 },
        ...
      ]
    }
  ]
}
```

组件按 `trace_id` 隔离每轮,可展开/收起。

### 4.3 消息渲染

- `messages[]` 仍然是 user / assistant 列表,流式输出时把 `delta` 拼接到最后一条 assistant 的 content
- `markdown-it` 已有,可继续用

### 4.4 WebSocket 客户端重写

把现在的事件订阅改为 **EventBus 风格**,并支持重连时恢复:

```js
class WsService {
  constructor() {
    this.clientId = this._loadOrCreateClientId()  // localStorage 持久
    this.handlers = new Map()
    this.ws = null
    this.state = 'idle'
  }

  _loadOrCreateClientId() {
    let cid = localStorage.getItem('skill_agent_client_id')
    if (!cid) {
      cid = crypto.randomUUID()
      localStorage.setItem('skill_agent_client_id', cid)
    }
    return cid
  }

  connect(url = 'ws://localhost:8000/ws/chat') {
    this.ws = new WebSocket(url)
    this.ws.onopen = () => {
      this.send({ type: 'init', client_id: this.clientId })
      this._emit('connected')
    }
    this.ws.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.type === 'event') {
        this._emit(msg.event, msg.payload, msg)
      }
    }
    this.ws.onclose = () => {
      this._emit('disconnected')
      setTimeout(() => this.connect(url), 3000)   // 无限重连
    }
  }

  on(event, handler) {
    if (!this.handlers.has(event)) this.handlers.set(event, [])
    this.handlers.get(event).push(handler)
  }

  chat(text) { this.send({ type: 'chat', content: text }) }
  reset()    { this.send({ type: 'reset' }) }

  send(obj) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj))
    } else {
      this._outbox.push(obj)   // 排队,等 onopen 后发出
    }
  }
}
```

要点:
- `clientId` 用 `localStorage` 持久化,刷新页面 / 重启浏览器都能复用
- 自动重连,**消息不会丢**(因为 Session 在服务端)
- 离线时调用 `chat()` 进 outbox,重连后批量发送(可选)

### 4.5 新增组件

| 组件 | 职责 |
|---|---|
| `ChatPanel.vue` | 主聊天区,展示 messages + 输入框 |
| `ToolList.vue` | 侧栏工具列表(从 `/api/tools`) |
| `SkillList.vue` | 侧栏技能列表(从 `/api/skills`) |
| `FlowPanel.vue` | 流转可视化(树形 + 耗时) |
| `StepItem.vue` | 单个步骤(可展开查看 params / data) |

---

## 5. REST 接口

```python
@app.get("/api/skills")
async def list_skills():
    """返回所有技能 + 元信息 + 步骤 DAG(用于前端可视化)"""
    skills = skill_registry.all()
    return {"skills": [s.to_dict() for s in skills]}

@app.get("/api/tools")
async def list_tools():
    return {"tools": [t.schema().model_dump() for t in tool_registry.all()]}

@app.get("/api/sessions/{client_id}/trace")
async def get_session_trace(client_id: str):
    """获取会话最近的 trace,用于事后回放"""
    ...

@app.get("/api/health")
async def health():
    return {"status": "ok", "tools": len(tool_registry), "skills": len(skill_registry)}
```

---

## 6. 关键改进 checklist

### 后端

- [ ] 引入 `SessionManager`,改 `client_id` 为 UUID4
- [ ] 改消息协议为统一 `event` 格式
- [ ] 重写 WebSocket 路由为 async + queue 推送
- [ ] `unsubscribe` 移到 `finally`
- [ ] LLM 流式调用
- [ ] Session TTL + 后台 GC

### 前端

- [ ] 拆组件:`ChatPanel` / `ToolList` / `SkillList` / `FlowPanel`
- [ ] `clientId` 持久化到 localStorage
- [ ] 重连自动续,离线消息缓存(可选)
- [ ] 流转面板支持展开参数 / 数据 / 耗时
- [ ] 端到端可视化:用户输入 → 规划 → DAG 步骤 → 工具结果 → 最终回答

---

## 7. 测试要点

| 测试 | 内容 |
|---|---|
| WebSocket 协议 | 所有 event 类型 JSON 结构正确 |
| SessionManager | 同 client_id 复用 / TTL 过期清理 |
| 流式输出 | 多个 `message_delta` 顺序拼接 |
| 重连恢复 | 断开重连后 Session 仍保留 |
| 前端事件聚合 | 同一 trace_id 的事件聚合到同一树 |