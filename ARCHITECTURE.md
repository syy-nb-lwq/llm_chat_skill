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
- `clientId`（服务端称 `conn_id`）保存在浏览器 `localStorage`，**只用于 topic 路由**，
  不再作为会话键，也不代表身份
- 后端在 `backend/websocket_handler.py` 分发：
  - `init/{conn_id}` → 服务端签发身份（`session_id` / `client_id`）
  - `chat/{conn_id}` → 直接放行
  - `reset/{conn_id}` → 直接放行
  - `ping/{conn_id}` → 心跳

### 2.2 会话层

- `backend/session.py` 按**服务端签发的 `client_id`** 持有 `Session`；
  额外维护 `session_id → Session`，**同会话的重连复用同一个 `Session`/`Agent`**
  （否则新旧两个 Agent 会同时写会话库、各自维护摘要基线）
- `SessionManager.issue_identity()` 维护 `conn_id → client_id` 绑定，
  `resolve_identity()` 按绑定放行（本地单用户，无令牌校验）
- 每个 `Session` 内含一个 `Agent`（带签发的 `session_id`）
- **每个 `Session` 有一条工作队列 + 一个 worker**：`chat` / `reset` / `feedback`
  都投递到队列里串行执行。`Agent.handle()` 依赖 `identity` / `execution_id` /
  `context` 等可变实例状态，两轮交叉会串台
- **dispatcher 只投递、不执行**：它是所有连接共用的一条回调，在它里面
  `await` 一整轮会把其他人的 `ping` / `init` 一起卡住（`ping` 走快通道）
- 一个 `Session` 可能被多条连接引用，`gc` / `destroy` **只摘除引用**，
  最后一个引用消失时才 dispose（并取消 worker）
- 支持 TTL GC（会话过期时同步清理身份绑定）
- dispose 回调现在支持异步 await

### 2.3 Agent 层

`core/agent.py` 是统一入口：

- `handle()`：异步主入口
- `chat()`：同步适配入口，供 `CLI` / `Streamlit` 使用（复用同一个事件循环，避免工具的 HTTP 连接池跨 loop 失效）

内部聚合：

- `ManagerAgent`
- `LearningAgent`
- `OrchestratorAgent`
- `DAGExecutor`
- `SkillTrainer`
- `ExecutionCritic`

### 2.4 执行标识

每次 `handle()` 在入口创建 `IdentityContext`（`core/identity.py`），生成唯一的
`execution_id` 与 `turn_id`，并沿着 `ExecutionContext` 传到 `MemoryStore` 落盘，
保证同一 session 的多轮执行不会互相覆盖记录。`trace_id` 与 `execution_id` 同值。

## 3. 三个 Agent 的职责

### 3.1 ManagerAgent

位置：`agents/manager.py`

职责：

- 分层意图识别：L0 结构化规则（无 LLM）→ L1 闭集判别 → L2 LLM 规划 → L3 澄清
  （四层架构的设计见 `docs/12-意图分层识别方案.md`）
- 当前实现：L0 结构化规则（`core/intent_detector.py`，只"证明强意图"）
  → L1 闭集判别（`core/llm_judge.py`，一次 LLM 调用，6 标签 + 置信度）
  → L2 LLM 规划（只规划，不再输出意图）
  → L3 澄清（`core/clarifier.py`，意图不明时问一句 + 候选项）
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
- 更新采用「生成新版本 + 切 active」策略：写 `<name>@<new_version>.yaml` 并标记
  `active: true`，旧版本文件标记 `active: false` 且内容保留，可随时回滚

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
- 审批 patch 后生成新的技能版本并切 active，旧版本保留
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

## 6.1 记忆三层：工作 / 短期 / 长期

记忆按**"它回答什么问题"**分三层，不按存储时效性分。三层的唯一入口在
`core/memorykit/`：主链只认这三张脸，不直接摸底层 store。

| 层 | 回答的问题 | 生命周期 | 载体 | 唯一入口 |
|---|---|---|---|---|
| **工作记忆** | 这一轮做到哪了？ | 单轮，轮末即弃 | 进程内对象（不落库） | `memorykit/working.py` |
| **短期记忆** | 这次会话说过什么？ | 会话，跨重启 | `memory/sessions.db` | `memorykit/short_term.py` |
| **长期记忆** | 这个人是谁 / 什么做法长期有效？ | 永久 | `semantic_memory.db` + `memory/*` | `memorykit/long_term.py` |

