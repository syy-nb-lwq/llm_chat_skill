"""插件基类和注册表"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class ToolSchema:
    """工具参数 schema"""
    name: str
    description: str
    parameters: Dict[str, Any]
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    data: Any = None
    error: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error
        }


class BasePlugin(ABC):
    """插件基类"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """插件名称"""
        pass
    
    @property
    @abstractmethod
    def version(self) -> str:
        """插件版本"""
        return "1.0.0"
    
    @property
    def description(self) -> str:
        """插件描述"""
        return ""
    
    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """获取工具 schema"""
        pass
    
    @abstractmethod
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        """执行插件"""
        pass
    
    def validate(self, params: Dict[str, Any]) -> bool:
        """验证参数"""
        return True
    
    def on_load(self):
        """插件加载时调用"""
        pass
    
    def on_unload(self):
        """插件卸载时调用"""
        pass


class PluginRegistry:
    """插件注册表"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._plugins: Dict[str, BasePlugin] = {}
            cls._instance._schemas: List[Dict] = []
        return cls._instance
    
    def register(self, plugin: BasePlugin) -> None:
        """注册插件"""
        self._plugins[plugin.name] = plugin
        plugin.on_load()
        self._update_schemas()
    
    def unregister(self, name: str) -> None:
        """卸载插件"""
        if name in self._plugins:
            self._plugins[name].on_unload()
            del self._plugins[name]
            self._update_schemas()
    
    def get(self, name: str) -> Optional[BasePlugin]:
        """获取插件"""
        return self._plugins.get(name)
    
    def list_all(self) -> List[BasePlugin]:
        """列出所有插件"""
        return list(self._plugins.values())
    
    def get_schemas(self) -> List[Dict]:
        """获取所有工具 schema"""
        return self._schemas
    
    def _update_schemas(self) -> None:
        """更新 schemas"""
        self._schemas = []
        for plugin in self._plugins.values():
            schema = plugin.get_schema()
            self._schemas.append({
                "type": "function",
                "function": schema.to_dict()
            })


# 全局注册表
registry = PluginRegistry()
