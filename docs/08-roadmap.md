# 08 — 改进路线图

> 本文档把改进拆成可独立交付的阶段,每阶段标注:目标 / 任务 / 不破坏什么 / 验收。

---

## 阶段总览

```
P0 文档        ──── 当前阶段,纯文档
P1 基础加固    ──── BaseAgent / JSON 重试 / Context / WS 修复 / .env.example
P2 工具 DAG    ──── Skill 升级为可执行规格,DAGExecutor,Learning 并行
P3 教导闭环    ──── SkillTrainer + UI 入口
P4 前端重做    ──── 流转可视化 + 多 Session
P5 工程化      ──── Docker / 测试 / CI / 监控
```

每个阶段结束,代码都能跑、原有能力不退化。

---

## P0 — 文档(本次已完成)

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

**验收**:docs 目录 9 个文档齐全,与代码对照过。

---

## P1 — 基础加固

**目标**:把现有代码里明显的 bug / 反模式 / 缺失配置修掉,**不改变主要 API**。

### 任务清单

| ID | 任务 | 文件 | 工作量 |
|---|---|---|---|
| P1-1 | 引入 `BaseAgent`,迁移 Manager/Orchestrator 公共逻辑 | 新建 `core/agent_base.py`,改 `agents/manager.py` `agents/orchestrator.py` | M |
| P1-2 | Manager 输出用 JSON Schema 校验 + 重试 | `agents/manager.py` | S |
| P1-3 | WebSocket `logger.unsubscribe` 移到 `finally`,修复泄漏 | `backend/main.py` | XS |
| P1-4 | `client_id` 改 UUID4,Session 持久化 | `backend/main.py` `backend/session.py` | M |
| P1-5 | 补 `.env.example`,改用 `pydantic-settings` | `infra/config.py` 新文件 `.env.example` | S |
| P1-6 | `LLMClient` 加 `chat_with_retry` | `infra/llm.py` | S |
| P1-7 | `Context` 接入 `Agent.chat`,加 token 压缩 | `core/context.py` `core/agent.py` | M |
| P1-8 | WebSocket 协议统一为 `event` 消息(后端 + 前端) | `backend/main.py` `frontend/src/websocket.js` `frontend/src/App.vue` | M |
| P1-9 | `Logger` 接入标准 logging + 文件输出 + 敏感脱敏 | `infra/logger.py` | S |
| P1-10 | 删除 `core/plugin.py`,统一走 `tools/registry.py` | 删除 / 改调用方 | S |

**不破坏**:
- Manager / Orchestrator / Learning 公共方法签名不变
- WebSocket 协议升级但兼容旧字段(后端 `step` 和 `event=step` 同时发一段时间)

**验收**:
- `pytest` 全绿(基础测试)
- WebSocket 断开重连后能恢复 Session
- `.env.example` 复制后即可跑通

---

## P2 — 工具 DAG + 技能可执行

**目标**:让技能真正可执行,支持并行 / 依赖 / 失败重试。

### 任务清单

| ID | 任务 | 文件 | 工作量 |
|---|---|---|---|
| P2-1 | `Skill` 升级为 `SkillStep` 列表,新增 YAML 加载器 | 新建 `skills/models.py` `skills/loader.py`,改 `skills/manager.py` | L |
| P2-2 | `SkillRegistry` 加权打分 + 失效检测 | 新建 `skills/registry.py` | M |
| P2-3 | `Tool` 加 `schema()`,`ToolResult` 加 `meta`,新增 `ToolRegistry` | 改 `tools/base.py`,新建 `tools/registry.py` | M |
| P2-4 | `LearningAgent.execute_dag` + 参数变量解析 + 重试/超时 | 改 `agents/learning.py` | L |
| P2-5 | 新建 `DAGExecutor`,负责 skill.steps 的拓扑执行 | 新建 `core/dag.py` | M |
| P2-6 | Orchestrator 改为 per-step + 模板 + 流式 | 改 `agents/orchestrator.py` | L |
| P2-7 | `Agent.handle` async 化,接 DAG 执行器 | 改 `core/agent.py` | M |
| P2-8 | web_search / weather_query 按新接口改造 | 改 `tools/search.py` `tools/weather.py` | S |
| P2-9 | `feature flag: SKILL_DAG_ENABLED` 控制新旧路径切换 | `infra/config.py` 各 Agent | XS |

**不破坏**:
- 旧 `.md` 技能文件仍能加载(降级路径)
- 老 `execute_tasks()` 保留

**验收**:
- `travel_plan.yaml` 完整定义并端到端跑通
- Manager 输出含 `depends_on` 时 DAG 正确拓扑
- 工具失败 → 重试 → fallback 全链路有日志

---

## P3 — 教导闭环(已完成)

**目标**:用户能通过对话"教"Agent 新技能,并自动沉淀。

### 任务清单

