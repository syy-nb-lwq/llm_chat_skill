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

`handle()` 的主要流程：

1. 写入用户消息到 `Context`
2. 调用 `ManagerAgent.plan()`
3. 根据意图分流到闲聊、教学、重试或工具链路
4. 调用 `LearningAgent.execute_dag()`
5. 调用 `OrchestratorAgent.stream()` 生成最终回复
6. 把回复写回 `Context`
7. 异步触发 `ExecutionCritic.evaluate()`

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
- `unknown`

当前策略：

- 优先用规则和意图检测器快速分类
- 需要时再调用 LLM 做 JSON 规划
- 在自演化开启时，会把历史提示拼进规划输入

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

- 识别用户是否在“教”系统
- 从教学文本中提取技能结构
- 把技能持久化到 `skills/user/`

教学流程：

1. 启发式判断是否为教学
2. 需要时用 LLM 做二次确认
3. 用 LLM 抽取 `Skill`
4. 检查是否已有相似技能
5. 不完整时返回交互式补充问题
6. 写入 YAML 文件并刷新技能库

## 6. 当前问题

- `ManagerAgent` 与 `SkillTrainer` 仍较依赖 prompt + JSON 输出稳定性
- `ExecutionCritic.build_execution_context()` 目前无法准确反推真实工具名
- 教学交互链路已经存在，但前端对补充问答的体验仍比较基础
