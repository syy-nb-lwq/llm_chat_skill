# Agents 模块

本文说明当前各个 Agent 的职责与接口。

## 1. Agent 总入口

文件：`core/agent.py`

公开接口：

- `await Agent.handle(user_input, on_event=None)`
- `Agent.chat(user_input, on_event=None)`
- `Agent.reset()`

约定：

- `handle()` 是异步主入口
- `chat()` 是同步桥接层，供 CLI / Streamlit 使用
- `reset()` 只清空当前 `Context`

### 1.1 四级执行标识（M0-01）

文件：`core/identity.py`

`Agent.handle()` 在入口创建 `IdentityContext`，统一四级标识：

| 字段 | 含义 | 生成时机 |
|---|---|---|
| `user_id` | 用户身份 | 由调用方传入或服务端签发（C-01） |
| `session_id` | 会话身份 | 同上 |
| `turn_id` | 一轮对话 | `IdentityContext.__post_init__` 自动生成 `turn-<ts>-<rand>` |
| `execution_id` | 一次执行（handle 调用） | 自动生成 `exec-<ts>-<rand>` |
| `parent_execution_id` | 重试时的父执行 | `child()` 派生时填入父 `execution_id` |

设计要点：

- 标识只承担"区分身份/回合/执行"职责，不绑定任何存储路径前缀；真正的存储路径由调用方决定，e2e 测试可用 `tmp_path` 隔离
- 同一 session 多次 `handle()` 各自拥有独立 `execution_id`，失败记录不再互相覆盖
- 重试通过 `IdentityContext.child()` 派生子 execution，同时保留 `parent_execution_id` 指针，便于回溯执行链路
- 每个 emit 的 event payload 都带上 `execution_id`，前端可按 execution 串起事件流

### 1.2 handle() 主流程

1. 写入用户消息到 `Context`
2. 创建 `IdentityContext`（M0-01）
3. 调用 `ManagerAgent.plan()`
4. 根据意图分流到闲聊、教学、重试、技能管理或工具链路
5. 调用 `LearningAgent.execute_dag()`
6. 调用 `OrchestratorAgent.stream()` 生成最终回复
7. 把回复写回 `Context`
8. `build_execution_context()` 把四级标识 + 工具结果 + latency 交给 `ExecutionCritic.evaluate()` 异步评估
9. `_maybe_record_episode()` 把本次执行写入 `MemoryRepository`（M2-03）
10. `_maybe_summarize()` 超过窗口时折叠早期消息为摘要（M2-07）

## 2. ManagerAgent

文件：`agents/manager.py`

职责：

- 意图识别
- 技能选择
- 工具任务规划
- 给工具任务补 `id`

输出对象是 `PlanResult`，核心字段：

- `intent`
- `intent_detail`
- `selected_skill`
- `is_retry`
- `tool_tasks`
- `need_llm`

当前支持的主要意图：

- `chitchat`
- `skill`
- `teach`
- `retry`
- `manager`（M1-09：技能管理意图，走 `SkillManagerAgent`）
- `unknown`

当前策略：

- 优先用规则和意图检测器快速分类
- 需要时再调用 LLM 做 JSON 规划
- 在自演化开启时，会把历史提示拼进规划输入
- `ManagerAgent._current_user_id` 在 `handle()` 入口被刷新，用于召回时按 `user_id` 过滤（M2-06）

## 3. LearningAgent

文件：`agents/learning.py`

职责：

- 按名称调用工具
- 执行 `ToolTask` DAG
- 处理依赖、超时、重试、占位符替换

`ToolTask` 的关键字段：

- `id`
- `type`
- `params`
- `depends_on`
- `parallel_group`
- `retry`
- `timeout_s`
- `fallback_to`

当前参数占位符支持：

- `${user_input.xxx}`
- `${task_id.data.xxx}`
- `${task_id.error}`
- `${task_id.success}`

当前特征：

- 未知工具会返回失败结果，而不是直接抛异常
- DAG 有循环检测
- 上游失败会导致下游跳过，除非命中了 `fallback_to`

## 4. OrchestratorAgent

文件：`agents/orchestrator.py`

职责：

- 把工具结果转成模型可消费的文本块
- 根据技能 `method` 组织回答
- 支持流式输出和非流式输出

主要接口：

- `await orchestrate(...)`
- `async for token in stream(...)`

当前行为：

- 如果选中了技能且技能有 `method`，优先按方法论组织答案
- 如果没有技能方法论，则直接基于工具结果生成回复
- 会把最近几轮 `Context` 拼进 prompt

## 5. SkillTrainer

文件：`agents/skill_trainer.py`

职责：

- 识别用户是否在"教"系统
- 从教学文本中提取技能结构
- 把技能持久化到 `skills/user/`

教学流程（M1-01 已重构为状态机）：

1. 启发式判断是否为教学
2. 用 LLM 抽取 `Skill` 增量字段
3. TeachingSession 状态机推进（Collecting → Draft → Active）
4. 检测相似/重复技能 → 用户选择 reuse/update_new/cancel
5. 草稿经 `validate_skill()` 校验后发布

关键模块：

- `TeachingSession` 状态机 → `agents/teaching_session.py`
- 验证流水线 → `skills/validator.py`
- 发布确认 API → `backend/main.py /api/teachings/*`

### 5.1 SkillManagerAgent（M1-09）

文件：`agents/skill_manager_agent.py`

`MANAGER` 意图走主链的技能管理 Agent，支持：

- `list`：列出所有技能 + active 版本
- `show`：展示某技能的某版本详情
- `versions`：列出同名技能的全部历史版本
- `activate`：切换 active 指针到指定版本
- `rollback`：回滚到上一版本（保留历史，不删除）

返回 `SkillManagerResult`（`action / ok / message / skill_name / version / details`），由 `Agent.handle()` 通过 `skill_manager_result` 事件推给前端。

### 5.2 修复 `_complete_teaching()` bug（M1-07）

- 移除不存在的 `self.llm` 调用
- 停止把 assistant 追问当作 user 消息回写 `Context`；中间追问以 `assistant_message` 角色留给下一轮

## 6. 当前问题

- `ManagerAgent` 仍较依赖 prompt + JSON 输出稳定性
- 教学交互式问答体验仍偏基础，复杂多轮教学需要前端更多引导
- `SkillManagerAgent` 当前只支持只读 + 切换/回滚，不支持通过对话创建版本