| ID | 任务 | 文件 | 状态 |
|---|---|---|---|
| P3-1 | `SkillTrainer` 实现(启发式 + LLM 二次确认) | [agents/skill_trainer.py](../agents/skill_trainer.py) | ✅ |
| P3-2 | `extract_skill` 输出符合新 Skill 结构 | 同上 | ✅ |
| P3-3 | 沉淀到 `skills/user/<name>@<version>.yaml`,版本化 | 同上 | ✅ |
| P3-4 | `Agent.handle` 优先走教导路径 | [core/agent.py](../core/agent.py) | ✅ |
| P3-5 | 新增 `event=skill_learned` 推送,前端高亮 | [frontend/src/App.vue](../frontend/src/App.vue) | ✅ |
| P3-6 | 前端加"技能库"标签页,可查看/展开 | [frontend/src/components/SkillManager.vue](../frontend/src/components/SkillManager.vue) | ✅ |

**不破坏**:
- 不教导时,行为与 P2 完全一致(启发式未命中直接降级)

**验收**:
- ✅ 用户输入 "以后做 X 应该 Y",系统识别为教导
- ✅ 新技能写入 `skills/user/`,重启后仍可用
- ✅ 教导后立刻能用新技能完成相关任务(下次 match 命中)
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

## P4 — 前端重做(已完成)

**目标**:把"流转可视化"做成主界面,展示 Agent 的完整思考过程。

### 任务清单

| ID | 任务 | 文件 | 状态 |
|---|---|---|---|
| P4-1 | 拆组件:`ChatPanel` / `ToolList` / `SkillList` / `FlowPanel` | [frontend/src/App.vue](../frontend/src/App.vue) | ✅ |
| P4-2 | `clientId` 持久化到 localStorage + 自动重连 | [frontend/src/websocket.js](../frontend/src/websocket.js) | ✅ |
| P4-3 | `FlowPanel` 树形/时间轴渲染,显示 trace_id / 工具 / 耗时 | [frontend/src/components/FlowPanel.vue](../frontend/src/components/FlowPanel.vue) | ✅ |
| P4-4 | 单步展开看 `params` / `data` / `error` + 复制按钮 | 同上 | ✅ |
| P4-5 | 流式输出 assistant 消息(打字机) | [frontend/src/components/ChatPanel.vue](../frontend/src/components/ChatPanel.vue) | ✅ |
| P4-6 | UI for SkillManager(列表 / 版本切换 / 删除单版本 / 删除全部) | [frontend/src/components/SkillManager.vue](../frontend/src/components/SkillManager.vue) | ✅ |
| P4-7 | 后端技能管理 API(DELETE /api/skills/{name}, {name}/{version}, POST /reload) | [backend/main.py](../backend/main.py) | ✅ |

**验收**:
- ✅ 用户输入一次,流程面板完整看到:规划 → 工具1/2 → 整合
- ✅ 工具调用展开能看到 params / data / 耗时 / 来源后端
- ✅ 断网 30s 重连后 Session 不丢
- ✅ 技能管理:可按 name 看所有版本,逐版本删除,全部删除,刷新

---

## P5 — 工程化(已完成)

**目标**:可部署、可测试、可观测。

### 任务清单

| ID | 任务 | 状态 |
|---|---|---|
| P5-1 | pytest 覆盖 BaseAgent / Skill / Tool / DAG / Trainer / 后端 | ✅ |
| P5-2 | GitHub Actions CI(python 3.10/3.11/3.12 + 语法检查) | ✅ |
| P5-3 | Dockerfile + docker-compose(后端 + 前端) | ✅ |
| P5-4 | `/api/health` 端点 + Docker HEALTHCHECK | ✅ |
| P5-5 | `.dockerignore` 排除不必要内容 | ✅ |
| P5-6 | README 启动方式 / 文档索引 | ✅ |

### 测试覆盖

```
tests/
├── conftest.py              # pytest 全局 fixture + 单例重置
├── test_tools.py            # ToolRegistry / ToolSchema / validate_params
├── test_skills.py           # YAML loader / 加权打分 / 循环检测 / 版本冲突
├── test_learning_dag.py     # resolve_params / execute_dag / 依赖 / 循环
├── test_agents.py           # Manager / Orchestrator / Trainer (mock LLM)
└── test_backend.py          # FastAPI 集成: REST + WebSocket 全协议
```

跑测试:
```bash
pip install pytest pytest-asyncio
pytest -v
```

### Docker 部署

```bash
cp .env.example .env  # 编辑 LLM_API_KEY
docker-compose up -d
# 后端: localhost:8000
# 前端: localhost:3000
```

### CI

`.github/workflows/ci.yml` 在 push/PR 触发:
- Python 3.10 / 3.11 / 3.12 矩阵测试
- 语法 + import 检查

---

## 优先级建议

| 阶段 | 价值 | 风险 | 建议时间 |
|---|---|---|---|
| P1 | 高 | 低 | 立即开始,1~2 周 |
| P2 | 高 | 中 | 紧随 P1,2~3 周 |
| P3 | 高(卖点) | 中 | P2 完成后,1~2 周 |
| P4 | 中(体验) | 低 | P3 同步或稍后,1~2 周 |
| P5 | 中 | 低 | 持续进行,穿插在 P1~P4 |

---

## 不在本次改进范围

明确推迟到 V2 的:
- 多 LLM 提供商适配
- 多模态(图文/语音)
- 分布式部署 / 高并发
- 权限/租户系统
- Embedding 语义匹配(版本 1 用关键词即可)

这些是**业务规模化阶段**才需要的,不要在 P1~P5 阶段提前做。