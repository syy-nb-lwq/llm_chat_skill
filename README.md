# Skill Agent

一个基于 FastAPI + Vue 3 的多 Agent 编排项目。核心目标不是单纯“调用模型”，而是把任务拆成：

- `ManagerAgent` 负责识别意图、选择技能、规划工具
- `LearningAgent` 负责执行工具与 DAG
- `OrchestratorAgent` 负责整合工具结果并生成最终回答

当前日期基线：2026 年 7 月 16 日。

## 当前状态

- 主链路可用：`FastAPI + WebSocket + Vue` 已打通
- `Streamlit / CLI` 入口可用：通过同步 `Agent.chat()` 适配层调用异步主链路
- 技能支持 YAML 持久化、版本化删除、按名称删除、审批后更新
- 工具层已统一到 `ToolHub`
- `weather_query` 与 `web_search` 都是异步实现
- 自演化面板已接入真实后端接口，不再依赖前端本地假状态

测试状态：

- `211 passed, 1 skipped`

## 快速启动

### 1. 本地启动

```bash
pip install -r requirements.txt
copy .env.example .env
```

编辑 `.env`，至少配置：

```env
OPENAI_API_KEY=your-key
```

启动后端：

```bash
python backend/main.py
```

启动前端：

```bash
cd frontend
npm install
npm run dev
```

默认地址：

- 后端：`http://localhost:8000`
- 前端：`http://localhost:3000`

### 2. 其他入口

```bash
streamlit run ui/app.py
python ui/cli.py
```

## 核心架构

### 后端接入层

- `backend/main.py`
  - REST API
  - WebSocket / PubSub 路由
  - 启动时配置校验、Provider 初始化、ToolHub 初始化
  - 关闭时后台任务和工具资源回收
- `backend/websocket_handler.py`
  - `init/chat/reset/ping` 事件分发
- `backend/session.py`
  - 按 `client_id` 管理会话
  - 支持异步 dispose 回调

### 编排层

- `core/agent.py`
  - 聚合 Manager / Learning / Orchestrator / DAG / Critic
  - `handle()` 是异步主入口
  - `chat()` 是同步适配入口
- `agents/manager.py`
  - 规则优先意图识别
  - LLM 兜底规划
- `agents/learning.py`
  - DAG 执行
  - 重试、超时、变量替换
- `agents/orchestrator.py`
  - 流式响应
  - 上下文整合

### 能力层

- `skills/`
  - 技能加载、匹配、版本管理、持久化更新
- `tools/`
  - `weather_query`
  - `web_search`
  - `ToolHub` 统一管理与关闭资源

### 前端

- `frontend/src/App.vue`
  - 聊天、流程、技能、补丁、进化面板整合
- `frontend/src/websocket.js`
  - `clientId` 持久化
  - 自动重连
- `frontend/src/components/EvolutionDashboard.vue`
  - 调用真实 API：
    - `GET /api/features`
    - `POST /api/features/self-evolution`
    - `GET /api/reflections`
    - `POST /api/reflections/request`

## 主要 API

### 基础

- `GET /`
- `GET /api/health`
- `GET /api/features`
- `POST /api/features/self-evolution`

### 技能

- `GET /api/skills`
- `DELETE /api/skills/{name}`
- `DELETE /api/skills/{name}/{version}`
- `POST /api/skills/reload`

### 工具

- `GET /api/tools`

### 自演化

- `GET /api/patches`
- `POST /api/patches/{patch_id}/approve`
- `POST /api/patches/{patch_id}/reject`
- `GET /api/memory/stats`
- `GET /api/reflections`
- `POST /api/reflections/request`

## 这轮已修复的问题

- `Streamlit / CLI` 调用不存在的 `Agent.chat()` 导致入口失效
- 启动配置校验只记日志不 fail-fast
- 补丁审批只改内存、不落盘
- 技能删除直接操作 `_registry` 内部结构
- 自演化面板只用 `localStorage` 假装切开关
- `web_search` 内部使用同步请求阻塞事件循环
- 工具关闭路径缺失

## 开发命令

运行测试：

```bash
pytest -q
```

局部语法检查：

```bash
python -m compileall backend agents core skills tools ui infra
```

## 目录概览

```text
backend/    FastAPI、Session、WebSocket 分发
frontend/   Vue 3 前端
agents/     Manager / Learning / Orchestrator / Trainer
core/       Agent、DAG、Context、Critic、Memory
skills/     技能模型、加载、注册、存储
tools/      工具实现与 ToolHub
infra/      配置、日志、LLM Provider
ui/         Streamlit / CLI
tests/      pytest
docs/       设计文档
```

## 说明

- `README.md` 和 `ARCHITECTURE.md` 已按 2026-07-16 当前实现更新
- `docs/` 下更细的分模块文档仍可继续细化
