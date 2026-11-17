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
- 技能更新生成新版本并切 `active`，旧版本保留可回滚
- 工具层已统一到 `ToolHub`
- `weather_query` 与 `web_search` 都是异步实现
- 自演化面板已接入真实后端接口，不再依赖前端本地假状态
- 每次 `handle()` 生成唯一 `execution_id`，同 session 多轮失败记录不互相覆盖
- 规划器的工具枚举从 `ToolHub` 动态生成，MCP / 自注册工具可被选中
- 管理接口支持 `OWNER_TOKEN` 鉴权（未配置时保持开放）
- WebSocket 身份由服务端签发，`client_id` 不再由客户端自带；令牌缺失或伪造一律拒绝
- 假用户服务签发长期用户令牌，同一浏览器始终是同一个 `user_id`（长期记忆可跨重启累积）
- 补丁发布前跑回归样例；自动发布还需通过静态校验并写审计记录
- 意图识别的三处关键词误伤已修（结构化正则 + 元问题识别）
- 意图识别已分层：L0 守卫层（0 次 LLM）→ L1 闭集判别（一次 LLM 调用，6 标签 + 置信度）
  → L2 规划（不再输出意图）→ L3 澄清（意图不明时问一句 + 候选项按钮）；
  低置信升级 L2 兜底，分类失败不拖垮链路
- 意图评估可量化：`scripts/intent_eval_nightly.py` 出混淆矩阵 + 审计文件
- **记忆分两层**（按"记的是什么"划分，不按时效）：
  - **短期记忆 = 会话内容**：全量落库、不设过期；服务重启 / 浏览器刷新后能接着聊
  - **长期记忆 = 用户画像**：`preference/habit/method/constraint/fact` 由规则抽取写入，
    **每轮常驻注入**（≤400 字符，进 **system prompt**，闲聊轮同样生效）；
    「忘掉这件事」软删除并同步清索引
- 长期记忆按 `user_id` 严格隔离：检索接口的 `user_id` 是必填参数（跨用户召回为 0）
- **检索链路对中文可用**：FTS5 切不开汉字，长期记忆另有一条 CJK 二元组通道
  （与 FTS 一起进 RRF 融合）
- **向量检索默认关闭**：没配 `EMBEDDING_PROVIDER` 就只用 FTS + 常驻注入 ——
  随机向量做语义检索比不检索更差，宁可不开
- **每轮分阶段耗时可观测**：`scripts/trace_report.py` + `GET /api/traces`
- **SQLite 走 WAL**：并发写不会 `database is locked`
- **「回答不对」进得去了**（自演化闭环的关键）：
  - 隐式：下一轮输入命中纠正语（不对/应该是/重来…）→ 自动关联上一轮 `execution_id`
  - 显式：前端每条回答有 👍/👎 按钮 → WS topic `feedback/<client_id>`
  - 纠正触发**上一轮重评**：撤销它的"成功"记录，记为失败并生成改进补丁
  - 新指标**答案采纳率** `adoption_rate = 1 − 被纠正轮数/评估轮数`（`GET /api/memory/stats`）
  - 纠正检测是**纯规则**（误判会污染记忆库，宁可漏判，不用 LLM）
- **技能也有可信度台账**（`core/skill_quality.py`）：
  - 两类信号合账：验收样例（离线）+ 线上被纠正情况（真实）
  - 低于阈值**自动降级** —— 不再被 planner 选中；数据变好自动恢复
  - `GET /api/skills/quality` 看每个技能的通过率，前端显示「通过率 xx%」徽章
- **能写脚本、能跑脚本**（`python_exec` / `write_script`）：
  - 子进程隔离执行：独立 cwd、`-I` 隔离模式、**env 白名单(子进程拿不到 API key)**、超时、AST 静态黑名单
  - **子进程隔离 = 崩溃隔离,不是安全边界**——多租户对外开放前必须关掉 `CODE_EXEC_ENABLED` 或换容器隔离
