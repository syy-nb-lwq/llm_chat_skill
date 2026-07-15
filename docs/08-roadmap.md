# 08 — 改进路线图

> 本文档把改进拆成可独立交付的阶段,每阶段标注:目标 / 任务 / 不破坏什么 / 验收。

---

## 阶段总览

```
P0 文档        ──── 已完成 ✅
P1 基础加固    ──── 已完成 ✅
P2 工具 DAG    ──── 已完成 ✅
P3 教导闭环    ──── 已完成 ✅
P4 前端重做    ──── 已完成 ✅
P5 工程化      ──── 已完成 ✅
P6 自我进化    ──── 已完成 ✅
  ├─ Phase 1: MemoryStore + Critic ✅
  ├─ Phase 2: SkillMerger + 审核 ✅
  └─ Phase 3: SelfReflectLoop ✅
```

每个阶段结束,代码都能跑、原有能力不退化。

---

## P0 — 文档(已完成 ✅)

**目标**:把"现状 + 改进设计"完整文档化,作为后续阶段的依据。

**已完成产物**:
- `docs/00-总览.md`
- `docs/01-架构设计.md`
- `docs/02-agents.md`
- `docs/03-skills.md`
- `docs/04-tools.md`
- `docs/05-core.md`
- `docs/06-backend-frontend.md`
- `docs/07-infra.md`
- `docs/08-roadmap.md`(本文件)

---

## P1 — 基础加固(已完成 ✅)

**目标**:把现有代码里明显的 bug / 反模式 / 缺失配置修掉,**不改变主要 API**。

### 任务清单

| ID | 任务 | 文件 | 状态 |
|---|---|---|---|
| P1-1 | 引入 `BaseAgent`,迁移 Manager/Orchestrator 公共逻辑 | `core/agent_base.py` | ✅ |
| P1-2 | Manager 输出用 JSON Schema 校验 + 重试 | `agents/manager.py` | ✅ |
| P1-3 | WebSocket `logger.unsubscribe` 移到 `finally`,修复泄漏 | `backend/main.py` | ✅ |
| P1-4 | `client_id` 改 UUID4,Session 持久化 | `backend/main.py` `backend/session.py` | ✅ |
| P1-5 | 补 `.env.example`,改用 `pydantic-settings` | `infra/config.py` + `.env.example` | ✅ |
| P1-6 | `LLMClient` 加 `chat_with_retry` | `infra/llm.py` | ✅ |
| P1-7 | `Context` 接入 `Agent.chat`,加 token 压缩 | `core/context.py` `core/agent.py` | ✅ |
| P1-8 | WebSocket 协议统一为 `event` 消息 | `backend/main.py` `frontend/src/websocket.js` | ✅ |
| P1-9 | `Logger` 接入标准 logging + 文件输出 | `infra/logger.py` | ✅ |
| P1-10 | 删除 `core/plugin.py`,统一走 `tools/registry.py` | 删除 | ✅ |

---

## P2 — 工具 DAG + 技能可执行(已完成 ✅)

**目标**:让技能真正可执行,支持并行 / 依赖 / 失败重试。

### 任务清单

| ID | 任务 | 状态 |
|---|---|---|
| P2-1 | `Skill` 升级为 `SkillStep` 列表,新增 YAML 加载器 | ✅ |
| P2-2 | `SkillRegistry` 加权打分 + 失效检测 | ✅ |
| P2-3 | `Tool` 加 `schema()`,`ToolResult` 加 `meta`,`ToolRegistry` | ✅ |
| P2-4 | `LearningAgent.execute_dag` + 参数变量解析 + 重试/超时 | ✅ |
| P2-5 | 新建 `DAGExecutor`,负责 skill.steps 的拓扑执行 | ✅ |
| P2-6 | Orchestrator 改为 per-step + 模板 + 流式 | ✅ |
| P2-7 | `Agent.handle` async 化,接 DAG 执行器 | ✅ |
| P2-8 | `weather_query` / `web_search` 按新接口改造 | ✅ |
| P2-9 | `SKILL_DAG_ENABLED` feature flag 控制新旧路径 | ✅ |

