# 项目架构设计

> 详细设计已迁移到 [`docs/`](docs/) 目录。本文件保留**概念级概览**,作为快速参考。
>
> 详细模块设计与改进路线:
> - [docs/00-总览.md](docs/00-总览.md)
> - [docs/01-架构设计.md](docs/01-架构设计.md)
> - [docs/02-agents.md](docs/02-agents.md) · [03-skills.md](docs/03-skills.md) · [04-tools.md](docs/04-tools.md)
> - [docs/05-core.md](docs/05-core.md) · [06-backend-frontend.md](docs/06-backend-frontend.md) · [07-infra.md](docs/07-infra.md)
> - [docs/08-roadmap.md](docs/08-roadmap.md) (含 P6 深化清单)

## 核心理念

**智能体 = 流转中枢**

每个智能体负责协调流转:判断用哪个技能,调用什么工具。

```
┌─────────────────────────────────────────────────────────────────┐
│                     智能体 = 流转中枢                              │
│                                                               │
│   技能 (Skill) = 完成任务的方法论/流程                          │
│   工具 (Tool)  = 获取数据、执行具体操作                          │
└─────────────────────────────────────────────────────────────────┘
```

## 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         Frontend (Vue 3)                         │
│                    http://localhost:3000                         │
└─────────────────────────────────────────────────────────────────┘
                              ↕ WebSocket
┌─────────────────────────────────────────────────────────────────┐
│                         Backend (FastAPI)                        │
│                    http://localhost:8000                         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       Core Agent                                  │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              Manager Agent (流转中枢)                      │  │
│  │  意图识别 → 选择技能 → 规划工具                          │  │
│  └────────────────────────────┬────────────────────────────┘  │
│                               ↓                                 │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              Learning Agent (流转中枢)                   │  │
│  │  执行工具 → 获取数据                                    │  │
│  └────────────────────────────┬────────────────────────────┘  │
│                               ↓                                 │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │              Orchestrator Agent (流转中枢)                │  │
│  │  整合数据 → 按技能方法论生成回答                         │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  + SkillTrainer (教导闭环)                                    │
│  + DAGExecutor (技能步骤拓扑执行)                             │
└─────────────────────────────────────────────────────────────────┘
```

## 目录结构

```
langchain_functioncall/
│
├── backend/                    # 后端服务
│   ├── main.py                 # FastAPI + WebSocket 入口
│   ├── session.py              # SessionManager + TTL GC + dispose 钩子
│   └── websocket_handler.py     # PubSub dispatcher
│
├── frontend/                   # 前端 (Vue 3)
│   ├── src/
│   │   ├── App.vue             # 主入口,聚合所有 WS 事件
│   │   ├── websocket.js        # WS 客户端(clientId 持久化 + 重连)
│   │   └── components/
│   │       ├── ChatPanel.vue   # 聊天区 + 流式打字机
│   │       ├── FlowPanel.vue   # 流转时间轴
│   │       ├── ToolList.vue    # 工具列表
│   │       ├── SkillList.vue   # 技能列表(简版)
│   │       └── SkillManager.vue # 技能管理(版本/删除/toast)
│   ├── package.json
│   └── vite.config.js
│
├── core/                       # 核心基础设施
│   ├── agent.py                # Agent 主类(异步,emit 顺序保证)
│   ├── agent_base.py           # BaseAgent(LLM 调用 + JSON 解析 + 重试)
│   ├── dag.py                  # DAGExecutor(SkillStep → ToolTask)
│   ├── context.py              # 多轮对话 Context(to_llm_messages 压缩)
│   └── capability.py           # Capability 分析器
│
├── agents/                     # 流转中枢
│   ├── manager.py              # Manager Agent(意图识别 + 规划 + Context)
│   ├── learning.py             # Learning Agent(DAG 执行 + 变量替换 + 重试)
│   ├── orchestrator.py         # Orchestrator Agent(流式 + Context)
│   └── skill_trainer.py        # SkillTrainer(教导闭环)
│
├── tools/                      # 工具模块
│   ├── base.py                 # Tool 协议 + ToolResult + ToolRegistry
│   ├── weather.py              # weather_query(httpx 异步)
│   └── search.py               # web_search(DuckDuckGo/Wikipedia/Bing 级联)
│
├── skills/                     # 技能库
│   ├── builtin/                # 内置技能(YAML)
│   │   └── travel_plan.yaml
│   ├── user/                   # 用户教导技能
│   ├── models.py               # Skill / SkillStep 数据类
│   ├── loader.py               # YAML + MD 加载器
│   ├── registry.py             # 加权打分 + 失效检测
│   └── manager.py              # SkillStore(兼容旧 API)
│
├── infra/                      # 基础设施
│   ├── config.py               # pydantic-settings 配置
│   ├── llm.py                  # AsyncOpenAI + 重试 + 流式 + token 计数
│   └── logger.py                # stdlib logging + 文件输出 + 订阅回调
│
├── ui/                         # Streamlit / CLI 入口
├── tests/                      # pytest + pytest-asyncio
├── docs/                       # 架构设计文档
├── docker-compose.yml
├── Dockerfile
├── requirements.txt            # 含 httpx
├── .env.example                # 配置示例
└── .env                        # 实际配置(勿提交)
```

## 智能体 = 流转中枢

### Manager Agent

```
职责:协调 Skill 和 Tool
- 意图识别:分析用户想要什么
- 技能选择:判断使用哪个技能(方法论)
- 工具规划:确定需要哪些工具获取数据
- 多轮上下文:接受 Context 参数,拼接到 LLM prompt
```

### Learning Agent

```
职责:执行 Tool
- 工具注册:管理可用工具
- 工具调用:执行具体的工具获取数据
- DAG 执行:按拓扑序 + 并行 group + 变量替换 + 重试
```

### Orchestrator Agent

```
职责:整合结果
- 数据整合:将工具获取的数据整合
- 回答生成:按技能方法论组织回答
- 流式输出:逐 token yield
- 多轮上下文:接受 Context 参数,拼接对话历史
```

## 技能 vs 工具

```
┌─────────────────────────────────────────────────────────────┐
│                        技能 (Skill)                          │
│                     完成任务的方法论                          │
│                                                               │
│  技能 = {                                                   │
│    name: "旅游规划",                                        │
│    method: "分析问题的方法论",                              │
│    steps: [{"id": "查天气", "tool": "weather_query",       │
│            "depends_on": []}],                              │
│    patterns: ["旅游", "行程"],                              │
│    source: "builtin | taught | imported"                    │
│  }                                                          │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                        工具 (Tool)                           │
│                    获取数据、执行操作                          │
│                                                               │
│  weather_query: {city, date} → {weather, temp, humidity}   │
│  web_search:    {query} → {text, source}                   │
└─────────────────────────────────────────────────────────────┘
```

## 处理流程

```
用户: "厦门明天天气,安排旅游行程"