- **能读文档，能引用原文**（`read_document` / `locate_document`）：
  - docx / xlsx / pptx / pdf / md / csv / html → 带锚点的 markdown
  - **锚点带位置**（如 `[B7 p.3]` / `[B2 数据|A1:C3]`）——LLM 引用后用户可核对
  - **扫描件 PDF 明确报错**（没有文本层）,不会返回空内容让模型瞎猜
- **能接 MCP 工具源**（现成的文件/网页/浏览器/Git/数据库工具）：
  - 配置 `MCP_SERVERS` 即可用;单个服务器连不上不影响其它也不影响启动
  - MCP 服务器是第三方进程 → **只应由管理员配置**
- **意图识别只由 LLM 判定，不用规则猜说法**：
  - 实测（130 条标注）：规则+LLM **94.7%** vs 只有 LLM **97.7%** —— 规则在拖低准确率
  - 规则退到「结构守卫」：只判空输入，其余全部走 L1（`core/llm_judge.py`）
  - 架构契约由 `tests/test_intent_architecture.py` 守着，防止规则偷偷回来
- **有真机基线了**（DeepSeek `deepseek-flash`）：
  - L1 意图：开发集 38 条 **100%**，留出集+封存集（95 条）**96.3%**
  - 画像抽取 `scripts/profile_eval.py`（纯规则）：精确率 **100%**、召回率 100%
  - 基线是**分布**：`--repeat` 多跑几遍，用最差的一遍做闸门

测试状态：

- `620 passed, 3 skipped`（skipped 为 `--live` 意图评估集的 LLM 通道，需真实 key）

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

可选：给管理接口（删除技能 / 审批补丁 / 切换开关）开启鉴权：

```env
OWNER_TOKEN=your-owner-token
```

配置后这些接口必须带 `Authorization: Bearer <token>` 请求头；前端可在「进化」面板里填写该令牌（保存在 `localStorage`）。不配置则保持开放，适合单机个人环境。

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
  - 四层意图编排：L0 守卫层 → L1 闭集判别 → L2 规划 → L3 澄清
  - LLM 规划只负责"怎么做"，不再输出意图
- `core/intent_labels.py`
  - 意图闭集（6 标签）与任务子类（6 种）、标签定义 / 正例 / 易混反例
- `core/llm_judge.py`
  - L1 闭集分类器：一次 LLM 调用，输出标签 + 置信度 + 槽位
- `core/intent_detector.py`
  - L0 守卫层：结构化规则，只"证明强意图"，不定案就交 L1
- `core/clarifier.py`
  - L3 澄清层：意图不明时生成**一个**带候选项的问题（连续澄清上限 1 轮）
- `core/memory_writer.py`
  - 用户画像写入管线：规则抽取 → 敏感检查 → 去重/冲突 → 入库（0 次 LLM）
- `core/semantic_memory.py`
  - 长期记忆混合检索（向量 + FTS + CJK，RRF 融合）、画像常驻注入块、遗忘
- `core/text_match.py`
  - CJK 二元组切分与命中打分（FTS5 切不开汉字，长期记忆检索靠它）
- `core/trace.py`
  - 每轮分阶段耗时记录（plan / tools / generate / first_token / total）
- `core/sqlite_util.py`
  - SQLite 连接工厂：WAL + `busy_timeout`（多用户并发写的前提）
- `core/correction.py`
  - 用户纠正检测（纯规则）：识别「不对/应该是/重来/太啰嗦」等纠正语，
    并要求存在上一条非空回答才判定为纠正
- `core/skill_quality.py`
  - 技能质量账本：验收样例通过率 + 线上采纳率 → 低通过率技能自动降级
- `agents/learning.py`
  - DAG 执行
  - 重试、超时、变量替换
- `agents/orchestrator.py`
  - 流式响应
  - 上下文整合
  - **用户画像进 system prompt**（回答风格偏好在这里才真正生效）

### 能力层

- `skills/`
  - 技能加载、匹配、版本管理、持久化更新
  - 技能 YAML 可带 `regression:` 回归样例，发布新版本前用它验证没有退化
