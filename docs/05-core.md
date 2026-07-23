# Core 模块

`core/` 负责把多个 Agent、上下文、记忆和反思链路串起来。

## 1. Context

文件：`core/context.py`

`Context` 是当前多轮对话的内存对象。

主要能力：

- 记录 `user` / `assistant` / `system` / `tool` 消息
- 保存上一轮 `tool_tasks`
- 生成 LLM 所需消息格式
- 按字符预算截断历史

当前作用点：

- `Agent.handle()` 在入口和出口都写入 `Context`
- `ManagerAgent` 和 `OrchestratorAgent` 会把历史拼到 prompt
- `retry` 意图依赖 `Context.get_last_tool_tasks()`

## 2. Agent 运行协调

文件：`core/agent.py`

`Agent` 不负责具体算法，而负责协调：

- 意图规划
- DAG 执行
- 响应合成
- 事件回调
- 执行评估

当前事件回调机制支持同步和异步两种 `on_event`。

### 2.1 执行标识（M0-01）

文件：`core/identity.py`

`IdentityContext` 统一四级标识 `user_id / session_id / turn_id / execution_id`，详见 [02-agents.md §1.1](./02-agents.md#11-四级执行标识m0-01)。

`Agent._new_execution()` 在每次 `handle()` 入口创建上下文；`_new_child_execution(parent)` 在重试时派生子 execution 并保留 `parent_execution_id`。

### 2.2 主链路写入 Episode（M2-03）

`Agent._maybe_record_episode()` 把一次执行打包成 `EpisodeRecord` 写入 `MemoryRepository.add_episode()`：

- 文件名按 `execution_id` 命名（`memory/episodes/{execution_id}.json`），不再互相覆盖
- 字段包含四级标识 + `selected_skill` + `selected_skill_version` + `success_rate` / `fallback_count` / `retry_count` / `latency_ms` + `tool_attempts`（每个工具的真实名称 / 成功 / 重试次数 / latency / 错误）
- `EpisodeRecord` 兼容旧 `FailureRecord` 字段名（`id` 属性返回 `ep-{execution_id}`）

## 3. 执行批评器

文件：`core/critic.py`

`ExecutionCritic` 在主回复结束后异步运行。

当前职责：

- 统计成功率
- 记录失败
- 记录成功路径
- 生成待审批 patch
- 在用户纠正时记录修正建议
- 高置信度 patch 标记 `auto_approved`，但仍进入待审队列（M3-06，不再直接落盘）

当前数据去向：

- 失败记录写入 `memory/failures/`
- 成功记录写入 `memory/successes/success_index.jsonl`
- 补丁写入 `memory/skill_patches/pending/`（含 `auto_approved`）

### 3.1 统一 patch schema（M3-01）

patch 字段统一为：

- `target_skill`：目标技能名
- `version_target`：目标版本号（如 `1.0.1`）
- `diff`：方法/字段层面的差异描述
- `recommendations`：建议列表
- `evidence_execution_id`：触发该 patch 的执行 id（可回溯到具体执行）

### 3.2 ResultValidator（M3-03）

文件：`core/result_validator.py`

`ResultValidator.validate(skill, final_output, user_input)` 在执行完成后判断最终输出是否真正满足用户目标：

- `non_empty` / `min_length`：输出非空且不过短
- `capability_coverage`：从 `skill.capability` 提取关键词，检查输出是否覆盖
- `method_steps`：从 `skill.method` 提取步骤标记，检查覆盖率（< 34% 视为不通过）
- `examples_style`：输出与至少一个 `examples` 有关键词重叠

返回 `ResultValidation(passed / score / issues / checks)`。没有工具的任务不再无条件得 100%。

### 3.3 FeedbackEvent（M3-02）

文件：`core/feedback.py`

`FeedbackEvent` 绑定 `execution_id`，确保用户纠正可回溯到具体执行：

- `type`：`accept / reject / correction / retry / rating`
- `content`：反馈正文
- `rating`：可选 1~5 评分
- `user_id` / `session_id`：用于会话/记忆隔离

`FeedbackStore` 持久化到 `memory/feedback/{execution_id}__{feedback_id}.json`，并维护 `_index.json` 按 `execution_id` 索引。`correction` 类型反馈会触发 patch 生成（在 backend API 中处理）。

## 4. MemoryRepository（M2-01 / M2-02）

文件：`core/memory_repository.py`

`MemoryRepository` 是统一长期记忆访问层。所有上层代码（`MemoryStore` / `SemanticMemory` / `Critic` / `Agent`）不再直接读写 JSON / JSONL / SQLite，全部通过这一层访问。

### 4.1 MemoryItem 数据模型（M2-02）

字段遵循 `10-目标架构评审与演进方案.md §6.2`：

| 字段 | 说明 |
|---|---|
| `user_id` / `project_id` / `session_id` | 作用域隔离（单用户场景，已移除 tenant_id） |
| `turn_id` / `execution_id` / `source_turn_id` | 追溯到具体执行 |
| `scope` | `global / user / project / session` |
| `type` | `fact / preference / context / episode / lesson / skill_hint` |
| `content` / `structured_value` | 文本内容 + 结构化值 |
| `confidence` | 置信度 0.0~1.0 |
| `sensitivity` | `normal / secret / pii`（写入时自动检测敏感词） |
| `valid_from` / `valid_until` | 时效窗口 |
| `supersedes_id` / `status` | 冲突替代指针 + `candidate / active / rejected / expired` 状态 |
| `tags` / `metadata` / `embedding` | 标签、元数据、向量 |

### 4.2 主要 API

- `add_memory_item(item)`：写入（含敏感检查 + 冲突检测）
- `recall(query, user_id, project_id, type, limit)`：混合召回（FTS5 + 向量），按 `user_id` 作用域过滤 + 作用域/置信度/时效排序
- `recall_strings(query, ...)`：召回并格式化为带类型标签的字符串列表
- `list_memory(user_id, scope, type, limit)`：列出
- `get_memory(item_id)` / `delete_memory(item_id, user_id)` / `forget_user(user_id)`
- `add_episode(ep)` / `list_episodes(user_id, limit)` / `get_episode(execution_id)`
- `set_embedding_service(service)`：M2-04 绑定 embedding service；失败时 `_embedding_failed=True`，降级为全文检索
- `cleanup()` / `get_stats()`

### 4.3 存储分层

- 短期/失败/成功/episode：JSON（`memory/episodes/`）
- 语义/偏好/项目事实：SQLite + FTS5 + 嵌入向量（`memory/semantic_memory.db`）
- 旧 `SemanticMemoryStore` 表兼容：`_MemoryDB._init()` 自动 `ALTER TABLE` 补齐缺失列

### 4.4 MemoryStore（旧 API 委托）

文件：`core/memory.py`

`MemoryStore` 继续可用，但内部委托到 `MemoryRepository`。保留 `record_failure()` / `record_success()` / `get_pending_patches()` / `approve_patch()` / `reject_patch()` / `get_stats()` 等旧 API，避免破坏现有调用方。

## 5. 自反思循环

文件：`core/reflect.py`

`SelfReflectLoop` 是后台反思任务。

触发方式：

- 后端启动后自动开启，前提是 `self_evolution_enabled=true`
- 前端调用 `POST /api/reflections/request`

当前触发条件：

- 最近失败数达到阈值
- 同一场景重复失败达到阈值
- 用户主动请求反思

## 6. 其他核心模块

- `core/dag.py`
  把技能步骤翻译为可执行 DAG
- `core/intent_detector.py`
  规则优先的意图识别
- `core/semantic_memory.py`
  语义记忆，委托到 `MemoryRepository.memory_db`（M2-01 之后）
- `core/merger.py`
  技能合并尝试，当前不是主链路核心依赖

## 7. 当前缺陷

- `ExecutionCritic` 的上下文摘要仍偏粗糙
- `Context` 只在单进程内存中保存，重启后丢失（Session 摘要 M2-07 仅折叠历史，不持久化）
- `SelfReflectLoop` 仍偏规则驱动，缺少成熟的评估闭环
- `core/` 下存在部分实验性模块，边界还不够清晰