**验收**:
- ✅ `travel_plan.yaml` 完整定义并端到端跑通
- ✅ Manager 输出含 `depends_on` 时 DAG 正确拓扑
- ✅ 工具失败 → 重试 → fallback 全链路有日志

---

## P3 — 教导闭环(已完成 ✅)

**目标**:用户能通过对话"教"Agent 新技能,并自动沉淀。

### 任务清单

| ID | 任务 | 文件 | 状态 |
|---|---|---|---|
| P3-1 | `SkillTrainer` 实现(启发式 + LLM 二次确认) | [agents/skill_trainer.py](../agents/skill_trainer.py) | ✅ |
| P3-2 | `extract_skill` 输出符合新 Skill 结构 | 同上 | ✅ |
| P3-3 | 沉淀到 `skills/user/<name>@<version>.yaml`,版本化 | 同上 | ✅ |
| P3-4 | `Agent.handle` 优先走教导路径 | [core/agent.py](../core/agent.py) | ✅ |
| P3-5 | 新增 `event=skill_learned` 推送,前端高亮 | [frontend/src/App.vue](../frontend/src/App.vue) | ✅ |
| P3-6 | 前端 SkillManager 完整功能(列表/版本/删除) | [frontend/src/components/SkillManager.vue](../frontend/src/components/SkillManager.vue) | ✅ |

**验收**:
- ✅ 用户输入 "以后做 X 应该 Y",系统识别为教导
- ✅ 新技能写入 `skills/user/`,重启后仍可用
- ✅ 教导后立刻能用新技能完成相关任务
- ✅ 教导时推送 `skill_learned` 事件,前端切到管理标签 + toast 提示

### 使用示例

```
用户: "以后做数据总结,应该先抓网页、再提炼要点、最后给出建议"
     → SkillTrainer 识别为教导
     → 抽取为 {name: data_summary, method: ..., capability: ..., patterns: [...]}
     → 写入 skills/user/data_summary@1.0.0.yaml

用户: "帮我做个电商网站的数据总结"
     → Manager.match 命中 data_summary
     → 走 Skill DAG(若声明了 tool steps)
     → Orchestrator 按 method 整合输出
```

---

## P4 — 前端重做(已完成 ✅)

**目标**:把"流转可视化"做成主界面,展示 Agent 的完整思考过程。

### 任务清单

| ID | 任务 | 状态 |
|---|---|---|
| P4-1 | 拆组件:`ChatPanel` / `ToolList` / `SkillList` / `FlowPanel` | ✅ |
| P4-2 | `clientId` 持久化到 localStorage + 自动重连 | ✅ |
| P4-3 | `FlowPanel` 树形/时间轴渲染,显示 trace_id / 工具 / 耗时 | ✅ |
| P4-4 | 单步展开看 `params` / `data` / `error` + 复制按钮 | ✅ |
| P4-5 | 流式输出 assistant 消息(打字机) | ✅ |
| P4-6 | UI for SkillManager(列表 / 版本切换 / 删除) | ✅ |
| P4-7 | 后端技能管理 API | ✅ |

---

## P5 — 工程化(已完成 ✅)

**目标**:可部署、可测试、可观测。

| ID | 任务 | 状态 |
|---|---|---|
| P5-1 | pytest 覆盖 BaseAgent / Skill / Tool / DAG / Trainer / 后端 | ✅ |
| P5-2 | GitHub Actions CI(python 3.10/3.11/3.12 + 语法检查) | ✅ |
| P5-3 | Dockerfile + docker-compose | ✅ |
| P5-4 | `/api/health` 端点 + Docker HEALTHCHECK | ✅ |
| P5-5 | `.dockerignore` 排除不必要内容 | ✅ |
| P5-6 | README 启动方式 / 文档索引 | ✅ |

### 测试覆盖

```
tests/
├── conftest.py              # pytest 全局 fixture + 单例重置
├── test_tools.py            # ToolRegistry / ToolSchema / validate_params / httpx 异步
├── test_skills.py           # YAML loader / 加权打分 / 循环检测 / 版本冲突
├── test_learning_dag.py     # resolve_params / execute_dag / 依赖 / 循环
├── test_agents.py           # Manager / Orchestrator / Trainer / Context / emit 顺序
├── test_more_dag.py         # 扩展 DAG 场景(并行/fallback/重试)
├── test_logger.py           # logging 级别 / 订阅 / 单例
├── test_memory.py          # MemoryStore / ExecutionCritic / SkillMerger
└── test_backend.py          # FastAPI 集成: REST + WebSocket 全协议
```

