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

## 3. 执行批评器

文件：`core/critic.py`

`ExecutionCritic` 在主回复结束后异步运行。

当前职责：

- 统计成功率
- 记录失败
- 记录成功路径
- 生成待审批 patch
- 在用户纠正时记录修正建议

当前数据去向：

- 失败记录写入 `memory/failures/`
- 成功记录写入 `memory/successes/success_index.jsonl`
- 补丁写入 `memory/skill_patches/pending/`

## 4. MemoryStore

文件：`core/memory.py`

`MemoryStore` 是自演化链路的持久化层。

主要 API：

- `record_failure()`
- `record_success()`
- `get_recent_failures()`
- `get_skill_hints()`
- `get_pending_patches()`
- `approve_patch()`
- `reject_patch()`
- `get_stats()`

当前特点：

- 按月分目录保存失败记录
- 成功记录走 JSONL
- pending patch 走独立目录

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
  语义记忆，可选能力，受 feature flag 控制
- `core/merger.py`
  技能合并尝试，当前不是主链路核心依赖

## 7. 当前缺陷

- `ExecutionCritic` 的上下文摘要仍偏粗糙
- `Context` 只在单进程内存中保存，重启后丢失
- `SelfReflectLoop` 仍偏规则驱动，缺少成熟的评估闭环
- `core/` 下存在部分实验性模块，边界还不够清晰
