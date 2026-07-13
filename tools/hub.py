"""工具中枢 Hub - 多源工具统一管理"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Awaitable

from infra.logger import get_logger
from tools.base import Tool, ToolResult, ToolSchema
from tools.sources.base import SourceType, ToolSource, ToolSourceBase


@dataclass
class ToolInfo:
    """工具信息(包含来源)"""
    name: str
    description: str
    schema: ToolSchema
    source_name: str
    source_type: SourceType
    instance: Any  # 原始工具实例


class ToolHub:
    """工具中枢

    统一管理多源工具(Python 代码、MCP 等),提供:
    - 工具注册/注销
    - 工具发现和搜索
    - 统一调用接口
    - 源健康检查
    """

    def __init__(self):
        self.logger = get_logger()
        self._sources: Dict[str, ToolSourceBase] = {}  # source_name -> source
        self._tools: Dict[str, ToolInfo] = {}  # tool_name -> ToolInfo
        self._python_tools_dir: List[str] = []  # Python 工具目录
        self._initialized = False  # 是否已初始化

    # ===== 源管理 =====

    def register_source(self, source: ToolSourceBase):
        """注册工具源"""
        if source.source.name in self._sources:
            raise ValueError(f"工具源 {source.source.name} 已存在")

        self._sources[source.source.name] = source
        self.logger.info("ToolHub", f"注册工具源: {source.source.name} ({source.source.type.value})")

    def unregister_source(self, source_name: str) -> bool:
        """注销工具源"""
        if source_name not in self._sources:
            return False

        # 移除该源的所有工具
        to_remove = [name for name, info in self._tools.items() if info.source_name == source_name]
        for name in to_remove:
            del self._tools[name]

        del self._sources[source_name]
        self.logger.info("ToolHub", f"注销工具源: {source_name}")
        return True

    async def connect_source(self, source_name: str) -> bool:
        """连接工具源"""
        source = self._sources.get(source_name)
        if not source:
            raise ValueError(f"工具源不存在: {source_name}")

        try:
            ok = await source.connect()
            if ok:
                # 加载工具
                await self._load_tools_from_source(source)
                self.logger.info("ToolHub", f"连接工具源成功: {source_name}, 工具数: {len(self._tools)}")
            return ok
        except Exception as e:
            self.logger.error("ToolHub", f"连接工具源失败: {source_name}, {e}")
            raise

    async def disconnect_source(self, source_name: str):
        """断开工具源"""
        source = self._sources.get(source_name)
        if source:
            await source.disconnect()

    async def connect_all(self):
        """连接所有启用的工具源"""
        for name, source in self._sources.items():
            if source.source.enabled:
                try:
                    await self.connect_source(name)
                except Exception as e:
                    self.logger.warning("ToolHub", f"连接失败: {name}, {e}")

    async def disconnect_all(self):
        """断开所有工具源"""
        for name in list(self._sources.keys()):
            await self.disconnect_source(name)

    # ===== 工具管理 =====

    def get_tool(self, name: str) -> Optional[ToolInfo]:
        """获取工具信息"""
        return self._tools.get(name)

    def list_tools(self) -> List[ToolInfo]:
        """列出所有工具"""
        return list(self._tools.values())

    def list_tools_by_source(self, source_name: str) -> List[ToolInfo]:
        """列出指定源的工具"""
        return [t for t in self._tools.values() if t.source_name == source_name]

    def schemas(self) -> List[Dict]:
        """获取所有工具的 schema(用于 LLM)"""
        return [t.schema for t in self._tools.values()]

    def names(self) -> List[str]:
        """获取所有工具名称"""
        return list(self._tools.keys())

    def all(self) -> List[Any]:
        """获取所有工具实例(兼容旧 API)"""
        return [t.instance for t in self._tools.values()]

    def register_python_tool(self, tool: Tool):
        """注册单个 Python 工具"""
        if not tool.name:
            raise ValueError("工具 name 不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已存在")

        schema = tool.schema() if hasattr(tool, 'schema') else None
        info = ToolInfo(
            name=tool.name,
            description=tool.description,
            schema=schema,
            source_name="builtin",
            source_type=SourceType.PYTHON,
            instance=tool,
        )
        self._tools[tool.name] = info
        self.logger.info("ToolHub", f"注册 Python 工具: {tool.name}")

    def register(self, tool: Tool):
        """注册工具(兼容旧 API)"""
        self.register_python_tool(tool)

    def unregister_tool(self, tool_name: str) -> bool:
        """注销工具"""
        if tool_name in self._tools:
            del self._tools[tool_name]
            self.logger.info("ToolHub", f"注销工具: {tool_name}")
            return True
        return False

    # ===== 工具调用 =====

    async def call_tool(
        self,
        tool_name: str,
        params: Dict[str, Any],
        *,
        on_event: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    ) -> ToolResult:
        """调用工具"""
        tool_info = self._tools.get(tool_name)
        if not tool_info:
            return ToolResult(success=False, error=f"工具不存在: {tool_name}")

        # 查找源
        source = self._sources.get(tool_info.source_name)

        # 如果源不存在但工具有实例，直接执行（兼容测试场景）
        if not source:
            instance = tool_info.instance
            if hasattr(instance, 'execute'):
                try:
                    if asyncio.iscoroutinefunction(instance.execute):
                        result = await instance.execute(**params)
                    else:
                        result = await asyncio.to_thread(instance.execute, **params)
                    return result
                except Exception as e:
                    return ToolResult(success=False, error=str(e))
            return ToolResult(success=False, error=f"工具 {tool_name} 没有可执行的 execute 方法")

        # 发送事件
        if on_event:
            await on_event("tool_call", {
                "tool": tool_name,
                "params": params,
                "source": tool_info.source_name,
            })

        try:
            result = await source.call_tool(tool_name, params)

            if on_event:
                await on_event("tool_result", {
                    "tool": tool_name,
                    "result": result.data if result.success else None,
                    "error": result.error if not result.success else None,
                    "meta": result.meta,
                })

            return result
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    # ===== 内部方法 =====

    async def _load_tools_from_source(self, source: ToolSourceBase):
        """从源加载工具"""
        try:
            tools = await source.list_tools()
            for tool in tools:
                if hasattr(tool, 'name') and tool.name:
                    schema = source.get_tool_schema(tool.name) if hasattr(source, 'get_tool_schema') else None
                    if schema is None and hasattr(tool, 'schema'):
                        schema = tool.schema() if callable(tool.schema) else tool.schema

                    info = ToolInfo(
                        name=tool.name,
                        description=getattr(tool, 'description', ''),
                        schema=schema,
                        source_name=source.source.name,
                        source_type=source.source.type,
                        instance=tool,
                    )
                    self._tools[tool.name] = info
        except Exception as e:
            self.logger.error("ToolHub", f"加载工具失败: {source.source.name}, {e}")

    # ===== 源信息 =====

    def get_sources(self) -> List[ToolSource]:
        """获取所有工具源信息"""
        return [s.source for s in self._sources.values()]

    def get_source_status(self) -> Dict[str, Dict]:
        """获取源状态"""
        return {
            name: {
                "name": s.source.name,
                "type": s.source.type.value,
                "enabled": s.source.enabled,
                "connected": s.is_connected(),
                "tool_count": len([t for t in self._tools.values() if t.source_name == name]),
            }
            for name, s in self._sources.items()
        }


# 全局单例
_hub: Optional[ToolHub] = None
_builtin_tools_added = False


def get_tool_hub() -> ToolHub:
    """获取 ToolHub 全局实例"""
    global _hub, _builtin_tools_added
    if _hub is None:
        _hub = ToolHub()

    # 自动添加内置工具(如果尚未添加)
    if not _builtin_tools_added and not _hub._initialized:
        try:
            from tools.weather import WeatherTool
            from tools.search import SearchTool
            _hub.register_python_tool(WeatherTool())
            _hub.register_python_tool(SearchTool())
            _builtin_tools_added = True
        except Exception:
            pass

    return _hub


def reset_tool_hub():
    """重置 ToolHub"""
    global _hub, _builtin_tools_added
    if _hub:
        try:
            loop = asyncio.get_running_loop()
            # 如果在事件循环中，不等待 disconnect
            asyncio.create_task(_hub.disconnect_all())
        except RuntimeError:
            # 没有运行中的事件循环，忽略 disconnect
            pass
    _hub = None
    _builtin_tools_added = False
