# Backend 与 Frontend

本文记录当前前后端通信和页面结构。

## 1. 后端入口

文件：`backend/main.py`

当前暴露两类接口：

- REST API
- WebSocket PubSub 路由 `/pubsub`

基础 API：

- `GET /`
- `GET /api/health`
  - 返回 `status` / `self_evolution_enabled` / `tool_sources` 聚合 / `sources` 详情（M0-06）
  - 工具源失败时顶层 status 降级为 `degraded`
- `GET /api/features`
- `POST /api/features/self-evolution`

技能 API：

- `GET /api/skills`
- `DELETE /api/skills/{name}`
- `DELETE /api/skills/{name}/{version}`
- `POST /api/skills/reload`

工具 API：

- `GET /api/tools`

自演化 API：

- `GET /api/patches`
- `POST /api/patches/{patch_id}/approve`
- `POST /api/patches/{patch_id}/reject`
- `GET /api/memory/stats`
- `GET /api/reflections`
- `POST /api/reflections/request`

## 2. WebSocket 协议

文件：

- `backend/websocket_handler.py`
- `frontend/src/websocket.js`

前端连接地址：

- `ws://localhost:8000/pubsub`

前端本地持久化：

- `localStorage['skill_agent_client_id']`

前端发布主题：

- `init/{client_id}`
- `chat/{client_id}`
- `reset/{client_id}`
- `ping/{client_id}`

前端订阅主题：

- `events/{client_id}`
- `log/{client_id}`

### 会话与切换

会话 = 一个 `session_id`，内容全量存在 `memory/sessions.db`（见 docs/13）。
前端在 `localStorage['skill_agent_session']` 里记着当前用哪个，刷新/重连时带回，
所以**重启后端对话不会丢**。

换会话不需要重连 WebSocket —— 对同一个连接重发一次 `init` 就够了：

| 前端动作 | 发什么 | 服务端做什么 |
|---|---|---|
| 开新对话 / `＋` 按钮 | `init/{conn}`，`session_id: ""` | `gen_session_id()` 签发新会话 → 新建 Agent |
| 点列表里的某条 | `init/{conn}`，`session_id: "<目标>"` | `get_or_create` 复用（或新建）该会话的 Agent |

每次 `init` 都会签发**新的** `client_id`，并把 `conn_id → client_id` 的映射覆盖掉，
所以后续 `chat/{conn}` 打到的就是新会话（`tests/test_c01_session_identity.py`
里 `test_reinit_same_conn_switches_session` 钉着这个行为）。

列表数据：

- `GET /api/sessions` —— 新→旧，每条带 `title`（后端从 `reset_at_seq` 之后的第一条
  用户消息现算，压成一行、截断 36 字）、`message_count`、`updated_at`
- `DELETE /api/sessions/{sid}` —— 连带消息一起删，不可恢复

界面上的两条约束：

1. **一轮回答正在流式输出时禁止切换/新建**。事件推送是按连接走的
   （`events/{conn_id}`），旧那轮的 `message_delta` 会飘进新会话的界面。
2. 刚新建、还没说过话的会话在库里**没有行**（`ensure_session` 要等第一条消息），
   所以 `SessionList.vue` 会把当前 `session_id` 补成一条「刚创建」的占位项，
   否则用户点完新建在列表里找不到自己刚开的那条。

## 3. 聊天链路

典型消息流：

1. 前端建立 WebSocket 连接
2. 订阅 `events/{client_id}` 与 `log/{client_id}`
3. 发布 `init/{client_id}`
4. 发布 `chat/{client_id}`
5. 后端创建或复用 `Session`
6. `Agent.handle()` 持续推送阶段事件
7. 前端按事件更新界面

## 4. 前端页面

关键文件：

- `frontend/src/App.vue` —— 页面骨架、事件总线接线、会话切换
- `frontend/src/websocket.js` —— 连接、RPC、`switchSession`
- `frontend/src/api/client.js` —— 唯一的 HTTP 出口（超时/错误结构/具名接口）
- `frontend/src/config.js` —— 只回答"后端在哪"（`VITE_API_BASE` / `VITE_WS_BASE`）
- `frontend/src/components/ChatPanel.vue` —— 对话区
- `frontend/src/components/SessionList.vue` —— 会话列表（侧栏「💬 对话」）
- `frontend/src/components/PatchReview.vue`
- `frontend/src/components/EvolutionDashboard.vue`

`EvolutionDashboard.vue` 当前已接入真实后端 API：

- 读取 feature flags
- 读取 memory stats
- 读取 reflections
- 请求即时 reflection
- 导出面板数据

这已经替代了旧版仅靠浏览器本地状态模拟的实现。

## 5. 补丁审批链路

当前行为：

1. 前端拉取 `GET /api/patches`
2. 用户点击 approve / reject
3. 后端更新 patch 状态
4. approve 时尝试把 `suggestion.method` 写回技能 YAML
5. 技能库 reload

## 6. 当前缺陷

- WebSocket 仍没有稳定的端到端测试覆盖（`scripts/e2e_switch_session.py` 是手测脚本，不进 CI）
- 前端 API base URL 的默认值仍是 `http://localhost:8000`（可用 `VITE_API_BASE` 覆盖，但没做同源相对路径）
- 前端组件存在编码问题，源码可读性差
- 前端缺少统一状态管理和自动化测试
- 切会话时旧那轮的 `Session` 还留在内存里（按 TTL 回收），期间没有"取消这一轮"的手段