### 谁写、谁读（硬约束）

| 层 | 谁**可以**写 | 谁**可以**读 | 禁止 |
|---|---|---|---|
| 工作记忆 | Agent 主链、SubAgentPool（本轮内） | 重规划、回答生成、goal loop | 跨轮复用；注入 prompt 以外的持久化 |
| 短期记忆 | **只有**会话层（`Context.sink` 落库路径） | 会话恢复、会话内检索、`/api/sessions` | 工具 / 子智能体 / critic 写；把摘要当长期事实 |
| 长期记忆 | **只有**后台写入器（`MemoryWriter` / `Critic` / `SkillQuality`） | 轮首装配一次（画像 ≤400 字常驻）、规划前 hints | 工具内写画像；每轮全量注入；同步阻塞回答 |

调用矩阵（谁 → 哪层 → 什么时候）：

| 调用方 | 工作记忆 | 短期记忆 | 长期记忆 |
|---|---|---|---|
| `Agent.handle` | 轮首 `begin_turn`，全程读写，`finally` 里 `end_turn` | 轮末经 `Context.sink` 追加消息 | 轮首读画像，异步写画像 |
| `ManagerAgent.plan` | 读 `findings`（重规划回灌） | 读会话内检索结果 | 读画像块 + 经验 hints |
| `LearningAgent` / `ToolHub` | 读上游槽位、写自己的结果 | — | — |
| `OrchestratorAgent` | 读 `tool_results` | —（上下文由 Agent 传入） | —（画像由 Agent 传入） |
| `SubAgentPool` | 独立命名空间 `subagent:<task_id>` | — | — |
| `MemoryWriter`（后台） | — | — | 写画像 |
| `ExecutionCritic` | 读执行上下文 | — | 写成功 / 失败经验 |
| REST API / 前端 | — | 列、删会话 | 列、忘记忆 |

### 唯一允许的流向：单向沉淀

```text
工作记忆 ──轮末──> 短期记忆（消息原文）──抽取──> 长期记忆（画像 / 经验）
```

反向全部禁止：长期记忆不写回短期（画像不是"说过的话"）；工作记忆不直接写长期
（一次工具输出不能改画像）；短期内容不被当长期知识注入（会话里的一句话不是这个人的属性）。

### 工作记忆的槽位契约

槽位名是跨模块约定，改名先改这张表：

| 槽位 | 写入方 | 读取方 |
|---|---|---|
| `plan` | Agent（规划后、重规划后） | 回答生成、重试 |
| `tool_results` | Agent（DAG 执行后） | 回答生成、Orchestrator |
| `findings` | Agent（重规划前追加） | `prior_findings` 回灌 |
| `subagent:<task_id>` | SubAgentPool | Agent 汇总 |
| `failure` | Agent（失败取证） | 排障脚本 / `/api/traces` |

约束：单值 4k 字符、最多 200 个槽位，超限截断并标注（工作记忆不能长成第二个数据库）。
**只在本轮失败时** `dump()` 到 `memory/turns/<execution_id>.json` 供排障，其余情况轮末 `clear()`。

### 短期记忆

取舍是：**全量保留，不设过期、不删除**。存储与注入分开 —— 存储一直留着，
注入时才按 token 预算裁剪成"摘要 + 最近 N 轮"。

```
用户消息 ──┐
           ├─→ Context.add_message ─→ sink ─→ session_messages（全量）
助手消息 ──┘                              │
                                          ├─→ 滚动摘要（超过阈值时）
                                          └─→ 会话内检索（CJK 二元组打分）

每轮调 LLM：system + 滚动摘要 + 最近 N 轮原文 + 本轮输入
```

### 长期记忆由三个子库组成，但只有一个入口