- `tools/`
  - `weather_query`
  - `web_search`
  - `ToolHub` 统一管理与关闭资源

技能 YAML 的回归样例格式：

```yaml
name: demo
version: 1.0.0
capability: 演示用能力描述
method: 演示方法论
patterns: [demo]
examples: [示例]
regression:
  - name: 北京明天天气
    input:
      user_input: "北京明天天气"
    expect:
      tool: weather_query   # 必须调用到的工具
      success: true         # 所有工具都应成功
      contains: ["天气"]     # 输出中应出现的子串
      output_keys: ["code"] # 工具结果 data 中应存在的键
```

### 前端

- `frontend/src/App.vue`
  - 聊天、流程、技能、补丁、进化面板整合
- `frontend/src/websocket.js`
  - `clientId`（连接句柄）持久化
  - 服务端签发的会话身份与令牌随 `chat` / `reset` 一起带上
  - `userToken`（长期用户凭证）持久化并在 `init` 时回传，服务端据此认出同一个用户
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

### 用户（假用户服务）

- `GET /api/users` —— 列出用户
- `POST /api/users` —— 新建用户，返回 `user_id` 与 `user_token`
- `DELETE /api/users/{user_id}` —— 删除用户档案（需 `OWNER_TOKEN`）

### 会话内容（短期记忆）

- `GET /api/sessions` —— 列出会话（可按 `user_id` 过滤）
- `GET /api/sessions/{session_id}/messages` —— 取会话消息（可按 `user_id` 过滤）
- `DELETE /api/sessions/{session_id}` —— 删除会话及其内容

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
- `Agent` 没有同步入口 `chat()`，CLI / Streamlit 实际不可用（已补适配层）
- `trace_id` 在 `__init__` 生成，同 session 多轮失败记录互相覆盖（改为每次 `handle()` 生成 `execution_id`）
- 执行记录缺真实工具名 / retry / fallback / latency（`build_execution_context` 支持 `task_specs`）
- 工具结果 `meta` 缺 `attempts` / `latency_ms` / `timeout_s` / `fallback_to`
- `infra/auth.py` 完全没接线，`Config` 里也没有 `owner_token` 字段（管理接口现在真的受保护）
- 规划 schema 硬编码工具枚举，MCP / 自注册工具无法被选中（改为从 `ToolHub` 动态生成）
- 补丁审批原地改写技能 YAML，破坏版本历史（改为生成新版本并切 `active`）
- 技能删除用通配符匹配，`delete demo` 会连 `demo2` 一起删掉（改为精确文件名匹配）
- `tests/conftest.py` 写入的是过期变量名 `LLM_API_KEY`，导致后端测试整片报错
- `_complete_teaching()` 把助手追问当用户消息回写 context，污染多轮教学
- `client_id` 由客户端自带并直接当会话键，任何人可冒用他人会话（改为服务端签发 + 令牌校验）
- 高置信度补丁跳过校验直接落盘（改为必须过静态校验 + 回归样例 + 审计记录）
- 补丁审批不跑新旧样例，新版本可能带病上线（审批前先在内存里算候选版本并校验）
- 每次 WebSocket 连接都重新生成 `user_id`，长期记忆按连接碎片化（引入假用户服务与长期用户令牌）
- 技能管理关键词裸子串匹配，`查看北京天气` 被判成技能管理（改为结构化正则）
- 教导关键词含 `要`/`遇到`/`以后` 等泛词，`遇到雨天要带伞` 被判成教导（改为结构化正则 + 排除规则）
- 工具关键词把 `搜索` 当强词，`搜索是一个动词吗` 被判成搜索任务（拆强弱关键词 + 元问题拦截）
- 会话内容只在内存里，服务重启 / 浏览器刷新后上下文全丢，"回来继续问答"无法实现
  （新增 `core/session_memory.py` 全量落库 + 会话恢复 + 归属校验）

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
core/       Agent、DAG、Context、Critic、Memory、SessionMemory
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
