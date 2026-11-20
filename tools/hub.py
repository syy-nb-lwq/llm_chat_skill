"""Unified tool hub for Python and external tool sources."""
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolResult, ToolSchema
from tools.sources.base import SourceType, ToolSource, ToolSourceBase
from core.events import EV_TOOL_CALL, EV_TOOL_RESULT
from tools.validation import validate_params
from core.exception_policy import downgrade


@dataclass
class ToolInfo:
    name: str
    description: str
    schema: ToolSchema
    source_name: str
    source_type: SourceType
    instance: Any
    # --- 归属（agent 自造的工具才有）---
    #
    # 之前注册完就不知道"这工具是谁造的"了 —— 既没法按来源禁用，
    # 也没法在将来收紧可见性时做数据迁移。
    owner_agent_id: str = ""
    visibility: str = "global"   # global | owner | tenant


class ToolHub:
    """Registry and executor for tools from multiple sources."""

    # 工具源状态机(M0-06):
    #   registered  → 已注册但尚未尝试连接
    #   connecting  → 正在连接(短暂)
    #   connected   → 连接成功,工具可用
    #   connect_failed → 连接失败(异常或返回 False),记录最近一次错误
    #   disconnected → 已主动断开
    SOURCE_STATE_REGISTERED = "registered"
    SOURCE_STATE_CONNECTING = "connecting"
    SOURCE_STATE_CONNECTED = "connected"
    SOURCE_STATE_FAILED = "connect_failed"
    SOURCE_STATE_DISCONNECTED = "disconnected"

    def __init__(self):
        self.logger = get_logger()
        self._sources: Dict[str, ToolSourceBase] = {}
        self._tools: Dict[str, ToolInfo] = {}
        self._initialized = False
        # M0-06: 记录每个源最近一次连接状态 + 错误信息
        self._source_state: Dict[str, str] = {}
        self._source_last_error: Dict[str, str] = {}
        self._source_connected_at: Dict[str, float] = {}

    def register_source(self, source: ToolSourceBase):
        if source.source.name in self._sources:
            raise ValueError(f"Tool source already exists: {source.source.name}")
        self._sources[source.source.name] = source
        self._source_state[source.source.name] = self.SOURCE_STATE_REGISTERED
        self._source_last_error.pop(source.source.name, None)
        self._source_connected_at.pop(source.source.name, None)
        self.logger.info("ToolHub", f"registered source {source.source.name}")

    def unregister_source(self, source_name: str) -> bool:
        if source_name not in self._sources:
            return False
        to_remove = [name for name, info in self._tools.items() if info.source_name == source_name]
        for name in to_remove:
            del self._tools[name]
        del self._sources[source_name]
        self.logger.info("ToolHub", f"unregistered source {source_name}")
        return True

    async def connect_source(self, source_name: str) -> bool:
        source = self._sources.get(source_name)
        if not source:
            raise ValueError(f"Tool source not found: {source_name}")

        # M0-06: 记录 connecting 状态
        self._source_state[source_name] = self.SOURCE_STATE_CONNECTING
        self._source_last_error[source_name] = ""
        try:
            ok = await source.connect()
        except Exception as exc:
            self._source_state[source_name] = self.SOURCE_STATE_FAILED
            self._source_last_error[source_name] = f"{type(exc).__name__}: {exc}"
            self.logger.warning("ToolHub", f"connect_source {source_name} raised: {exc}")
            raise
        if ok:
            await self._load_tools_from_source(source)
            self._source_state[source_name] = self.SOURCE_STATE_CONNECTED
            self._source_last_error[source_name] = ""
            self._source_connected_at[source_name] = time.time()
            self.logger.info("ToolHub", f"connected source {source_name}")
        else:
            self._source_state[source_name] = self.SOURCE_STATE_FAILED
            self._source_last_error[source_name] = "connect() returned False"
            self.logger.warning("ToolHub", f"connect_source {source_name} returned False")
        return ok

    async def disconnect_source(self, source_name: str):
        source = self._sources.get(source_name)
        if source:
            await source.disconnect()
            self._source_state[source_name] = self.SOURCE_STATE_DISCONNECTED
            self._source_connected_at.pop(source_name, None)

    async def connect_all(self):
        for name, source in list(self._sources.items()):
            if not source.source.enabled:
                # 未启用的源:状态保持 registered,不算失败
                continue
            try:
                await self.connect_source(name)
            except Exception as exc:
                # connect_source 内部已记录 FAILED + last_error,这里仅补充 warning
                self.logger.warning("ToolHub", f"failed connecting {name}: {exc}")
        self._initialized = True

    async def disconnect_all(self):
        for name in list(self._sources.keys()):
            await self.disconnect_source(name)
        await self.aclose_tools()
        self._initialized = False

    async def aclose_tools(self):
        """Close resources held by tool instances."""
        closed_ids = set()
        for info in list(self._tools.values()):
            instance = info.instance
            instance_id = id(instance)
            if instance_id in closed_ids:
                continue
            closed_ids.add(instance_id)
            close_fn = getattr(instance, "aclose", None)
            if close_fn is None:
                continue
            try:
                if asyncio.iscoroutinefunction(close_fn):
                    await close_fn()
                else:
                    result = close_fn()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                self.logger.warning("ToolHub", f"failed closing {info.name}: {exc}")

    def get_tool(self, name: str) -> Optional[ToolInfo]:
        return self._tools.get(name)

    def list_tools(self) -> List[ToolInfo]:
        return list(self._tools.values())

    def list_tools_by_source(self, source_name: str) -> List[ToolInfo]:
        return [tool for tool in self._tools.values() if tool.source_name == source_name]

    def schemas(self) -> List[ToolSchema]:
        return [tool.schema for tool in self._tools.values()]

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def all(self) -> List[Any]:
        return [tool.instance for tool in self._tools.values()]

    def register_python_tool(
        self,
        tool: Tool,
        *,
        owner_agent_id: str = "",
        visibility: str = "global",
    ):
        if not tool.name:
            raise ValueError("tool.name 不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具已存在: {tool.name}")

        info = ToolInfo(
            name=tool.name,
            description=tool.description,
            schema=tool.schema(),
            source_name="builtin",
            source_type=SourceType.PYTHON,
            instance=tool,
            owner_agent_id=owner_agent_id or "",
            visibility=visibility or "global",
        )
        self._tools[tool.name] = info
        self.logger.info("ToolHub", f"registered python tool {tool.name}")

    def tools_by_owner(self, owner_agent_id: str) -> List[str]:
        """某个 owner 名下的工具名（agent 自造的工具）。"""
        return [
            info.name
            for info in self._tools.values()
            if info.owner_agent_id and info.owner_agent_id == owner_agent_id
        ]

    def unregister_owner(self, owner_agent_id: str) -> List[str]:
        """一次性禁用某个 owner 的全部工具。返回被禁用的工具名。

        存在的意义：**出了事要能立刻收手**。agent 自造的工具默认全局可见
        （docs/18 §10.2），那就必须有一条"一键收回"的路径。
        """
        removed = []
        for name in self.tools_by_owner(owner_agent_id):
            if self.unregister_tool(name):
                removed.append(name)
        return removed

    def register(self, tool: Tool):
        self.register_python_tool(tool)

    def unregister_tool(self, tool_name: str) -> bool:
        if tool_name not in self._tools:
            return False
        del self._tools[tool_name]
        self.logger.info("ToolHub", f"unregistered tool {tool_name}")
        return True

    async def call_tool(
        self,
        tool_name: str,
        params: Dict[str, Any],
        *,
        on_event: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    ) -> ToolResult:
        """所有工具调用的**唯一入口** —— 在这里记账。

        记账放在这一层而不是各工具内部，保证：
        - 不漏（任何路径都经过这里）
        - 不重复（工具自己不需要关心观测）
        """
        import time as _time

        from core.tool_health import get_health_store

        started = _time.perf_counter()
        known = tool_name in self._tools

        # 调一个**不存在**的工具 —— 这是"能力缺口"的强信号
        # （agent 想要的能力还没有），要单独记，不能当普通工具失败统计，
        # 否则"健康度"里会混进一堆幽灵工具。
        if not known:
            try:
                get_health_store().record_gap(
                    f"需要一个能做「{tool_name}」的能力，但工具表里没有",
                    tried=f"尝试调用 {tool_name}",
                    blocked_by="工具不存在",
                )
            except Exception as exc:
                downgrade(exc, where='tools/hub.py')
        result = await self._call_tool_inner(tool_name, params, on_event=on_event)

        if known:
            try:
                source = ""
                info = self._tools.get(tool_name)
                if info is not None:
                    source = str(getattr(info, "source_name", "") or "")
                get_health_store().record(
                    tool_name,
                    bool(getattr(result, "success", False)),
                    error=str(getattr(result, "error", "") or ""),
                    duration_ms=(_time.perf_counter() - started) * 1000,
                    source=source,
                )
            except Exception as exc:
                downgrade(exc, where='tools/hub.py')
        return result

    async def _call_tool_inner(
        self,
        tool_name: str,
        params: Dict[str, Any],
        *,
        on_event: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    ) -> ToolResult:
        tool_info = self._tools.get(tool_name)
        if not tool_info:
            return ToolResult(success=False, error=f"Unknown tool: {tool_name}")

        # **参数校验是唯一收口点**（见 tools/validation.py）。
        #
        # 不校验时，`**kwargs` 派的工具会把拼错的 `file_path` 当成"没传 path"，
        # 有些直接给默认值返回成功 —— 模型拿到"成功"会据此继续推理，
        # 比报错危险得多。这里宁可让调用失败。
        ok, clean_params, param_error = validate_params(tool_info.schema, params)
        if not ok:
            result = ToolResult(
                success=False,
                error=f"工具 {tool_name} 参数错误：{param_error}",
                meta={"invalid_params": True, "tool": tool_name},
            )
            if on_event:
                await on_event(
                    EV_TOOL_CALL,
                    {"tool": tool_name, "params": params, "source": tool_info.source_name},
                )
                await on_event(
                    EV_TOOL_RESULT,
                    {
                        "tool": tool_name,
                        "result": None,
                        "error": result.error,
                        "meta": result.meta,
                    },
                )
            return result
        params = clean_params

        source = self._sources.get(tool_info.source_name)

        # **Python 工具直接执行实例，不要委托给 source。**
        #
        # 踩过的坑（实机）：`register_python_tool()` 只把工具放进 `hub._tools`，
        # 而 `PythonSource._tools` 是它**自己扫描目录**得到的另一份。
        # 内置工具两边都有（扫描时就加载了），所以没问题；
        # 但 **agent 动态创建的工具**不在目录里 → `source.call_tool()` 找不到 →
        # 报"工具不存在"，可 hub 里明明有它。
        #
        # 语义上也该直接调实例：实例就是工具本身，绕一层源没有意义。
        if source is None or tool_info.source_type == SourceType.PYTHON:
            if on_event:
                await on_event(
                    EV_TOOL_CALL,
                    {"tool": tool_name, "params": params, "source": tool_info.source_name},
                )
            result = await self._call_instance(tool_info.instance, params)
            if on_event:
                await on_event(
                    EV_TOOL_RESULT,
                    {
                        "tool": tool_name,
                        "result": result.data if result.success else None,
                        "error": result.error if not result.success else None,
                        "meta": result.meta,
                    },
                )
            return result

        if on_event:
            await on_event(
                EV_TOOL_CALL,
                {"tool": tool_name, "params": params, "source": tool_info.source_name},
            )

        try:
            result = await source.call_tool(tool_name, params)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        if on_event:
            await on_event(
                EV_TOOL_RESULT,
                {
                    "tool": tool_name,
                    "result": result.data if result.success else None,
                    "error": result.error if not result.success else None,
                    "meta": result.meta,
                },
            )
        return result

    async def _call_instance(self, instance: Any, params: Dict[str, Any]) -> ToolResult:
        if not hasattr(instance, "execute"):
            return ToolResult(success=False, error="Tool instance has no execute()")
        try:
            execute = instance.execute
            if asyncio.iscoroutinefunction(execute):
                return await execute(**params)
            return await asyncio.to_thread(execute, **params)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    async def _load_tools_from_source(self, source: ToolSourceBase):
        try:
            tools = await source.list_tools()
        except Exception as exc:
            self.logger.error("ToolHub", f"failed loading tools from {source.source.name}: {exc}")
            return

        for tool in tools:
            if not getattr(tool, "name", None):
                continue
            schema = None
            if hasattr(source, "get_tool_schema"):
                schema = source.get_tool_schema(tool.name)
            if schema is None and hasattr(tool, "schema"):
                schema = tool.schema() if callable(tool.schema) else tool.schema
            self._tools[tool.name] = ToolInfo(
                name=tool.name,
                description=getattr(tool, "description", ""),
                schema=schema,
                source_name=source.source.name,
                source_type=source.source.type,
                instance=tool,
            )

    def get_sources(self) -> List[ToolSource]:
        return [source.source for source in self._sources.values()]

    def get_source_status(self) -> Dict[str, Dict]:
        """返回每个工具源的状态(M0-06)。

        state 取值:
          - registered: 已注册,未尝试连接
          - connecting: 正在连接(通常极短,API 拉取时少见)
          - connected:  已连接,工具可用
          - connect_failed: 连接失败(异常或返回 False)
          - disconnected: 已主动断开
        """
        return {
            name: {
                "name": source.source.name,
                "type": source.source.type.value,
                "enabled": source.source.enabled,
                "connected": source.is_connected(),
                "state": self._source_state.get(name, self.SOURCE_STATE_REGISTERED),
                "error": self._source_last_error.get(name, "") or None,
                "connected_at": self._source_connected_at.get(name),
                "tool_count": len(
                    [tool for tool in self._tools.values() if tool.source_name == name]
                ),
            }
            for name, source in self._sources.items()
        }

    def health_summary(self) -> Dict[str, Any]:
        """汇总健康状态,供 /api/health 暴露(M0-06)。

        返回字段:
          - total_sources: 已注册的源数量
          - connected: 已连接数量
          - failed: 连接失败数量
          - disconnected: 已断开数量
          - sources: 每个源的详细状态(同 get_source_status)
          - has_failures: 是否有任何源处于失败状态
        """
        statuses = self.get_source_status()
        connected = sum(1 for s in statuses.values() if s["state"] == self.SOURCE_STATE_CONNECTED)
        failed = sum(1 for s in statuses.values() if s["state"] == self.SOURCE_STATE_FAILED)
        disconnected = sum(1 for s in statuses.values() if s["state"] == self.SOURCE_STATE_DISCONNECTED)
        return {
            "total_sources": len(statuses),
            "connected": connected,
            "failed": failed,
            "disconnected": disconnected,
            "sources": statuses,
            "has_failures": failed > 0,
        }