| 子库 | 存什么 | 载体 | 写入方 |
|---|---|---|---|
| 画像 | 偏好 / 习惯 / 方法 / 雷区 / 事实 | `memory/semantic_memory.db` | `MemoryWriter`（异步 + 证据校验） |
| 经验 | 失败诊断、成功模式、待审补丁 | `memory/failures`、`memory/successes`、`memory/skill_patches` | `ExecutionCritic`、`Reflect` |
| 台账 | 技能通过率（验收 + 线上被纠正） | `memory/skills/quality.jsonl` | `SkillQualityStore` |

> 画像的注入方式是"常驻"而不是"按需召回"，因为画像描述的是**这个人是谁**
> （说话方式、习惯、做事方法），等 query 命中才注入就晚了。
> 画像同时进入 **L2 规划提示词**（影响调哪个工具）和 **回答生成的 system prompt**
> （影响怎么说话）—— 后者是画像真正生效的地方。

### 会话恢复

```
浏览器                                        服务端
  │ localStorage['skill_agent_session']         │
  │      .session_id                            │
  ├─────── init { session_id } ─────────────────→│
  │                                              │ 复用该会话（内容恢复）
  │←──── connected { session_id } ───────────────┤
```

**本地单用户**：只有使用者本人，不存在越权读取他人会话的问题。

### 相关位置

- `core/memorykit/` —— **三层门面与职责契约（改记忆先看这里）**
- `core/memorykit/working.py` —— 工作记忆（轮级工作台，contextvar 暴露给工具/子智能体）
- `core/session_memory.py` —— 短期记忆的真实存储（消息 / 摘要 / 检索）
- `core/session_rollup.py` —— 滚动摘要
- `core/session_config.py` —— 恢复条数 / 滚动阈值
- `core/semantic_memory.py` + `core/memory_db.py` —— 长期记忆·画像
- `core/memory.py` + `core/skill_quality.py` —— 长期记忆·经验与台账
- `core/context.py` —— `sink` + `summary`（工作上下文，**不是**记忆）
- `core/agent.py` —— 三层的调用编排（`begin_turn` / `sink` / `profile_block` / `end_turn`）

### 相关 API

- `GET /api/sessions`
- `GET /api/sessions/{session_id}/messages`
- `DELETE /api/sessions/{session_id}`
- `GET /api/memories`
- `DELETE /api/memories/{memory_id}`

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

## 7.1 身份与鉴权

### 管理接口

- 本地单用户，**管理接口无鉴权**（`require_auth` 保留为 no-op 装饰器）
- 删除技能 / 审批补丁 / 切换特性开关 / 复盘请求 —— 直接调用

### WebSocket 会话身份

- `client_id` 由**服务端签发**：客户端自带的 `conn_id` 只是消息路由句柄
- `init` 时服务端签发 `session_id` / `client_id`
- `session_id` 存在浏览器 `localStorage`，重连时带回即可续上会话
- `chat` / `reset` / `feedback` 直接放行（本地单用户，无令牌）

### 为什么没有用户身份

项目跑在使用者本地，只有他一个人。因此**没有多用户隔离、没有令牌、没有登录**：

| | 原多用户设计 | 现状 |
|---|---|---|
| 身份 | `user_id` + `tenant_id` | 只有 `session_id` |
| 令牌 | 会话令牌 + 长期用户令牌（HMAC） | 无 |
| 假用户服务 | `core/user_service.py` + `POST /api/users` | 全部删除（含 `/api/users` 端点） |
| 管理接口鉴权 | `OWNER_TOKEN` | 无（`require_auth` 为 no-op） |
| 记忆隔离 | 按 `user_id` 过滤 | 按 `scope` + `agent_id` 分层 |
| 执行目录 | 按 `user_id + session_id` | 按 `session_id` |

**保留的是多智能体维度**：智能体私有记忆、工具归属（`owner_agent_id`）、
执行目录隔离都还在 —— 它们与用户数无关。剥离的只是"用户"这一个维度。

## 8. 当前设计取舍

### 已完成的收口

