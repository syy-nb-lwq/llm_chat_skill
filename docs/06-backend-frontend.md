# Backend 与 Frontend

本文记录当前前后端通信和页面结构。

## 1. 后端入口

文件：`backend/main.py`

当前暴露两类接口：

- REST API
- WebSocket PubSub 路由 `/pubsub`

### 1.1 身份认证（C-01）

- 管理类写路由通过 `Depends(require_owner_token)` 校验 `Authorization: Bearer <owner_token>`
- `owner_token` 配置为空时放行（单机个人环境兼容）
- 详见 [07-infra.md §5](./07-infra.md#5-身份认证层c-01)

### 1.2 基础 API

- `GET /`
- `GET /api/health`
  - 返回 `status` / `self_evolution_enabled` / `tool_sources` 聚合 / `sources` 详情（M0-06）
  - 工具源失败时顶层 status 降级为 `degraded`
- `GET /api/features`
- `POST /api/features/self-evolution` 🔒
- `GET /api/diag`：诊断端点

### 1.3 技能 API

- `GET /api/skills`
- `DELETE /api/skills/{name}` 🔒
- `DELETE /api/skills/{name}/{version}` 🔒
- `POST /api/skills/reload` 🔒
- `POST /api/skills/{name}/rollback/{version}` 🔒（C-03 回滚）
- `GET /api/skills/{name}/versions`
- `GET /api/skills/{name}/audit`（C-03 审计）

### 1.4 教学 API（M1-01 / M1-08）

- `GET /api/teachings`：拉取草稿/当前问题/缺失字段/重复决策
- `POST /api/teachings/confirm` 🔒：确认发布草稿
- `POST /api/teachings/cancel` 🔒：取消教学
- `POST /api/teachings/choose` 🔒：重复技能决策（reuse / update_new / cancel）

### 1.5 工具 API

- `GET /api/tools`

注：M4 工具提案审批/发布目前通过 Python API（`ToolApprovalService`）暴露，无 REST 路由（见 [04-tools.md §8.7](./04-tools.md#87-e2e-闭环m4-06)）。

### 1.6 自演化 API

- `GET /api/patches`
- `POST /api/patches/{patch_id}/approve` 🔒
- `POST /api/patches/{patch_id}/reject` 🔒
- `POST /api/feedback` 🔒（M3-02 FeedbackEvent）
- `GET /api/feedback`
- `GET /api/memory`：列出当前用户记忆（M2-08）
- `DELETE /api/memory/{item_id}` 🔒（M2-08 删除单条）
- `DELETE /api/memory` 🔒（M2-08 forget_user 清空）
- `POST /api/memory/recall`
- `GET /api/episodes`
- `GET /api/episodes/{execution_id}`（按 execution_id 查执行链路）
- `GET /api/memory/stats`
- `GET /api/reflections`
- `POST /api/reflections/request`

> 🔒 标记表示路由挂了 `Depends(require_owner_token)`，共 13 个写管理路由。

## 2. WebSocket 协议

文件：

- `backend/websocket_handler.py`
- `frontend/src/websocket.js`

前端连接地址（M0-04 配置化）：

- 从 `import.meta.env.VITE_API_BASE` / `VITE_WS_BASE` 读取，dev / prod 可切换
- 代码中无 `localhost:8000` 硬编码

前端本地持久化：

- `localStorage['skill_agent_client_id']`（旧值，兼容保留）

前端发布主题：

- `init/{client_id}`
- `chat/{client_id}`
- `reset/{client_id}`
- `ping/{client_id}`

前端订阅主题：

- `events/{client_id}`
- `log/{client_id}`

### 2.1 身份签发（C-01）

`init/{client_id}` 由 `dispatcher()` 处理：

- 调用 `extract_ws_identity(data)` 提取/生成 `user_id` / `session_id`
- `client_id` 始终由服务端 `gen_client_id()` 签发，不再信任客户端提交
- 通过 `events/{client_id}` 推送 `connected` 事件，payload 包含：
  - `client_id`：前端订阅用的 id（兼容）
  - `server_client_id`：C-01 服务端签发，前端可选用
  - `user_id` / `session_id`

`chat/{client_id}` 同样走 `extract_ws_identity()`，确保每次对话有稳定身份。

## 3. 聊天链路

典型消息流：

1. 前端建立 WebSocket 连接
2. 订阅 `events/{client_id}` 与 `log/{client_id}`
3. 发布 `init/{client_id}`，收到 `connected` 事件拿到 `server_client_id` / `user_id` / `session_id`
4. 发布 `chat/{client_id}`（可带 `user_id`）
5. 后端创建或复用 `Session`
6. `Agent.handle()` 持续推送阶段事件（每个 event payload 带 `execution_id`）
7. 前端按事件更新界面

## 4. 前端页面

关键文件：

- `frontend/src/App.vue`
- `frontend/src/websocket.js`（导出 `WsService` 类供测试构造独立实例）
- `frontend/src/components/PatchReview.vue`
- `frontend/src/components/EvolutionDashboard.vue`
- `frontend/src/components/SkillManager.vue`（M1-08 教学草稿确认面板）

`EvolutionDashboard.vue` 当前已接入真实后端 API：

- 读取 feature flags
- 读取 memory stats
- 读取 reflections
- 请求即时 reflection
- 导出面板数据

`SkillManager.vue`（M1-08）：

- 展示教学草稿、能力边界、方法论、关键词、步骤、缺失字段、当前问题、重复决策
- 联调 `/api/teachings` GET/POST confirm/cancel/choose 三类后端接口
- 挂载与刷新时自动拉取草稿，按钮防抖

### 4.1 前端测试（M0-08）

引入 `vitest@1.6` + `@vue/test-utils@2.4` + `jsdom@24` 测试栈，3 个测试文件共 26 个用例：

- `tests/websocket.test.js`：WsService 纯逻辑 + RPC 协议
- `tests/PatchReview.test.js`：formatTime / formatSuggestion / confidenceClass + 挂载
- `tests/EvolutionDashboard.test.js`：formatTime / formatTrigger + 挂载

## 5. 补丁审批链路（M3-04 / M3-05 / C-03）

当前行为：

1. 前端拉取 `GET /api/patches`
2. 用户点击 approve / reject
3. 后端 `POST /api/patches/{patch_id}/approve`：
   - 跑旧样例 + 用户纠正样例回归（M3-05）
   - 生成 `regression_results` 与 `risk_summary`
   - 回归失败 → 阻止发布，返回原因
   - 通过 → 不原地改 YAML，生成新版本草稿（M3-04）
4. 技能库 reload，旧版本仍可查看
5. 审批记录：提案来源、模型提示版本、diff、测试结果、审批人、发布时间、被替代版本（C-03）
6. 回滚：`POST /api/skills/{name}/rollback/{version}` 🔒

## 6. 当前缺陷

- WebSocket 集成测试有 1 个用例仍 skip（`tests/test_backend.py:97`，TestClient + PubSub RPC + sandbox env 时序问题，已手动 curl 验证）
- 前端缺少统一状态管理，组件间状态靠 props 传递
- 前端无专门诊断页面（CLI `diagnose` 已覆盖命令行场景）
