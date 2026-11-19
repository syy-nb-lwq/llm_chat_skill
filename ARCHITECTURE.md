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
  - `init/{conn_id}` → 服务端签发身份（`user_id` / `session_id` / `client_id`）+ 签名令牌
  - `chat/{conn_id}` → 必须带令牌
  - `reset/{conn_id}` → 必须带令牌
  - `ping/{conn_id}` → 心跳，不要求令牌

### 2.2 会话层

- `backend/session.py` 按**服务端签发的 `client_id`** 持有 `Session`
- `SessionManager.issue_identity()` 维护 `conn_id → client_id` 绑定，
  `resolve_identity()` 校验令牌后才放行
- 每个 `Session` 内含一个 `Agent`（带签发的 `user_id` / `session_id`）
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

## 6.1 记忆分层

记忆按**"记的是什么"**分两层，不是按存储时效性分：

| 层 | 内容 | 存储 | 注入方式 |
|---|---|---|---|
| **短期** | 会话内容（用户与助手说了什么） | `memory/sessions.db` | 摘要 + 最近 N 轮 |
| **长期** | 用户画像（习惯 / 偏好 / 做事方法） | `memory/semantic_memory.db` | **常驻注入**（≤400 字符，进 system prompt） |

会话内容的取舍是：**全量保留，不设过期、不删除**。存储与注入分开 ——
存储一直留着，注入时才按 token 预算裁剪成"摘要 + 最近 N 轮"。

> 长期记忆的注入方式是"常驻"而不是"按需召回"，因为画像描述的是**这个人是谁**
> （说话方式、习惯、做事方法），等 query 命中才注入就晚了。
> 画像同时进入 **L2 规划提示词**（影响调哪个工具）和 **回答生成的 system prompt**
> （影响怎么说话）—— 后者是画像真正生效的地方。

### 写入与读取

```
用户消息 ──┐
           ├─→ Context.add_message ─→ sink ─→ session_messages（全量）
助手消息 ──┘                              │
                                          ├─→ 滚动摘要（超过阈值时）
                                          └─→ 会话内检索（CJK 二元组打分）

每轮调 LLM：system + 滚动摘要 + 最近 N 轮原文 + 本轮输入
```

### 会话恢复

```
浏览器                                        服务端
  │ localStorage['skill_agent_session']         │
  │      .session_id                            │
  ├─────── init { user_token, session_id } ─────→│
  │                                              │ 会话属于该 user_id ?
  │                                              │   是 → 复用（内容恢复）
  │                                              │   否 → 新建会话
  │←──── connected { session_id, token } ────────┤
```

**归属校验是硬要求**：会话内容是用户数据，仅凭 `session_id` 就给读等于越权。

### 相关位置

- `core/session_memory.py` —— `SessionMemoryStore`（消息 / 摘要 / 检索）
- `core/session_rollup.py` —— 滚动摘要
- `core/session_config.py` —— 恢复条数 / 滚动阈值
- `core/context.py` —— `sink` + `summary`
- `core/agent.py` —— `persist_session` 开关与恢复逻辑

### 相关 API

- `GET /api/sessions`
- `GET /api/sessions/{session_id}/messages`
- `DELETE /api/sessions/{session_id}`

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

- `infra/auth.py` 提供 `require_auth` 装饰器，保护管理类接口
  （删除技能 / 审批补丁 / 切换特性开关 / 复盘请求）
- `OWNER_TOKEN` 未配置时跳过校验（单机个人环境的默认行为）
- 配置后请求必须带 `Authorization: Bearer <token>`；前端通过 `localStorage`
  里的令牌自动附带请求头

### WebSocket 会话身份（C-01）

- `client_id` 由**服务端签发**：客户端自带的 `conn_id` 只是消息路由句柄
- `init` 时服务端签发 `user_id` / `session_id` / `client_id`，并下发 HMAC 签名令牌
  （`base64url(payload).base64url(HMAC-SHA256)`）
- `chat` / `reset` 必须携带该令牌；缺失、过期或被篡改一律拒绝
- 签名密钥取 `SESSION_SECRET`，未配置时使用进程级随机密钥
  （进程重启后旧令牌失效，前端重连时自动重新 `init`）
- 签发的 `user_id` 会传到 `Agent`，为跨用户记忆隔离打底

### 用户身份（假用户服务）