- 同步入口与异步主链路统一（`Agent.chat()` 复用同一事件循环）
- 配置启动失败直接中止
- 搜索工具不再阻塞事件循环
- 补丁审批从“内存修改”改为“文件持久化修改”
- 补丁审批从“原地改写 YAML”改为“生成新版本 + 切 active”
- 补丁发布前跑回归样例（`core/result_validator.py`），自动发布还需写审计记录
- 每次 `handle()` 生成唯一 `execution_id`，失败记录不再互相覆盖
- 规划器工具枚举从 `ToolHub` 动态生成
- WebSocket 连接由服务端签发 `client_id` 作为路由句柄，**不校验令牌**（本地单用户）
- 会话跨连接稳定：`session_id` 由客户端带回，服务端据此恢复会话内容
- 意图识别的三处关键词误伤已修（结构化正则 + 元问题拦截）
- 意图识别已分层：L0 守卫层（0 LLM）→ L1 闭集判别（一次 LLM 调用）→ L2 规划（不再输出意图）
  → L3 澄清（意图不明时问一句 + 候选项）；L1 低置信升级 L2 兜底，分类失败不拖垮链路
- 会话内容全量落库，服务重启 / 浏览器刷新后可继续问答（短期记忆）
- 用户画像（长期记忆）已接入主链：**LLM 抽取（证据校验）** → 去重/冲突 → 入库 → **每轮常驻注入**；
  混合检索用 RRF（本地单用户，无 `user_id` 过滤）
- 画像进 **回答生成的 system prompt**（不只是规划器）—— 说话方式偏好这才真正生效
- 长期记忆检索对中文可用：FTS5 切不开汉字，另有一条 **CJK 二元组通道**一起进 RRF
- 向量通道**默认关闭**：没配 `EMBEDDING_PROVIDER` 就只用 FTS + CJK；
  合成向量（`mock`）会被类型级拒绝 —— 随机向量做语义检索比不检索更差
- 每轮分阶段耗时落盘（`core/trace.py`），`scripts/trace_report.py` 汇总，`GET /api/traces` 查询
- SQLite 统一走 `core/sqlite_util.py::connect()`：WAL + `busy_timeout`，并发写不再锁库
- 「忘掉这件事」软删除并同步清 FTS 索引，删掉的不再被召回
- 自演化面板走真实 API
- **用户纠正信号已接通**（自演化闭环的关键）：
  - 两条入口走同一条链路 —— 隐式（`core/correction.py` 纠正语检测，关联上一轮 `execution_id`）
    与显式（前端 👍/👎 → WS topic `feedback/<client_id>`）
  - 纠正触发**上一轮重评**：撤销它的"成功"记录、记为失败、生成改进补丁
  - 新指标**答案采纳率** `adoption_rate`（`GET /api/memory/stats`）——
    比只看工具成功率的 `success_rate` 诚实得多
  - 纠正检测是纯规则：误判会污染长期记忆，宁可漏判也不用 LLM
- 回答出口统一兜底：上游返回空内容时给明确失败说明，不给用户空气泡
- **技能质量台账**（`core/skill_quality.py`）：验收样例（离线）+ 线上被纠正（真实）
  两类信号合账 → 低通过率技能自动降级，不再被 planner 选中；
  降级是推导出来的，数据变好自动恢复
- **能执行代码**：`python_exec` 子进程隔离（独立 cwd / `-I` / env 白名单 / 超时 / AST 黑名单）；
  `write_script` 持久化脚本。**子进程隔离是崩溃隔离不是安全边界**，
  多租户对外开放前必须关掉 `CODE_EXEC_ENABLED` 或换容器隔离
- **用户可见可管理自己的长期记忆**（「记忆」面板）:
  列出、分类、逐条忘掉、全部忘掉;同时显示正在注入的内容。
  **没有用户隔离维度**：本地只有一个人，按 `session_id` 与 `scope` 分层
- **能读文件**：`read_file` 读文本文件（源码/配置/日志），返回带行号的内容；
  单文件上限 10MB，超了明确报错而不是截断当全文；
  大文件分段读；凭据类文件里的密钥值打码后再返回
- **能读文档并引用原文**：`read_document` 返回带锚点的 markdown
  (锚点带页码/表名/单元格),再用 `locate_document` 反查原文;
  扫描件 PDF 明确报错而不是返回空
- **MCP 工具源**：配 `MCP_SERVERS` 即可接入现成工具器；单服务器失败不影响启动
- **意图只由 LLM 判**：规则退到「结构守卫」（只判空输入）。
  实测 130 条：规则+LLM 94.7% vs 只有 LLM 97.7% —— 用规则猜说法是在拖低准确率
