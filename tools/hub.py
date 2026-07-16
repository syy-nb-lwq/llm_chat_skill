"""Unified tool hub for Python and external tool sources."""
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolResult, ToolSchema
from tools.sources.base import SourceType, ToolSource, ToolSourceBase


@dataclass
class ToolInfo:
    name: str
    description: str
    schema: ToolSchema
    source_name: str
    source_type: SourceType
    instance: Any


class ToolHub:
    """Registry and executor for tools from multiple sources."""

    def __init__(self):
        self.logger = get_logger()
        self._sources: Dict[str, ToolSourceBase] = {}
        self._tools: Dict[str, ToolInfo] = {}
        self._initialized = False

    def register_source(self, source: ToolSourceBase):
        if source.source.name in self._sources:
            raise ValueError(f"Tool source already exists: {source.source.name}")
        self._sources[source.source.name] = source
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

        ok = await source.connect()
        if ok:
            await self._load_tools_from_source(source)
            self.logger.info("ToolHub", f"connected source {source_name}")
        return ok

    async def disconnect_source(self, source_name: str):
        source = self._sources.get(source_name)
        if source:
            await source.disconnect()

    async def connect_all(self):
        for name, source in list(self._sources.items()):
            if not source.source.enabled:
                continue
            try:
                await self.connect_source(name)
            except Exception as exc:
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

    def register_python_tool(self, tool: Tool):
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
        )
        self._tools[tool.name] = info
        self.logger.info("ToolHub", f"registered python tool {tool.name}")

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
        tool_info = self._tools.get(tool_name)
        if not tool_info:
            return ToolResult(success=False, error=f"Unknown tool: {tool_name}")

        source = self._sources.get(tool_info.source_name)
        if not source:
            return await self._call_instance(tool_info.instance, params)

        if on_event:
            await on_event(
                "tool_call",
                {"tool": tool_name, "params": params, "source": tool_info.source_name},
            )

        try:
            result = await source.call_tool(tool_name, params)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        if on_event:
            await on_event(
                "tool_result",
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
        return {
            name: {
                "name": source.source.name,
                "type": source.source.type.value,
                "enabled": source.source.enabled,
                "connected": source.is_connected(),
                "tool_count": len(
                    [tool for tool in self._tools.values() if tool.source_name == name]
                ),
            }
            for name, source in self._sources.items()
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

            _hub.register_python_tool(WeatherTool())
            _hub.register_python_tool(SearchTool())
            _builtin_tools_added = True
        except Exception:
            pass

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