会话身份是**一次连接**的；用户身份是**这个人**的。两者用不同的令牌承载：

| | 会话令牌 | 用户令牌 |
|---|---|---|
| 生命周期 | 一次 WS 连接 | 长期 |
| 绑定 | `user_id + session_id + client_id` | `user_id + tenant_id` |
| 用途 | 证明"这条连接是谁" | 证明"这个人还是那个人" |
| 过期 | 随连接断开失效 | 默认不过期 |

- `core/user_service.py` 负责签发/解析用户令牌、维护用户档案（落盘 `memory/users/<id>.json`）
- `init` 时客户端回传 `user_token`：有效 → 认出同一个用户；无效/为空 → 新建用户
- 前端把 `user_token` 存在 `localStorage`，下次连接自动带上
- **修掉的缺陷**：改造前每次 WS 连接都新生成 `user_id`，浏览器刷新一次就是一个新用户，
  长期记忆会按连接碎片化、用户画像永远积累不起来
- `POST /api/users` 可签发新用户与令牌 —— 用来模拟多用户、验证记忆隔离
- 将来接入真实登录时，只需把 `UserService.get_or_create()` 换成"从登录态取 user_id"，上层无感

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
- WebSocket 身份由服务端签发，令牌校验后才放行
- 用户身份跨连接稳定（假用户服务 + 长期用户令牌）
- 意图识别的三处关键词误伤已修（结构化正则 + 元问题拦截）
- 意图识别已分层：L0 守卫层（0 LLM）→ L1 闭集判别（一次 LLM 调用）→ L2 规划（不再输出意图）
  → L3 澄清（意图不明时问一句 + 候选项）；L1 低置信升级 L2 兜底，分类失败不拖垮链路
- 会话内容全量落库，服务重启 / 浏览器刷新后可继续问答（短期记忆）
- 用户画像（长期记忆）已接入主链：**LLM 抽取（证据校验）** → 去重/冲突 → 入库 → **每轮常驻注入**；
  混合检索用 RRF，`user_id` 强制过滤
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
  **隔离是强的**:`user_id` 只从令牌里取,不信任 query 参数
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

### 仍然存在的限制

- `docs/` 下的细分设计文档仍有部分历史描述，需继续同步
- `frontend` 没有端到端自动化测试
- 长期记忆**前端面板**未做（「你记住了我什么」目前只有 `GET /api/memories`）
- 用户画像抽取**只用规则**（`core/memory_writer.py`）—— 召回率有上限，
  用户显式说"记住…"能兜住一部分；见 `docs/13` D14
- 统一存储层（Repository 协议）未做：`MemoryStore`(JSON) 与 `MemoryDB`(SQLite) 仍是两套
- **自演化质量信号已接通**，但**只有纠正信号**：
  `user_feedback` 现在会被写入（隐式纠正语 + 前端 👍/👎），critic 会把被纠正的轮次记成失败；
  见 `docs/14` §2.3
- **技能质量台账已建**，但验收样例仍由"提补丁的人"自己给，
  缺独立于实现者的标准；见 `docs/14` §2.4
- **真模型基线已建立，但只覆盖意图与画像**：L1 意图（开发集 100% / 留出集 94.7%）、
  画像抽取（精确率 100%）。**答案质量与澄清质量仍没有基线** —— 那要有标注问答对；
  且基线是单模型的，换模型必须重跑；见 `docs/14` §4.1
- L1 判别每轮多一次 LLM 调用（主路径 +1 轮往返），可用 `INTENT_MODEL` 指向更快的模型
- 会话内检索是 Python 侧 CJK 二元组打分，十万级消息后需要换 `sqlite-vec` 之类
- 第三方 WebSocket 客户端必须回 RPC ACK（服务端事件推送是请求/响应式的），
  服务端另有 `PUSH_TIMEOUT=10s` 兜底，但客户端不回 ACK 时每条事件会多等一个超时


## 9. 目录摘要

```text
backend/      FastAPI、Session、WebSocket
frontend/     Vue 3 前端
agents/       三个业务 Agent + Trainer
core/         Agent、Context、DAG、Memory、SessionMemory、Critic、Reflect
skills/       技能模型、加载、注册、存储
tools/        工具实现与 ToolHub
infra/        配置、Provider、日志
ui/           Streamlit / CLI
tests/        自动化测试
docs/         细分设计文档
```
