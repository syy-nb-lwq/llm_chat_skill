"""Python 工具源 - 从 Python 代码加载工具"""
import asyncio
import importlib
import pkgutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.base import Tool, ToolResult, ToolSchema
from tools.sources.base import SourceType, ToolSource, ToolSourceBase


class PythonSource(ToolSourceBase):
    """Python 工具源

    从指定目录扫描 Python 模块,自动注册继承 Tool 的类作为工具。
    """

    def __init__(self, source: ToolSource):
        super().__init__(source)
        self._connected = False
        self._tool_dirs: List[Path] = []

    async def connect(self) -> bool:
        """扫描并加载 Python 工具"""
        try:
            # 从配置获取工具目录
            tool_dirs = self.source.config.get("directories", [])
            for dir_path in tool_dirs:
                path = Path(dir_path)
                if path.exists() and path.is_dir():
                    self._tool_dirs.append(path)
                    await self._scan_directory(path)

            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise e

    async def disconnect(self):
        """断开连接"""
        self._tools.clear()
        self._connected = False

    async def list_tools(self) -> List[Tool]:
        """列出所有工具"""
        return [t for t in self._tools.values() if isinstance(t, Tool)]

    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """调用工具"""
        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"工具不存在: {tool_name}")

        if not isinstance(tool, Tool):
            return ToolResult(success=False, error=f"{tool_name} 不是有效的 Python 工具")

        try:
            # 异步执行
            if asyncio.iscoroutinefunction(tool.execute):
                result = await tool.execute(**params)
            else:
                result = await asyncio.to_thread(tool.execute, **params)
            return result
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def get_tool_schema(self, tool_name: str) -> Optional[ToolSchema]:
        """获取工具 schema"""
        tool = self._tools.get(tool_name)
        if tool and isinstance(tool, Tool):
            return tool.schema()
        return None

    async def _scan_directory(self, path: Path):
        """扫描目录下的 Python 模块"""
        for _, module_name, is_pkg in pkgutil.iter_modules([str(path)]):
            if module_name.startswith("_"):
                continue
            try:
                # 动态导入模块
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    path / f"{module_name}.py"
                )
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # 查找继承 Tool 的类
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (
                            isinstance(attr, type)
                            and issubclass(attr, Tool)
                            and attr is not Tool
                        ):
                            # 实例化并注册
                            instance = attr()
                            if instance.name:
                                self._tools[instance.name] = instance
            except Exception:
                pass

    def register_tool(self, tool: Tool):
        """手动注册工具"""
        if not tool.name:
            raise ValueError("工具 name 不能为空")
        self._tools[tool.name] = tool

    def unregister_tool(self, tool_name: str) -> bool:
        """注销工具"""
        if tool_name in self._tools:
            del self._tools[tool_name]
            return True
        return False


def create_python_source(
    name: str = "python",
    directories: Optional[List[str]] = None,
) -> PythonSource:
    """创建 Python 工具源"""
    config = {"directories": directories or []}
    source = ToolSource(
        name=name,
        type=SourceType.PYTHON,
        config=config,
        description="Python 代码编写的工具",
    )
    return PythonSource(source)
