# Tools 模块

当前工具系统已经以 `ToolHub` 为统一入口。

## 1. 基础抽象

文件：`tools/base.py`

关键对象：

- `Tool`
- `ToolSchema`
- `ToolParam`
- `ToolResult`

约定：

- 每个工具至少提供 `name`、`description`、`schema()`
- 工具执行返回 `ToolResult`
- `ToolResult` 包含 `success`、`data`、`error`、`meta`

## 2. ToolHub

文件：`tools/hub.py`

`ToolHub` 当前负责：

- 注册工具源
- 加载工具定义
- 暴露统一调用接口
- 管理工具生命周期

主要接口：

- `register_source()`
- `connect_source()`
- `connect_all()`
- `disconnect_all()`
- `register_python_tool()`
- `call_tool()`
- `names()`
- `schemas()`

最近的重要变化：

- 工具层已经从“散落的注册逻辑”收敛到 `ToolHub`
- `disconnect_all()` 会调用 `aclose_tools()`
- 具备 `aclose()` 的工具现在会在关闭时释放资源

## 3. 内置工具

当前默认注册两个内置 Python 工具：

- `weather_query`
- `web_search`

### 3.1 weather_query

文件：`tools/weather.py`

特点：

- 使用 `httpx.AsyncClient`
- 默认访问 `wttr.in`
- 提供 `city`、`date` 参数
- 支持 `aclose()`

### 3.2 web_search

文件：`tools/search.py`

特点：

- 已改为异步实现
- 使用共享 `httpx.AsyncClient`
- 当前后端顺序：
  - `SearXNG`
  - `Wikipedia`
- 成功时会在 `meta.source` 中标明后端来源

## 4. 工具调用流程

运行时工具调用路径：

```text
LearningAgent.execute_tool()
  -> ToolHub.get_tool()
  -> ToolHub.call_tool()
  -> source.call_tool() or instance.execute()
  -> ToolResult
```

如果工具是同步实现，`ToolHub` 会用线程包装调用。

## 5. 工具扩展方式

推荐做法：

1. 新建工具类并继承 `Tool`
2. 实现 `schema()` 和 `execute()`
3. 如有外部资源，补 `aclose()`
4. 在 Python source 中自动发现，或显式注册到 `ToolHub`

## 6. 当前缺陷

- 工具源健康状态已通过 `/api/health`（§7）和 CLI `diagnose` 子命令暴露，但缺少前端专门诊断页面
- `web_search` 的公网依赖较多，稳定性受外部服务影响
- 没有统一缓存层，重复查询会直接打外部接口（`tool_cache_enabled` feature flag 仍是占位）
- M4 工具提案审批/发布只暴露 Python API，无 REST 路由（见 §8.7）

## 7. 启动可观测性（M0-06）

`ToolHub` 维护每个工具源的状态机：

| state | 含义 | 触发 |
|---|---|---|
| `registered` | 已注册,未尝试连接 | `register_source()` |
| `connecting` | 正在连接 | `connect_source()` 入口 |
| `connected` | 已连接,工具可用 | `source.connect()` 返回 True |
| `connect_failed` | 连接失败 | 返回 False 或抛异常 |
| `disconnected` | 已主动断开 | `disconnect_source()` |

- `ToolHub.get_source_status()` 返回每个源的 `state / error / connected_at / tool_count`
- `ToolHub.health_summary()` 汇总 `total_sources / connected / failed / disconnected / has_failures`
- `GET /api/health` 暴露 `tool_sources` 聚合字段 + `sources` 详情;顶层 `status` 在 `has_failures=True` 时降级为 `degraded`
- `connect_all()` 单源失败不影响其他源,失败状态被显式记录

## 8. 受控工具沉淀（M4 整组）

允许用户通过对话提出并发布新的工具能力，先声明式（HTTP/MCP），暂不允许 LLM 直接生成并执行 Python 工具。

### 8.1 ToolProposal 数据模型（M4-01）

文件：`tools/proposal.py`

- `ToolProposal`：完整提案描述
  - `name` / `version`（semver）/ `runtime`（`declarative_http` / `mcp`）
  - `endpoint: ToolEndpoint`（method / path / params / returns）
  - `permissions` / `network_policy: NetworkPolicy` / `side_effect`
  - `secret_refs`（命名空间引用，如 `github.token`，不存明文）
  - `test_cases: List[ToolTestCase]` / `status` / `proposal_id`