跑测试:
```bash
pip install pytest pytest-asyncio
pytest -v
```

---

## P6 — 自我进化 ✅

> 详见 [docs/09-self-evolution.md](docs/09-self-evolution.md)。

### 概述

自我进化让 Agent 能从执行结果中学习,记住失败教训并生成改进建议。

### Phase 1: MemoryStore + Critic ✅

**目标**:先让系统"记住",不急着"自我修改"。

| 组件 | 文件 |
|---|---|
| MemoryStore | `core/memory.py` |
| ExecutionCritic | `core/critic.py` |
| Manager 集成 | `agents/manager.py` |

### Phase 2: SkillMerger + 审核 ✅

**目标**:允许自我改进,但有审核门槛。

| 组件 | 文件 |
|---|---|
| SkillMerger | `core/merger.py` |
| PatchReview UI | `frontend/src/components/PatchReview.vue` |

### Phase 3: SelfReflectLoop ✅

**目标**:Agent 能主动反思并修改自身行为。

| 组件 | 文件 |
|---|---|
| SelfReflectLoop | `core/reflect.py` |
| EvolutionDashboard | `frontend/src/components/EvolutionDashboard.vue` |

### 启用方式

```bash
# .env 文件
SELF_EVOLUTION_ENABLED=true
```

### API 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/patches` | GET | 获取待审阅建议 |
| `/api/patches/{id}/approve` | POST | 批准建议 |
| `/api/patches/{id}/reject` | POST | 拒绝建议 |
| `/api/memory/stats` | GET | 记忆统计 |
| `/api/reflections` | GET | 反思报告列表 |
| `/api/reflections/request` | POST | 请求立即反思 |

### 测试覆盖

- ✅ 73 passed / 1 skipped

---

## 优先级建议

| 阶段 | 价值 | 状态 |
|---|---|---|
| P0 | 文档 | ✅ 已完成 |
| P1 | 高 | ✅ 已完成 |
| P2 | 高 | ✅ 已完成 |
| P3 | 高(卖点) | ✅ 已完成 |
| P4 | 中(体验) | ✅ 已完成 |
| P5 | 中 | ✅ 已完成 |
| P6 | 自我进化 | ✅ 已完成 |

---

## 不在本次改进范围

明确推迟到 V2 的:
- 多模态(图文/语音)
- 分布式部署 / 高并发
- 权限/租户系统

---

## P7 — OpenClaw 特性借鉴

