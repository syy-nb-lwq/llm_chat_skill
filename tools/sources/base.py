"""工具源抽象 - 定义工具来源的统一接口"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from tools.base import ToolResult, ToolSchema


class SourceType(Enum):
    """工具源类型"""
    PYTHON = "python"      # Python 代码编写的工具
    MCP = "mcp"            # MCP (Model Context Protocol) 协议工具
    HTTP = "http"          # HTTP API 工具
    PLUGIN = "plugin"      # 插件形式


@dataclass
class ToolSource:
    """工具源信息"""
    name: str                    # 源名称
    type: SourceType              # 源类型
    enabled: bool = True          # 是否启用
    config: Dict[str, Any] = field(default_factory=dict)  # 源配置
    description: str = ""         # 源描述


class ToolSourceBase(ABC):
    """工具源基类"""

    def __init__(self, source: ToolSource):
        self.source = source
        self._tools: Dict[str, Any] = {}  # name -> Tool instance or MCP tool info

    @abstractmethod
    async def connect(self) -> bool:
        """连接工具源"""
        ...

    @abstractmethod
    async def disconnect(self):
        """断开工具源"""
        ...

    @abstractmethod
    async def list_tools(self) -> List[Any]:
        """列出所有可用工具"""
        ...

    @abstractmethod
    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """调用工具"""
        ...

    @abstractmethod
    def get_tool_schema(self, tool_name: str) -> Optional[ToolSchema]:
        """获取工具 schema"""
        ...

    def get_tools(self) -> Dict[str, Any]:
        """获取所有工具"""
        return self._tools

    def is_connected(self) -> bool:
        """检查是否已连接"""
        return hasattr(self, '_connected') and self._connected

    def get_source_info(self) -> ToolSource:
        """获取源信息"""
        return self.source
