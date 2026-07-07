"""工具基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    data: Any = None
    error: str = ""
    
    @property
    def content(self) -> str:
        if self.success:
            return str(self.data) if self.data is not None else ""
        return f"错误: {self.error}"


class Tool(ABC):
    """工具基类"""
    
    name: str = ""
    description: str = ""
    
    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """执行工具"""
        pass
    
    def validate(self, **kwargs) -> tuple[bool, str]:
        """验证参数，返回 (是否有效, 错误信息)"""
        return True, ""
