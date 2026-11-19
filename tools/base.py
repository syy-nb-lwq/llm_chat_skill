"""工具协议"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolResult:
    """工具执行结果

    字段:
    - success: 是否成功
    - data:    原始结构化数据(给 Orchestrator/DAG 用)
    - error:   错误信息
    - meta:    元信息(耗时/来源/重试次数),前端可观测
    """
    success: bool
    data: Any = None
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_content(self) -> str:
        """给 LLM 看的字符串形式"""
        if not self.success:
            return f"[错误] {self.error}"
        if self.data is None:
            return ""
        import json
        try:
            return json.dumps(self.data, ensure_ascii=False, indent=2)
        except Exception:
            return str(self.data)

    # 兼容旧字段
    @property
    def content(self) -> str:
        return self.to_content()


@dataclass
class ToolParam:
    name: str
    type: str                       # string/number/integer/boolean/object/array
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Optional[List] = None


@dataclass
class ToolSchema:
    """工具对外暴露的 schema(给 LLM / 前端用)"""
    name: str
    description: str
    params: List[ToolParam] = field(default_factory=list)
    returns: Dict[str, str] = field(default_factory=dict)
    examples: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "params": [
                {
                    "name": p.name,
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "default": p.default,
                    "enum": p.enum,
                }
                for p in self.params
            ],
            "returns": self.returns,
            "examples": self.examples,
        }


class Tool(ABC):
    """工具基类"""

    name: str = ""
    description: str = ""
    #: 需要某个配置开关才启用(如 "code_exec_enabled")。
    #:
    #: **为什么需要它**:工具目录会被 `PythonSource` 扫描并自动注册 ——
    #: 那是"把工具放进目录就能用"的便利。但有些工具是**安全开关**管的
    #: (代码执行 = 执行 LLM 生成的代码),光靠"不手动注册"关不掉,
    #: 扫描器照样会把它加载进来。
    #: 声明了这个字段,扫描器就会去查对应开关。
    required_flag: str = ""

    @abstractmethod
    def schema(self) -> ToolSchema: ...

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult: ...

    def validate_params(self, params: Dict[str, Any]) -> tuple:
        """按 schema 校验参数（必填 / 未知 / 类型 / enum）。

        实现收在 `tools/validation.py`，与 ToolHub 执行前用的是同一套规则 ——
        否则"注册表说合法、执行时说非法"会变成两个真相。
        返回 `(ok, 错误信息)`；需要**转换后**的参数请直接用 `validate_params()`。
        """
        from tools.validation import validate_params as _validate

        ok, _, err = _validate(self.schema(), params)
        return ok, err


class ToolRegistry:
    """工具注册表"""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool):
        if not tool.name:
            raise ValueError("tool.name 不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已存在")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def validate_params(self, name: str, params: Dict) -> tuple:
        tool = self.get(name)
        if not tool:
            return False, f"未知工具: {name}"
        return tool.validate_params(params)

    def schemas(self) -> List[Dict]:
        return [t.schema().to_dict() for t in self.all()]


# 全局单例
_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
        _register_builtins(_registry)
    return _registry


def reset_tool_registry():
    global _registry
    _registry = None


def _register_builtins(reg: ToolRegistry):
    """注册内置工具(延迟 import 避免循环)"""
    from tools.weather import WeatherTool
    from tools.search import SearchTool
    reg.register(WeatherTool())
    reg.register(SearchTool())