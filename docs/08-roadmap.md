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
P6 深化       ──── 本轮已做,待归档
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
└── test_backend.py          # FastAPI 集成: REST + WebSocket 全协议
```

跑测试:
```bash
pip install pytest pytest-asyncio
pytest -v
```

---

## P6 — 深化(本轮已做,全部完成 ✅)

> 2026-07 本轮审查后的修复与增强。

### 已完成(第一批)

| ID | 问题 | 文件 | 改动 |
|---|---|---|---|
| B1 | `session.py` 字段名不一致 | `backend/session.py` | `log_callbacks` → `dispose_callbacks` |
| A2 | Context 未真正接入 LLM | `agents/manager.py` `agents/orchestrator.py` `core/agent.py` | Manager/Orchestrator 接受 context 参数,拼到 user prompt;Agent.handle 实际传入;新增 2 个单测 |
| C8 | 前端未接入 SkillManager + 未订阅 skill_learned | `frontend/src/App.vue` | SkillManager 替换 SkillList;加 `skill_learned` 监听;toast + 自动重载 + 切标签 |
| C4 | 缺 `.env.example` | 新建 `.env.example` | 覆盖所有配置项,含各 LLM 端点示例 |
| B5 | weather.py async 内用同步 requests | `tools/weather.py` | 改 httpx.AsyncClient;共享连接池;15s timeout 真正生效;加 aclose() 钩子 |
| B3 | emit() 异步路径 create_task 未 await | `core/agent.py` | `ensure_future` 收集到列表,handle() 末尾 `asyncio.gather` drain,finally 保证;新增 2 个单测 |
| B2 | logger 简陋 print 输出 | `infra/logger.py` | 升级 stdlib logging;支持 config.log_level/文件输出/订阅回调;新增 6 个单测 |

### 已完成(第二批)

| ID | 问题 | 文件 | 改动 |
|---|---|---|---|
| B8 | `persist` 直接写 `_registry._by_name` | `agents/skill_trainer.py` | 改用 `store.add()` 公共 API |
| B7 | 重复 `@app.on_event("startup")` | `backend/main.py` | 合并为单一 startup,GC 移入内部 |
| B4 | 城市列表仅 18 个 | `core/agent.py` | 扩充至 37 城(含港澳台);去占位代码 |
| B9 | 版本递增粗粒度 try/except | `agents/skill_trainer.py` | 精确捕获 ValueError/IndexError,逐段解析 |

**测试结果:58 passed / 1 skipped**

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
| P6 | 深化 | ✅ 本轮已做 |

---

## 不在本次改进范围

明确推迟到 V2 的:
- 多 LLM 提供商适配
- 多模态(图文/语音)
- 分布式部署 / 高并发
- 权限/租户系统
- Embedding 语义匹配(版本 1 用关键词即可)

## V2 目标:自我进化

详见 [docs/09-self-evolution.md](docs/09-self-evolution.md)。

核心组件:
- **ExecutionCritic**:每次执行后评估质量,生成改进建议
- **MemoryStore**:跨 Session 持久化经验,让 Agent 记住历史失败
- **SkillMerger**:多版本 Skill 自动合并
- **SelfReflectLoop**:低负载时主动反思,生成洞察

演进路径:Phase 1(MemoryStore+Critic,只记录不修改) → Phase 2(SkillMerger+审核) → Phase 3(开放自我修改)
