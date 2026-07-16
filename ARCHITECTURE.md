# 架构设计

本文档描述 2026 年 7 月 16 日当前仓库的实际架构，而不是历史设计草案。

## 1. 总览

系统按四层组织：

1. 接入层
   - FastAPI REST
   - WebSocket / PubSub
   - Streamlit / CLI 适配入口
2. 编排层
   - `Agent`
   - `ManagerAgent`
   - `LearningAgent`
   - `OrchestratorAgent`
3. 能力层
   - `skills/`
   - `tools/`
4. 基础设施层
   - 配置
   - LLM Provider
   - 日志
   - Memory / Critic / Reflect

## 2. 运行主链路

一次典型请求的执行路径：

```text
Frontend / CLI / Streamlit
  -> backend/websocket_handler.py or direct Agent.chat()
  -> core/agent.py
  -> agents/manager.py
  -> agents/learning.py
  -> agents/orchestrator.py
  -> output
```

### 2.1 Web 入口

- 前端通过 `frontend/src/websocket.js` 与 `/pubsub` 建立连接
- `clientId` 保存在浏览器 `localStorage`
- 后端在 `backend/websocket_handler.py` 分发：
  - `init/{client_id}`
  - `chat/{client_id}`
  - `reset/{client_id}`
  - `ping/{client_id}`

### 2.2 会话层

- `backend/session.py` 按 `client_id` 持有 `Session`
- 每个 `Session` 内含一个 `Agent`
- 支持 TTL GC
- dispose 回调现在支持异步 await

### 2.3 Agent 层

`core/agent.py` 是统一入口：

- `handle()`：异步主入口
- `chat()`：同步适配入口，供 `CLI` / `Streamlit` 使用

内部聚合：

- `ManagerAgent`
- `LearningAgent`
- `OrchestratorAgent`
- `DAGExecutor`
- `SkillTrainer`
- `ExecutionCritic`

## 3. 三个 Agent 的职责

### 3.1 ManagerAgent

位置：`agents/manager.py`

职责：

- 规则优先意图识别
- 必要时用 LLM 兜底规划
- 选择技能
- 生成工具任务

输入：

- 用户输入
- `Context`
- 历史 hints / memory

输出：

- `PlanResult`

### 3.2 LearningAgent

位置：`agents/learning.py`

职责：

- 调用工具
- 按 DAG 调度任务
- 处理重试、超时、依赖、变量替换

关键能力：

- `${user_input.xxx}`
- `${task.data.xxx}`
- fallback / retry / timeout

### 3.3 OrchestratorAgent

位置：`agents/orchestrator.py`

职责：

- 整合工具结果
- 结合技能方法论组织答案
- 输出流式响应
- 注入近期对话上下文

## 4. 技能层

位置：`skills/`

核心对象：

- `Skill`
- `SkillStep`
- `SkillLoader`
- `SkillRegistry`
- `SkillStore`

当前实现特点：

- 技能可从 YAML / MD 加载
- 支持名称级删除
- 支持版本级删除
- 支持审批补丁后持久化更新字段

## 5. 工具层

位置：`tools/`

当前内置工具：

- `weather_query`
- `web_search`

### 5.1 ToolHub

位置：`tools/hub.py`

职责：

- 统一注册 Python 工具与外部来源工具
- 提供统一调用接口
- 负责工具关闭与资源释放

### 5.2 weather_query

位置：`tools/weather.py`

- 使用 `httpx.AsyncClient`
- 通过 `aclose()` 释放共享客户端

### 5.3 web_search

位置：`tools/search.py`

- 已改为真正异步实现
- 当前后端为：
  - SearXNG
  - Wikipedia

## 6. 自演化与记忆

相关位置：

- `core/memory.py`
- `core/critic.py`
- `core/reflect.py`
- `frontend/src/components/PatchReview.vue`
- `frontend/src/components/EvolutionDashboard.vue`

当前能力：

- 记录 success / failure
- 生成待审 patch
- 审批 patch 后将更新写回 skill YAML
- 前端进化面板调用真实后端接口，而不是本地假数据

相关 API：

- `GET /api/features`
- `POST /api/features/self-evolution`
- `GET /api/patches`
- `POST /api/patches/{id}/approve`
- `POST /api/patches/{id}/reject`
- `GET /api/memory/stats`
- `GET /api/reflections`
- `POST /api/reflections/request`

## 7. 启动与关闭

入口：`backend/main.py`

启动阶段：

- `config.validate()` fail-fast
- 初始化 providers
- 初始化 ToolHub
- 启动 session GC
- 按配置决定是否启动反思循环

关闭阶段：

- 停止 GC task
- 停止 reflect loop
- `ToolHub.disconnect_all()`，关闭工具资源

## 8. 当前设计取舍

### 已完成的收口

- 同步入口与异步主链路统一
- 配置启动失败直接中止
- 搜索工具不再阻塞事件循环
- 补丁审批从“内存修改”改为“文件持久化修改”
- 自演化面板走真实 API

### 仍然存在的限制

- `docs/` 下的细分设计文档仍有部分历史描述，需继续同步
- `frontend` 没有端到端自动化测试
- WebSocket 集成测试仍保留 1 个 skip，用例依赖 sandbox 下的不稳定时序

## 9. 目录摘要

```text
backend/      FastAPI、Session、WebSocket
frontend/     Vue 3 前端
agents/       三个业务 Agent + Trainer
core/         Agent、Context、DAG、Memory、Critic、Reflect
skills/       技能模型、加载、注册、存储
tools/        工具实现与 ToolHub
infra/        配置、Provider、日志
ui/           Streamlit / CLI
tests/        自动化测试
docs/         细分设计文档
```
