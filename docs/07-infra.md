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
  - `SOUL_ENABLED`
  - `TOOL_CACHE_ENABLED`
  - `MCP_ENABLED`
- Auth（C-01）
  - `OWNER_TOKEN`：管理 API owner token，留空则跳过校验（单机个人环境兼容）
- Embeddings
  - `EMBEDDING_PROVIDER`（默认 `mock`）/ `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL`
  - `EMBEDDING_MODEL`（默认 `text-embedding-3-small`）/ `EMBEDDING_DIMENSION`（默认 1536）
- Soul / MCP / intent
  - `SOUL_PATH` / `MCP_SERVERS`（JSON 数组字符串）
  - `INTENT_MODE`（默认 `rule_first`）/ `INTENT_RULE_THRESHOLD` / `INTENT_LLM_FALLBACK`

最近的重要变化：

- `config.validate()` 在启动时直接执行
- 配置不合法会 fail-fast，而不是只记日志继续跑
- `set_feature_flag()` 支持可选写回 `.env`
- `owner_token`（C-01）：管理类 API 通过 `Depends(require_owner_token)` 校验 `Authorization: Bearer <token>`，留空则放行
- `embedding_*` 配置项统一收口，启动时初始化 embedding service 并绑定 `MemoryRepository`（M2-04），失败时降级为全文检索不阻塞主流程

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
- `multi_provider_enabled`
  开启多 provider 路由
- `soul_enabled` / `mcp_enabled` / `tool_cache_enabled`
  分别控制 Soul 系统、MCP 工具源、工具结果缓存

## 5. 身份认证层（C-01）

文件：`infra/auth.py`

职责：

- 从请求头解析或生成 `user_id` / `session_id`
- 管理端 API 加 owner token 校验
- WebSocket `client_id` 由服务端签发，不再信任客户端提交

主要 API：

- `require_owner_token(request)`：FastAPI 依赖，校验 `Authorization: Bearer <token>`；未配置 `owner_token` 时放行（单机环境兼容）
- `require_auth(func)`：装饰器形式，等价于上面的依赖
- `get_user_from_request(req)`：从 HTTP 头 `X-User-ID` / `X-Session-ID` 提取，缺失则服务端签发
- `get_user_from_ws(ws)` / `extract_ws_identity(data, headers)`：WebSocket 侧身份提取，`client_id` 始终由 `gen_client_id()` 签发
- `gen_user_id()` / `gen_session_id()` / `gen_client_id()`：生成带前缀的短 id

`backend/main.py` 中 13 个写管理路由已挂 `Depends(require_owner_token)`（详见 `06-backend-frontend.md`）。

## 6. 配置诊断命令（C-02）

文件：`ui/cli.py`

CLI 重构为 argparse 双子命令：

- `chat`（默认，向后兼容）：进入交互式聊天循环
- `diagnose`（C-02）：一次性打印运行时状态，返回退出码 0/2

`diagnose` 子命令展示：

| 小节 | 内容 |
|---|---|
| Provider | `default_provider` / base_url / model / API key 是否配置 |
| Feature flags | 7 个 feature flag 当前值，`[OK]` / `[--]` 标记 |
| Tool sources | `ToolHub.health_summary()` 聚合 + 每个源 `state/enabled/tool_count/error` |
| Embedding | provider / model / dimension / service 实例状态 |
| Config validation | `config.validate()` 结果，失败返回退出码 2 |

诊断输出用 ASCII 标记（`[OK]` / `[--]`）替代 emoji，避免 Windows GBK stdout 编码错误。

调用方式：

```bash
py -m ui.cli diagnose
# 或
py -c "from ui.cli import main; main(['diagnose'])"
```

## 7. 当前缺陷

- feature flag 变更写回 `.env` 后，没有额外的变更审计
- provider 初始化状态没有统一暴露到健康检查（`/api/health` 只覆盖工具源，未覆盖 provider）