_hub: Optional[ToolHub] = None
_builtin_tools_added = False


def get_tool_hub() -> ToolHub:
    global _hub, _builtin_tools_added
    if _hub is None:
        _hub = ToolHub()

    if not _builtin_tools_added and not _hub._initialized:
        try:
            from tools.search import SearchTool
            from tools.weather import WeatherTool
            from tools.web_fetch import WebFetchTool

            _hub.register_python_tool(WeatherTool())
            _hub.register_python_tool(SearchTool())
            _hub.register_python_tool(WebFetchTool())
            _builtin_tools_added = True
        except Exception as exc:
            downgrade(exc, where='tools/hub.py')
        # 代码执行/脚本工具 —— 受 CODE_EXEC_ENABLED 控制。
        # 这是**安全开关**:执行的是 LLM 生成的代码,对外服务前应当关掉
        # 或换成容器隔离(见 tools/python_exec.py 顶部的安全模型说明)。
        try:
            from infra.config import config

            if config.code_exec_enabled:
                from tools.python_exec import PythonExecTool
                from tools.script_writer import ListScriptsTool, WriteScriptTool

                _hub.register_python_tool(
                    PythonExecTool(timeout_s=config.code_exec_timeout_s)
                )
                _hub.register_python_tool(WriteScriptTool())
                _hub.register_python_tool(ListScriptsTool())
        except Exception as exc:
            downgrade(exc, where='tools/hub.py')
        # 文档工具 —— 读文档 + 定位锚点
        try:
            from tools.document import LocateDocumentTool, ReadDocumentTool

            _hub.register_python_tool(ReadDocumentTool())
            _hub.register_python_tool(LocateDocumentTool())
        except Exception as exc:
            downgrade(exc, where='tools/hub.py')
        # 文件工具 —— 读任意文本文件(源码/配置/日志),上限 10MB
        #
        # 和 `read_document` 分开:文档工具做的是"解析成带锚点的结构",
        # 这里做的是"把文件原样读出来",两者的失败原因和用法都不一样。
        try:
            from tools.file_read import ReadFileTool

            _hub.register_python_tool(ReadFileTool())
        except Exception as exc:
            downgrade(exc, where='tools/hub.py')
        # 元工具:让 agent 自己造工具(受 TOOL_CREATION_ENABLED 控制)
        try:
            from infra.config import config

            if config.tool_creation_enabled:
                from tools.meta_tools import (
                    CreateToolTool,
                    ListCreatedToolsTool,
                    ReportCapabilityGapTool,
                )

                _hub.register_python_tool(CreateToolTool())
                _hub.register_python_tool(ListCreatedToolsTool())
                # 自诊断：让 agent 自己申报"我缺什么能力"
                _hub.register_python_tool(ReportCapabilityGapTool())

            if config.tool_repair_enabled:
                from tools.meta_tools import RepairToolTool

                _hub.register_python_tool(RepairToolTool())

            # 工具健康度（任何时候都可查，和造工具无关）
            try:
                from tools.meta_tools import ToolHealthTool

                _hub.register_python_tool(ToolHealthTool())
            except Exception as exc:
                downgrade(exc, where='tools/hub.py')
            # 产物登记:把授权目录里已有的文件变成可下载产物
            try:
                from tools.artifact_publish import PublishArtifactTool

                _hub.register_python_tool(PublishArtifactTool())
            except Exception as exc:
                downgrade(exc, where='tools/hub.py')
            # 视频:探测 / 拼接(ffmpeg 由我们受控调用,python_exec 里的 subprocess 仍然禁)
            try:
                from tools.video import VideoConcatTool, VideoProbeTool

                _hub.register_python_tool(VideoProbeTool())
                _hub.register_python_tool(VideoConcatTool())
            except Exception as exc:
                downgrade(exc, where='tools/hub.py')
            # 子智能体:把互不依赖的子任务派出去并行做
            if config.subagents_enabled:
                from core.subagents import get_subagent_pool
                from tools.subagent_tool import RunSubAgentsTool, SubAgentStatusTool

                # 池子的限制来自配置,而不是写死在代码里
                pool = get_subagent_pool()
                pool.max_concurrent = max(1, int(config.subagent_max_concurrent))
                pool.max_depth = max(0, int(config.subagent_max_depth))
                pool.timeout_s = float(config.subagent_timeout_s)

                _hub.register_python_tool(RunSubAgentsTool())
                _hub.register_python_tool(SubAgentStatusTool())
        except Exception as exc:
            downgrade(exc, where='tools/hub.py')
    return _hub


def reset_tool_hub():
    global _hub, _builtin_tools_added
    if _hub:
        try:
            asyncio.get_running_loop()
            asyncio.create_task(_hub.disconnect_all())
        except RuntimeError:
            asyncio.run(_hub.disconnect_all())
    _hub = None
    _builtin_tools_added = False