- **有真机基线了**（DeepSeek `deepseek-flash`）：L1 意图开发集 38 条 100%、
  留出集+封存集 96.3%；画像抽取精确率 100%（CI 闸门 ≥95%）；
  基线按**分布**报（多遍跑，取最差一遍做闸门），不是单次数字
- **事件名有唯一来源**：`core/events.py` → 生成 `frontend/src/events.js`，
  后端只写常量、前端只用 `EV.*`；四道契约测试守着（见 §8.5）
- **工具参数有校验边界**：`tools/validation.py` 在 `hub` 执行前校验必填 / 未知 /
  类型 / enum，未知参数给近似候选（`file_path` → `path`），不再"静默空转"
- **前端只有一个 HTTP 出口**：`frontend/src/api/client.js`（超时 / ApiError / 具名接口），
  页面里不再有裸 `fetch`
- **会话串行化**：`Session.submit()` 单队列 + worker；重连复用同一个 Agent
  （`session_id → Session` 索引），不再出现"同 session 两个 Agent 同时写库"
- **工程质量有闸门**：ruff（E4/E7/E9/F）零告警；`requirements.txt` 有上界 +
  `requirements.lock`；`except Exception` 走棘轮（基线 368，只降不升）

### 仍然存在的限制

- `docs/` 下的细分设计文档仍有部分历史描述，需继续同步
- `frontend` 没有端到端自动化测试
- 长期记忆面板已做（`MemoryPanel.vue` 接 `GET /api/memory/profile` 与 `/api/memory/overview`），
  但**没有**「这条记忆是从哪句话来的」溯源展示
- 用户画像抽取是 **LLM + 证据校验**（`core/memory_writer.py` + `core/profile_llm.py`），
  不是纯规则；召回率的天花板在于"用户没说出口的偏好"抽不出来
- 记忆三层已收口到 `core/memorykit/`（调用方只认三个门面）。
  底层是**两种载体、互斥职责**：画像走 SQLite（`MemoryDB`），经验走 JSON
  （`MemoryStore`）。这不是"同一份数据的两个副本"，所以不做合并；
  代价是"查全部经验"与"查全部画像"是两个入口，`long_term` 门面负责收口
- `Agent.handle()` 仍是长函数（345 行，原 494）：准备 / 事件 / 流式 / 快捷意图
  四个 stage 已抽出；主执行段（目标循环 + DAG）还耦合在一起，
  要彻底切分需先引入 `TurnContext` 对象
- `ManagerAgent` 948 行（原 1190）：提示词（`agents/prompts.py`）、判别层
  （`agents/intents.py`）、数据类型（`agents/manager_types.py`）已拆分；
  `plan()` 仍是 L0→L1→L2 的编排核心
- 经验存储（JSON）到 SQLite 的迁移方案与脚本已就绪（`scripts/migrate_experience_to_sqlite.py`），
  但数据未迁移、读写未切换，见 `docs/20`
- `core/agentkit` 已收口：主链**直接**引用 `roles` / `sources`，`context` / `memory`
  经 `sources` 间接使用，`identity` / `brief` / `blackboard` / `messaging` 是契约层。
  接线表在 `core/agentkit/__init__.py`，由 `test_declared_wiring_matches_reality` 守着；
  多智能体协作能力仍未全部启用（当前主链只用单角色 + 事件推送）
- **自演化质量信号已接通**，但**只有纠正信号**：
  `user_feedback` 现在会被写入（隐式纠正语 + 前端 👍/👎），critic 会把被纠正的轮次记成失败；
  见 `docs/archive/14` §2.3
- **技能质量台账已建**，但验收样例仍由"提补丁的人"自己给，
  缺独立于实现者的标准；见 `docs/archive/14` §2.4
- **真模型基线已建立，但只覆盖意图与画像**：L1 意图（开发集 100% / 留出集 94.7%）、
  画像抽取（精确率 100%）。**答案质量与澄清质量仍没有基线** —— 那要有标注问答对；
  且基线是单模型的，换模型必须重跑；见 `docs/archive/14` §4.1