┌─────────────────────────────────────────────────────────────┐
│  Manager Agent                                               │
│  输入: 用户请求 + 多轮 Context                                │
│  判断: 需要"旅游规划"技能                                    │
│  规划: weather_query(city=厦门)                             │
│       web_search(query=厦门景点)                             │
└────────────────────────────┬────────────────────────────────┘
                             ↓
┌─────────────────────────────────────────────────────────────┐
│  Learning Agent                                              │
│  (若 SKILL_DAG_ENABLED 且 skill 有 steps,走 DAGExecutor)    │
│                                                               │
│  执行: weather_query(city=厦门, date=tomorrow)              │
│  结果: 天气数据                                              │
│  执行: web_search(query=厦门景点)                            │
│  结果: 景点数据                                              │
└────────────────────────────┬────────────────────────────────┘
                             ↓
┌─────────────────────────────────────────────────────────────┐
│  Orchestrator Agent                                          │
│  输入: 天气数据 + 景点数据 + 旅游规划技能                     │
│  按技能步骤整合: 查天气 → 搜景点 → 整合行程                   │
│  输出: 完整的旅游行程安排                                     │
└─────────────────────────────────────────────────────────────┘
```

## 工具系统

| 工具 | 功能 |
|------|------|
| **weather_query** | 查询天气数据(httpx 异步,15s 超时) |
| **web_search** | 搜索网络信息(DuckDuckGo → Wikipedia → Bing 级联) |

## 启动方式

**后端:**
```bash
cd backend
pip install -r ../requirements.txt
python main.py
# 运行在 http://localhost:8000
```

**前端:**
```bash
cd frontend
npm install
npm run dev
# 运行在 http://localhost:3000
```

## 扩展指南

> 详细扩展方式与改进后设计见 [`docs/`](docs/) 目录。

### 添加新工具

1. 在 `tools/` 下创建新文件
2. 继承 `Tool` 基类,实现 `schema()` 和 `async execute(**kwargs)`
3. 在 `tools/base.py` 的 `_register_builtins()` 中 `register`
4. 详见 [docs/04-tools.md §8](docs/04-tools.md)

### 教导新技能

用户通过对话教导:
```
"分析问题应该先收集数据,再制定方案,最后执行"
```
→ SkillTrainer 抽取为 `Skill`,沉淀到 `skills/user/<name>@<version>.yaml`
→ 下次类似任务按此方法论处理
→ 前端自动弹出 toast 并刷新技能库
→ 详见 [docs/02-agents.md §6](docs/02-agents.md) 和 [docs/03-skills.md §8](docs/03-skills.md)

## 已知架构问题与改进路线

详见 [docs/08-roadmap.md](docs/08-roadmap.md)。

已在本轮修复:
- ✅ 多轮对话 Context 未启用 → Manager/Orchestrator 全部接入
- ✅ emit 异步路径调度顺序不可控 → ensure_future + gather drain
- ✅ weather_query 同步阻塞事件循环 → httpx 异步
- ✅ logger 简陋 print → stdlib logging + 文件 + 订阅
- ✅ 缺 .env.example
- ✅ 前端未接入 SkillManager + 未订阅 skill_learned
- ✅ session 字段名不一致

待处理(详见 P6 待做清单):
- 实体提取城市硬编码
- 工具 search.py 异步化
- backend/main.py 重复 on_event
- skill_trainer.persist 脏写 _registry