- `SideEffectLevel`：`read_only` / `local_write` / `network_write` / `destructive`（仅 `read_only` 可自动发布）
- `ToolProposalStatus`：`draft` → `sandbox_ok` / `sandbox_failed` → `approved` → `published` → `disabled` / `rejected`
- `NetworkPolicy`：`allowed_hosts`（支持 `*.example.com` 通配符）/ `denied_hosts` / `require_https`
- `ToolParamSpec`：`name / type / description / required / default / enum / location`（query/path/header/body）
- `ToolTestCase`：`name / input / expected_status / expected_keys / expect_error`

静态校验 `ToolProposal.validate()` 返回错误列表：name 格式、version semver、runtime 范围、endpoint 合法性、side_effect 合法性、secret_refs 命名空间化、网络白名单非空。

### 8.2 声明式 HTTP 工具（M4-02）

文件：`tools/declarative_http.py`

`DeclarativeHTTPTool(proposal, base_url, secret_resolver)` 把 `ToolProposal` 渲染成 `Tool` 实例：

- `schema()` 从 `endpoint.params` 生成 `ToolSchema` / `ToolParam`
- `execute()` 按 `endpoint.method` + `endpoint.path` 渲染 path/query/body/header，发起 HTTP 请求
- 返回 `ToolResult`，`meta.source` 标明来源
- `register_to_hub(hub)` 注册到 `ToolHub`

### 8.3 网络白名单（M4-04）

执行前强制校验：

- `_host_in_allowed(host, allowed)`：精确匹配 + `*.example.com` 通配符匹配
- `_enforce_network_policy(url, policy)`：返回 None 通过，否则返回错误信息
- `require_https=True` 时强制 HTTPS（localhost/127.0.0.1/::1 沙箱豁免）
- host 不在 `allowed_hosts` 内或在 `denied_hosts` 内 → 拒绝
- secret 引用通过 `infra.config.get_secret(name)` 解析，工具不接触明文

### 8.4 沙箱测试运行器（M4-03）

文件：`tools/declarative_http.py`（`SandboxRunner` 类）

- 隔离目录 + 受限网络 + 测试用例运行，失败不污染主进程
- 静态检查：proposal `validate()` + 网络白名单覆盖 endpoint host
- 多用例执行：每个 `ToolTestCase` 跑一次，断言 `expected_status` / `expected_keys` / `expect_error`
- 支持 `success` / `expect_error` 两类断言

### 8.5 审批发布 + 注册（M4-05）

文件：`tools/approval.py`

`ToolApprovalService` 提供 sandbox → approve → publish → disable 全生命周期：

- `run_sandbox(proposal)`：跑沙箱测试，推进到 `SANDBOX_OK` 或 `SANDBOX_FAILED`
- `approve_proposal(name, version, approver)`：把 `DRAFT` / `SANDBOX_OK` 推进到 `APPROVED`；`read_only` + 沙箱通过可自动发布
- `publish_proposal(name, version)`：把 `APPROVED` 注册到 `ToolHub`（`DeclarativeHTTPTool.register_to_hub`），状态变为 `PUBLISHED`
- `disable_proposal(name, version)`：把 `PUBLISHED` 工具从 `ToolHub` 注销，状态变为 `DISABLED`；规划器不再选择该工具
- `reject_proposal(name, version, reason)`：拒绝草案
- 每次审批记录审计日志：`approver / timestamp / from_status / to_status / proposal_id`

`ToolProposalStore.save(proposal, overwrite=...)`：

- `overwrite=False`（默认）：同 `name@version` 视为版本冲突，抛 `FileExistsError`（强制"新建版本"约束，避免原地覆盖）
- `overwrite=True`：仅用于状态机迁移，不允许新增字段

存储位置：`tools/proposals/{name}@{version}.yaml`。

### 8.6 副作用分级审批策略

| SideEffectLevel | 自动发布 | 审批要求 |
|---|---|---|
| `read_only` | ✅ 允许（沙箱通过后） | 可跳过人工审批 |
| `local_write` | ❌ | 普通审批 |
| `network_write` | ❌ | 高风险审批 |
| `destructive` | ❌ | 禁止自动发布，需显式审批 |

### 8.7 e2e 闭环（M4-06）

文件：`tests/e2e/test_m4_06_declarative_tool_e2e.py`

4 个 e2e 用例覆盖声明式工具全链路：

- draft → sandbox → approve → publish → call → disable 全生命周期
- 禁用后工具从 `ToolHub` 注销
- 发布后可真实调用（本地 mock HTTP server 验证 read-only 声明式工具）
- 审计日志记录生命周期

注：M4 的审批/发布目前通过 Python API（`ToolApprovalService`）暴露，`backend/main.py` 尚未提供 `/api/tools/proposals/*` REST 路由（见 `00-总览.md` 当前已知限制）。