- L1 判别每轮多一次 LLM 调用（主路径 +1 轮往返），可用 `INTENT_MODEL` 指向更快的模型
- 会话内检索是 Python 侧 CJK 二元组打分，十万级消息后需要换 `sqlite-vec` 之类
- 第三方 WebSocket 客户端必须回 RPC ACK（服务端事件推送是请求/响应式的），
  服务端另有 `PUSH_TIMEOUT=10s` 兜底，但客户端不回 ACK 时每条事件会多等一个超时


## 8.5 事件契约（前后端唯一真相）

事件名以前散在十几个文件里各写一遍字面量：后端 `emit("thinking")`、前端
`wsService.on('thinking')`。改一个名字要全仓搜，**漏掉任何一端都不报错** ——
只是那个面板从此不动了，而且没人知道为什么。

现在只有一个来源：

```text
core/events.py  ──(scripts/gen_events_js.py)──>  frontend/src/events.js
      │                                                  │
      │ 后端：emit(EV_THINKING, ...)                      │ 前端：wsService.on(EV.THINKING, ...)
      └──────────────────────────────────────────────────┘
                     tests/test_event_contract.py 守着
```

- **后端**只能 `from core.events import EV_*`，不允许写字面量；
- **前端**只能用 `EV.*`（`events.js` 由脚本生成，禁止手改）；
- 事件分两类：`STREAM_EVENTS`（推前端）与 `AUDIT_EVENTS`（只落 `memory/audit/`）。

测试有四道闸，任何一端偷写新名字都会红：

1. 后端出现的字面量事件名必须在 `core/events.py` 里；
2. 前端出现的事件名必须在契约里；
3. `EV.XXX` 引用的常量必须真实存在 —— **JS 里 `on(undefined)` 不报错，
   只会让监听永远不触发**，这是比写错字符串更隐蔽的失效；
4. `events.js` 必须与重新生成的结果逐字节一致（防止手改或忘了重新生成）。

CI 里也跑 `python scripts/gen_events_js.py --check`。

## 8.6 异常分类约定

宽捕获 `except Exception` 不是问题本身 —— **静默吞掉**才是。
统一按三类处理（`core/exception_policy.py` 是"降级"这一类唯一的落点）：

| 类别 | 什么时候用 | 怎么写 | 例子 |
|---|---|---|---|
| **可降级** | 外部依赖 / 观测 / 缓存失败，失败不影响主链路 | `except Exception as exc: downgrade(exc, where=...)` —— 记 debug 后走默认值 | 画像读取失败用空串、审计写失败不影响业务 |
| **必须冒泡** | 数据完整性 / schema 不一致 / 配置错误 | 捕获窄异常类型后 `raise`，或干脆不捕获 | JSON 解析失败、DB 行与 FTS 索引不一致 |
| **必须重试** | 网络 / 子进程 / 临时 I/O | 交给该层已有的重试逻辑，不在业务层假装成功 | 工具调用重试、Provider 请求重试 |

**硬约束**：不允许 `except Exception: pass`（静默吞）。
这类写法已全部替换为 `downgrade()`；CI 有检查，`scripts/check_except_baseline.py`
保证总数只降不升，新增一处就得说明理由。

注意 `downgrade()` 记的是 **debug** 而不是 warning：这些降级点是设计意图，
用 warning 会淹没真正的问题；但完全静默会让"功能悄悄失效"无从排查。

## 9. 目录摘要

```text
backend/      FastAPI、Session、WebSocket
frontend/     Vue 3 前端（HTTP 统一走 frontend/src/api/client.js）
agents/       三个业务 Agent + Trainer；agents/prompts.py 是规划提示词与 schema
core/         Agent、Context、DAG、Memory、SessionMemory、Critic、Reflect
core/events.py   事件契约（前后端唯一事件名来源）
core/memorykit/  记忆三层门面（工作 / 短期 / 长期）与职责契约
skills/       技能模型、加载、注册、存储
tools/        工具实现与 ToolHub（tools/validation.py 是参数校验边界）
infra/        配置、Provider、日志
scripts/      gen_events_js.py（生成前端事件常量）、check_except_baseline.py（异常棘轮）
ui/           Streamlit / CLI
tests/        自动化测试
docs/         细分设计文档（00-总览里标注了哪些是历史快照）
```
