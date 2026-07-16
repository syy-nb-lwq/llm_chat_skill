# Infra 模块

`infra/` 提供配置、日志和模型提供商初始化能力。

## 1. 配置

文件：`infra/config.py`

配置基于 `pydantic-settings`，从 `.env` 读取。

关键配置项：

- Provider
  - `DEFAULT_PROVIDER`
  - `OPENAI_API_KEY`
  - `OPENAI_BASE_URL`
  - `OPENAI_MODEL`
  - `ANTHROPIC_API_KEY`
  - `ANTHROPIC_MODEL`
  - `LOCAL_BASE_URL`
  - `LOCAL_MODEL`
- Runtime
  - `SESSION_TTL_S`
  - `REQUEST_TIMEOUT_S`
  - `MAX_ITERATIONS`
- Feature flags
  - `SKILL_DAG_ENABLED`
  - `SELF_EVOLUTION_ENABLED`
  - `SEMANTIC_MEMORY_ENABLED`
  - `MULTI_PROVIDER_ENABLED`

最近的重要变化：

- `config.validate()` 在启动时直接执行
- 配置不合法会 fail-fast，而不是只记日志继续跑
- `set_feature_flag()` 支持可选写回 `.env`

## 2. Provider 初始化

后端启动时会调用 `infra.providers.registry.init_providers(...)`。

当前思路：

- 按配置初始化 OpenAI / Anthropic / Local provider
- 默认使用 `default_provider`
- provider 选择对上层 Agent 透明

## 3. 日志

文件：`infra/logger.py`

当前日志用途：

- 记录启动和关闭过程
- 记录 Agent 执行阶段
- 记录工具调用结果
- 记录会话创建和 GC
- 记录自演化链路事件

## 4. Feature Flags

当前常用开关：

- `skill_dag_enabled`
  控制是否优先使用技能结构化步骤
- `self_evolution_enabled`
  控制批评器、反思和补丁审批相关能力
- `semantic_memory_enabled`
  控制 Manager 是否尝试语义记忆

## 5. 当前缺陷

- 缺少单独的配置诊断命令
- feature flag 变更写回 `.env` 后，没有额外的变更审计
- provider 初始化状态没有统一暴露到健康检查
