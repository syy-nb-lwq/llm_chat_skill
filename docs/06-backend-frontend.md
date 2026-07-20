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

- `frontend/src/App.vue`
- `frontend/src/websocket.js`
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

- WebSocket 仍没有稳定的端到端测试覆盖
- 前端 API base URL 仍是硬编码 `http://localhost:8000`
- 前端组件存在编码问题，源码可读性差
- 前端缺少统一状态管理和自动化测试