> 借鉴 [OpenClaw 架构](https://learnopenclaw.org/architecture.html) 的优秀设计,增强 Agent 的主动性与记忆能力。

### 阶段总览

```
P7-5 Provider Plugin      ──── ✅ 已完成
P7-4 SQLite 长期记忆      ──── ✅ 已完成
P7-2 SOUL 身份系统        ──── ✅ 已完成
P7-1 Heartbeat 主动任务   ──── ✅ 已完成
P7-3 MCP 工具协议         ──── ✅ 已完成
```

---

### P7-1 — Heartbeat 主动任务 ✅

**目标**:让 Agent 能主动定时执行任务,不止被动响应。

**参考**:OpenClaw 的 Heartbeat 机制,每 30 分钟唤醒检查 `HEARTBEAT.md` 清单。

#### 核心设计

```
┌─────────────────────────────────────────────────────┐
│                    HeartbeatScheduler               │
│  ┌─────────────────────────────────────────────┐    │
│  │ HEARTBEAT.md (用户定义的定时任务清单)          │    │
│  │ - 每天 8 点发送日程摘要                       │    │
│  │ - 检查 CI 构建状态变化                        │    │
│  │ - 重要邮件标记提醒                            │    │
│  └─────────────────────────────────────────────┘    │
│                       │                              │
│            每 N 分钟触发一次                          │
│                       ▼                              │
│  ┌─────────────────────────────────────────────┐    │
│  │ HeartbeatAgent                              │    │
│  │ 1. 加载 HEARTBEAT.md                         │    │
│  │ 2. 按条件筛选待执行项                         │    │
│  │ 3. 若无任务 → 返回 HEARTBEAT_OK (静默)        │    │
│  │ 4. 若有任务 → 正常执行 + 推送结果              │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [core/heartbeat.py](file:///d:\pythonProject\langchain_functioncall\core\heartbeat.py) | HeartbeatScheduler + HeartbeatAgent |
| [heartbeat/HEARTBEAT.md](file:///d:\pythonProject\langchain_functioncall\heartbeat\HEARTBEAT.md) | 任务配置示例 |
| [tests/test_heartbeat.py](file:///d:\pythonProject\langchain_functioncall\tests\test_heartbeat.py) | 单元测试 (11 passed) |

#### 启用方式

```bash
# .env 配置
HEARTBEAT_ENABLED=true
HEARTBEAT_INTERVAL_SECONDS=1800  # 30 分钟
HEARTBEAT_PATH=heartbeat/HEARTBEAT.md
```

#### 验收

- [x] HeartbeatScheduler 定时调度器
- [x] HEARTBEAT.md 加载器 + 条件解析
- [x] HEARTBEAT_OK 静默跳过机制
- [x] 支持每天/每隔 N 分钟/每隔 N 小时 触发条件

---

### P7-2 — SOUL 身份系统 ✅

**目标**:用配置文件定义 Agent 身份,更灵活地定制 Agent 性格和行为风格。

**参考**:OpenClaw 的 `SOUL.md` 文件,定义 Agent 的名字、性格、沟通风格、核心价值观。

#### 核心设计

```
# SOUL.md (Agent 身份定义)
---
name: "小智"
personality: "专业、高效,但不失幽默"
communication_style: "简洁明了,适当使用 emoji"
values:
  - "用户隐私第一"
  - "透明度优先"
  - "持续学习改进"
standing_instructions:
  - "每次回答前先确认用户需求"
  - "不确定时主动询问"
---
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [core/soul.py](file:///d:\pythonProject\langchain_functioncall\core\soul.py) | Soul 数据结构 + SoulLoader |
| [soul/SOUL.md](file:///d:\pythonProject\langchain_functioncall\soul\SOUL.md) | 默认 SOUL 配置 |
| [tests/test_soul.py](file:///d:\pythonProject\langchain_functioncall\tests\test_soul.py) | 单元测试 (9 passed) |

#### 启用方式

```bash
# .env 配置
SOUL_ENABLED=true
SOUL_PATH=soul/SOUL.md
```

#### 验收

- [x] Soul 数据结构定义 (name/personality/values/expertise 等)
- [x] SoulLoader 加载/热重载
- [x] BaseAgent 集成 SOUL
- [x] 支持 Markdown + YAML front-matter 格式

---

### P7-3 — MCP 工具协议 ✅

**目标**:标准化工具集成,支持动态发现和双向通信。

**参考**:OpenClaw 基于 MCP (Model Context Protocol) 连接 GitHub/数据库等工具服务。

#### 核心设计

```
┌─────────────┐    MCP 协议     ┌─────────────────────┐
│   Agent     │◀──────────────▶│    Tool Server      │
│  (Client)   │  stdio / SSE   │  (GitHub/DB/FS...)  │
└─────────────┘                 └─────────────────────┘
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [tools/mcp_client.py](file:///d:\pythonProject\langchain_functioncall\tools\mcp_client.py) | MCPClient + StdioTransport |
| [tests/test_mcp.py](file:///d:\pythonProject\langchain_functioncall\tests\test_mcp.py) | 单元测试 (10 passed) |

#### 启用方式

```bash
# .env 配置
MCP_ENABLED=true
# MCP 服务器配置 (JSON 数组)
MCP_SERVERS=[{"name": "github", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}]
```

#### 验收

- [x] MCPClient 基础实现
- [x] StdioTransport 传输层
- [x] 工具定义与 OpenAI function calling 格式转换
- [x] MCPServerConfig 配置管理

---

### P7-4 — SQLite 长期记忆 ✅

**目标**:用 SQLite + Embeddings 实现 Agent 的长期记忆和语义检索。

**参考**:OpenClaw 的记忆架构,分层管理 Working/Short-term/Long-term Memory。

#### 核心设计

```
┌─────────────────────────────────────────────────────────┐
│                    记忆分层架构                          │
│                                                         │
│  Working Memory ──── 当前对话 Context (已实现)          │
│        ↓                                                │
│  Short-term Memory ──── 本次会话历史 (已实现)           │
│        ↓                                                │
│  Long-term Memory ──── SQLite + Embeddings (新增)      │
│        │                                                │
│        ├── 成功执行路径 (用于复现)                       │
│        ├── 失败教训 (用于避免重复错误)                   │
│        └── 用户偏好 (用于个性化服务)                     │
└─────────────────────────────────────────────────────────┘
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [core/memory_db.py](file:///d:\pythonProject\langchain_functioncall\core\memory_db.py) | MemoryDB SQLite + FTS5 |
| [infra/embedding.py](file:///d:\pythonProject\langchain_functioncall\infra\embedding.py) | Embedding 服务封装 |
| [core/semantic_memory.py](file:///d:\pythonProject\langchain_functioncall\core\semantic_memory.py) | 语义检索实现 |
| [tests/test_memory_db.py](file:///d:\pythonProject\langchain_functioncall\tests\test_memory_db.py) | 单元测试 (18 passed) |

#### 启用方式

```bash
# .env 配置
SEMANTIC_MEMORY_ENABLED=true

# Embedding 配置
EMBEDDING_PROVIDER=openai  # openai / local / mock
EMBEDDING_API_KEY=sk-xxx
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSION=1536
```

#### 验收

- [x] 语义搜索支持混合搜索(关键词+向量)
- [x] SQLite + FTS5 全文搜索
- [x] 支持 OpenAI/Local/Mock 三种 Embedding
- [x] 与 Manager Agent 集成

---

### P7-5 — Provider Plugin (多 LLM 支持) ✅

**目标**:支持动态切换 LLM 提供商,支持本地模型。

**参考**:OpenClaw 的 Provider Plugin 系统,支持 Anthropic/OpenAI/本地模型动态注册。

#### 核心设计

```
┌─────────────────────────────────────────────────────┐
│                  Provider Plugin 系统               │
│                                                     │
│  @register_provider("openai")                       │
│  class OpenAIProvider(BaseProvider): ...             │
│                                                     │
│  @register_provider("anthropic")                   │
│  class AnthropicProvider(BaseProvider): ...         │
│                                                     │
│  @register_provider("local")                       │
│  class LocalProvider(BaseProvider): ...             │
│                                                     │
│  LLMClient.get_provider() → BaseProvider            │
└─────────────────────────────────────────────────────┘
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [infra/providers/base.py](file:///d:\pythonProject\langchain_functioncall\infra\providers\base.py) | BaseProvider 抽象接口 |
| [infra/providers/openai.py](file:///d:\pythonProject\langchain_functioncall\infra\providers\openai.py) | OpenAI Provider |
| [infra/providers/anthropic.py](file:///d:\pythonProject\langchain_functioncall\infra\providers\anthropic.py) | Anthropic Provider |
| [infra/providers/local.py](file:///d:\pythonProject\langchain_functioncall\infra\providers\local.py) | Local (Ollama) Provider |
| [infra/providers/manager.py](file:///d:\pythonProject\langchain_functioncall\infra\providers\manager.py) | Provider 动态注册管理 |
| [infra/llm.py](file:///d:\pythonProject\langchain_functioncall\infra\llm.py) | LLMClient 集成 Provider |
| [tests/test_providers.py](file:///d:\pythonProject\langchain_functioncall\tests\test_providers.py) | 单元测试 (15 passed) |

#### 启用方式

```bash
# .env 配置
MULTI_PROVIDER_ENABLED=true
DEFAULT_PROVIDER=openai

# OpenAI
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini

# Anthropic (可选)
# ANTHROPIC_API_KEY=sk-ant-xxx
# ANTHROPIC_MODEL=claude-sonnet-4-20250514

# Local/Ollama (可选)
LOCAL_BASE_URL=http://localhost:11434
LOCAL_MODEL=llama3.2
```

#### 验收

- [x] 能动态切换 OpenAI / Anthropic / Ollama
- [x] Provider 失败时自动重试
- [x] 兼容旧配置(单 Provider 模式)

---

### P7 优先级建议

| 子阶段 | 价值 | 复杂度 | 状态 |
|---|---|---|---|
| P7-5 Provider Plugin | 高(解耦 LLM) | 中 | ✅ 已完成 |
| P7-4 SQLite 记忆 | 高(体验提升) | 中 | ✅ 已完成 |
| P7-2 SOUL 身份 | 中(差异化) | 低 | ✅ 已完成 |
| P7-1 Heartbeat | 中(主动性) | 中 | ✅ 已完成 |
| P7-3 MCP 协议 | 中(生态集成) | 高 | ✅ 已完成 |

---

### P7 不破坏什么

- 所有现有 API 保持兼容
- Feature Flag 控制开关,默认关闭
- P6 自我进化系统仍可用

### 启用方式

```bash
# .env 文件
HEARTBEAT_ENABLED=false
SOUL_ENABLED=false
MCP_ENABLED=false
SEMANTIC_MEMORY_ENABLED=false
MULTI_PROVIDER_ENABLED=false
```

---

## P8 — Skill Manager Agent

> 技能库智能管理系统,负责技能检索、创建、更新和整理。

### 阶段总览

```
P8-1 技能检索     ──── ✅ 已完成
P8-2 技能创建     ──── ✅ 已完成
P8-3 技能更新     ──── ✅ 已完成
P8-4 技能整理     ──── ✅ 已完成
```

---

### P8-1 — 技能检索 ✅

**目标**:使用 LLM 判断技能库中是否有能覆盖当前任务的技能。

**核心设计**:

```python
class SkillRetrievalAgent(BaseAgent):
    async def find_matching_skills(user_input: str, top_k: int = 3) -> List[SkillMatch]
```

**检索流程**:

```
用户输入: "帮我写日报"
    │
    ▼
SkillRetrievalAgent.find_matching_skills()
    │
    ▼
LLM 判断: 哪些技能能覆盖这个需求
    │
    ▼
返回: SkillMatch(skill, score, coverage)
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [agents/skill_manager.py](file:///d:\pythonProject\langchain_functioncall\agents\skill_manager.py) | SkillRetrievalAgent |

---

### P8-2 — 技能创建 ✅

**目标**:支持交互式教导,创建新技能。

**核心设计**:

```python
class SkillCreatorAgent(BaseAgent):
    async def create_skill_from_teaching(user_input: str) -> Tuple[bool, str, Optional[Skill]]
```

**创建流程**:

```
用户: "生成一个技能用来做日报编写"
    │
    ▼
检查相似技能 (LLM 语义判断)
    │
    ▼
信息完整 → 直接保存
信息不完整 → 交互式询问 → 保存
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [agents/skill_trainer.py](file:///d:\pythonProject\langchain_functioncall\agents\skill_trainer.py) | 交互式教导 |

---

### P8-3 — 技能更新 ✅

**目标**:基于用户反馈更新已有技能。

**核心设计**:

```python
class SkillUpdaterAgent(BaseAgent):
    async def update_skill(skill_name: str, feedback: str) -> Tuple[bool, str, Optional[Skill]]
```

---

### P8-4 — 技能整理 ✅

**目标**:定时检查、归纳、合并重复技能。

**核心设计**:

```python
class SkillOrganizerAgent(BaseAgent):
    async def analyze_skill_duplication() -> List[SkillAnalysis]
    async def merge_skills(skill1: str, skill2: str) -> Tuple[bool, str, Optional[Skill]]
```

**整理流程**:

```
定时触发 / 手动触发
    │
    ▼
两两对比技能 (LLM 判断相似度)
    │
    ▼
重叠度 > 50% → 建议合并
    │
    ▼
用户确认 → 执行合并
```

#### 已完成文件

| 文件 | 说明 |
|---|---|
| [agents/skill_manager.py](file:///d:\pythonProject\langchain_functioncall\agents\skill_manager.py) | SkillManagerAgent 统一入口 |
| [skills/manager.py](file:///d:\pythonProject\langchain_functioncall\skills\manager.py) | SkillStore.remove() |

#### 测试覆盖

```
tests/test_skill_manager.py - 4 passed
```

---

### P8 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    SkillManagerAgent                         │
│                       (统一入口)                             │
├───────────────┬───────────────┬─────────────┬────────────┤
│ SkillRetrieval│ SkillCreator  │ SkillUpdater│ Organizer  │
│    Agent     │    Agent     │    Agent    │   Agent   │
├───────────────┴───────────────┴─────────────┴────────────┤
│                    SkillStore                               │
│                (skills/*.yaml)                            │
└───────────────────────────────────────────────────────────┘
```

#### 四大职责

| 职责 | Agent | 方法 |
|------|-------|------|
| **1. 检索技能** | `SkillRetrievalAgent` | `find_matching_skills()` |
| **2. 创建技能** | `SkillCreatorAgent` | `create_skill_from_teaching()` |
| **3. 更新技能** | `SkillUpdaterAgent` | `update_skill()` |
| **4. 整理技能** | `SkillOrganizerAgent` | `analyze_skill_duplication()` / `merge_skills()` |

---

### P8 验收

- [x] 技能检索: LLM 判断任务覆盖度
- [x] 技能创建: 检查相似技能,避免重复
- [x] 技能更新: 基于反馈更新
- [x] 技能整理: 分析重复,支持合并
- [x] 测试覆盖: 204 passed

---

## V2 特性(未来规划)

- 多模态(图文/语音)
- 分布式部署 / 高并发
- 权限/租户系统
- Agent 协作网络

---

## P9 — 修复与依赖完善 ✅

> 修复拉取 `4f75742` 后发现的工程问题与测试缺陷。

### 问题清单

| ID | 问题 | 影响 |
|---|---|---|
| P9-1 | `backend/websocket_handler.py` 等 4 个文件被误删,导致后端 import 即崩 | 后端完全不可用 |
| P9-2 | `requirements.txt` 缺 `tiktoken` / `jsonschema` / `numpy` / `anthropic` / `python-multipart` | 多个模块运行时缺包 |
| P9-3 | `Agent.handle` 异步 emit 顺序错乱:用 `ensure_future` 调度后,多个回调并发 `await asyncio.sleep(0.01)`,完成顺序由 sleep 决定而非 emit 顺序 | `test_agent_handle_async_on_event_preserves_order` 失败;前端事件流顺序错乱 |

### 修复

| ID | 修复 | 文件 |
|---|---|---|
| P9-1 | `git checkout HEAD -- skills/loader.py skills/models.py skills/registry.py backend/websocket_handler.py` 恢复 | — |
| P9-2 | `requirements.txt` 补齐 5 个依赖 | `requirements.txt` |
| P9-3 | `emit` 改为「不立即 schedule」,只把 `_deferred` 协程挂入 `pending_async`;`_drain_pending` 用 `await asyncio.ensure_future(coro)` **串行** 触发,保证完成顺序与 emit 一致 | `core/agent.py` |

### 修复前后对比

```python
# 修复前(乱序)
def emit(event, payload):
    task = asyncio.ensure_future(on_event(event, payload))
    pending_async.append(task)

async def _drain_pending():
    await asyncio.gather(*pending_async, return_exceptions=True)  # 并发,顺序由 sleep 决定

# 修复后(顺序保证)
def emit(event, payload):
    async def _deferred():
        await _wrap()  # _wrap 调用 on_event
    pending_async.append(_deferred())  # 仅挂 coroutine,不 schedule

async def _drain_pending():
    for coro in batch:
        await asyncio.ensure_future(coro)  # 串行,顺序与 list 中加入顺序一致
```

### 测试结果

- 修复前: **203 passed, 1 failed, 1 skipped**
- 修复后: **204 passed, 1 skipped** ✅

### 验收

- [x] 后端可正常 import (17 个 routes 注册)
- [x] `pytest` 全绿 (204 passed / 1 skipped)
- [x] `test_agent_handle_async_on_event_preserves_order` 通过
- [x] `requirements.txt` 包含全部运行依赖
