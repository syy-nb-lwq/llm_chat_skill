# Skill Agent

**技能 = 方法论 + 步骤 + 代码（可选）**

> 📚 详细架构设计与改进路线见 [`docs/`](docs/) 目录:
> - [docs/00-总览.md](docs/00-总览.md) · [01-架构设计.md](docs/01-架构设计.md) · [08-roadmap.md](docs/08-roadmap.md)
> - 模块设计: [agents](docs/02-agents.md) · [skills](docs/03-skills.md) · [tools](docs/04-tools.md) · [core](docs/05-core.md) · [backend/frontend](docs/06-backend-frontend.md) · [infra](docs/07-infra.md)
>
> 旧架构概览保留在 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 核心理念

技能不只是代码，更是一套完成任务的方法论。

```
┌──────────────────────────────────────┐
│          Skill Agent                      │
├──────────────────────────────────────┤
│  接收任务                            │
│  ↓                                  │
│  分析任务 → 选择/生成技能              │
│  ↓                                  │
│  方法论 → 指导分析思路                │
│  步骤 → 指导执行流程                │
│  代码 → 可选的执行补充                │
└──────────────────────────────────────┘
```

## 技能定义

| 字段 | 说明 |
|------|------|
| method | 分析问题的方法论 |
| steps | 处理步骤(可含 tool + depends_on,执行器按 DAG 调度) |
| code | 可选的执行代码 |
| patterns | 触发关键词 |
| tags | 分类标签 |

## 快速开始

### 方式 A:Docker(推荐)

```bash
cp .env.example .env
# 编辑 .env 填 LLM_API_KEY
docker-compose up -d
# 后端: http://localhost:8000
# 前端: http://localhost:3000
```

### 方式 B:本地

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 配置 API Key

# 后端
cd backend && python main.py

# 前端(另一终端)
cd frontend && npm install && npm run dev
```

### Streamlit / CLI

```bash
# Streamlit 单页
streamlit run ui/app.py

# 终端交互
python ui/cli.py
```

## 核心能力

- **三大 Agent 协作**:Manager(规划) + Learning(执行) + Orchestrator(整合)
- **技能 DAG 执行**:技能步骤声明 tool + depends_on + parallel_group,执行器按拓扑序调度,支持并行 / 重试 / 超时 / 变量替换
- **教导闭环**:用户输入"以后做 X 应该 Y",系统自动抽取为 Skill,持久化到 `skills/user/`
- **多 Session**:每个 client 独立 Session,断线重连后 Context 不丢
- **全链路可观测**:WebSocket 实时推送 thinking / plan / tool_call / tool_result / skill_learned / 流式 message_delta
- **前端流转可视化**:FlowPanel 显示 trace_id / 耗时 / 步骤数,点开看完整 payload + 复制
- **技能管理**:Web UI 看 / 删 / 刷新技能(含多版本)

## 测试

```bash
pip install pytest pytest-asyncio
pytest -v
```

CI: GitHub Actions 跑 Python 3.10 / 3.11 / 3.12 矩阵测试。

## 项目结构

```
├── backend/                    # 后端服务
│   ├── main.py                 # FastAPI + WebSocket 入口
│   └── session.py              # SessionManager + TTL GC
├── frontend/                   # 前端 (Vue 3)
│   ├── src/
│   │   ├── App.vue             # 主入口,聚合事件
│   │   ├── websocket.js        # WS 客户端(clientId 持久化 + 重连)
│   │   └── components/
│   │       ├── ChatPanel.vue   # 聊天主区
│   │       ├── FlowPanel.vue   # 流转可视化
│   │       ├── ToolList.vue    # 工具列表
│   │       ├── SkillList.vue   # 技能列表(简版)
│   │       └── SkillManager.vue # 技能管理(版本/删除)
│   └── package.json
├── core/                       # 核心 Agent / DAG / Context
│   ├── agent.py                # Agent 主类(异步)
│   ├── agent_base.py           # BaseAgent(LLM/重试/Schema)
│   ├── dag.py                  # DAGExecutor
│   ├── context.py              # 多轮对话 Context
│   └── capability.py           # Capability 分析器
├── agents/                     # 流转中枢
│   ├── manager.py              # Manager Agent
│   ├── learning.py             # Learning Agent(DAG 执行)
│   ├── orchestrator.py         # Orchestrator Agent(流式)
│   └── skill_trainer.py        # Skill Trainer(教导闭环)
├── tools/                      # 工具模块
│   ├── base.py                 # Tool 协议 + ToolRegistry
│   ├── weather.py              # weather_query
│   └── search.py               # web_search(4 后端级联)
├── skills/                     # 技能库
│   ├── builtin/                # 内置技能(YAML)
│   ├── user/                   # 用户教导技能
│   ├── models.py               # Skill / SkillStep
│   ├── loader.py               # YAML + MD 加载
│   ├── registry.py             # 加权打分 + 失效检测
│   └── manager.py              # SkillStore(兼容旧 API)
├── infra/                      # 基础设施
│   ├── config.py               # pydantic-settings
│   ├── llm.py                  # AsyncOpenAI + 重试 + 流式
│   └── logger.py               # 标准 logging + 敏感脱敏
├── tests/                      # pytest 测试
├── ui/                         # Streamlit / CLI 入口
├── docs/                       # 架构设计文档
├── docker-compose.yml          # 一键启动
├── Dockerfile
├── requirements.txt
└── .env.example
```

## License

MIT