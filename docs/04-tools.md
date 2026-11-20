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

内置工具都在 `ToolHub.get_tool_hub()` 里注册（见 `tools/hub.py`）。
目前包括：

- `weather_query`
- `web_search` / `web_fetch`
- `read_file`
- `read_document` / `locate_document`
- `python_exec` / `write_script` / `list_scripts`（受 `CODE_EXEC_ENABLED` 控制）
- `create_tool` / `list_created_tools` / `report_capability_gap`（受 `TOOL_CREATION_ENABLED` 控制）
- `run_subagents` / `subagent_status`（受 `SUBAGENTS_ENABLED` 控制）
- `video_probe` / `video_concat`、`publish_artifact`、`tool_health`

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

### 3.3 read_file

文件：`tools/file_read.py`

**读任意文本文件**（源码/配置/日志/数据），和 `read_document` 分开：
`read_document` 做的是"解析成带锚点的结构"，`read_file` 做的是"把文件原样读出来"。

特点：

- **单文件上限 10MB**（`MAX_FILE_BYTES`）——超了**明确报错**，
  不做"只读前 10MB"。截断读取最坏的情况是模型以为看到了全文，然后据此下结论。
- 返回**带行号**的内容，方便模型引用具体行
- 大文件分段读：`start_line` / `end_line`；超出 `max_chars` 时返回
  `next_start_line`，告诉模型从哪一行续
- 编码识别：BOM → utf-8 → gb18030 → utf-8(replace)
- 二进制/文档文件**指对工具**：图片/压缩包直接报错，PDF/Word/Excel 提示改用 `read_document`
- 凭据类文件（`.env` / `*.pem` / `*.key` …）里的密钥值**打码后再返回**
  —— 工具结果会进对话、进会话库、进日志
- 相对路径依次尝试：**当前目录 → 工作区执行目录(`memory/workspace/_run`)
  → 工作区根目录**；找不到时报错会列出试过的位置

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


## 代码缩进自动修复（`try_repair_indentation`）

规划层把 `python_exec` 的代码作为 **JSON 字符串**的 `code` 字段输出。
JSON 字符串里没有编辑器的缩进对齐，模型偶发会让某一行行首多一个空格：

```python
print("日期列:", date_col)   # 顶层
# 2. 定位合计行
 total = None                # ← 多一个空格，注释不参与缩进判定
```

结果 `ast.parse` 抛 `IndentationError: unexpected indent`，整段代码**一行都不执行**。
实机踩过（2026-11-20 报销表加行那次，报"第 34 行"）。

`tools/python_exec.py::try_repair_indentation()` 在静态检查**之前**做保守修复：
只处理"前一有效代码行没有以 `:` 结尾（没开块）、当前行却突然更深缩进"的孤立行，
把它的缩进拉回上一行级别。合法的 `for/if/def` 块内缩进不会被误伤（上一行必以 `:` 结尾）。
最多迭代 8 次；修不动就原样交给 `check_code` 正常拦截。修复记录写进结果的
`meta["indent_fixed"]`，便于观测。
