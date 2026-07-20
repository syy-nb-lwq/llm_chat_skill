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

- 工具源的健康状态暴露有限，没有专门诊断页面
- `web_search` 的公网依赖较多，稳定性受外部服务影响
- 没有统一缓存层，重复查询会直接打外部接口

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
