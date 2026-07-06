"""代码执行插件"""
import sys
import io
import traceback
import contextlib

from core.plugin import BasePlugin, ToolSchema, ToolResult


class PythonSandbox:
    """Python 沙箱"""
    
    SAFE_BUILTINS = {
        'len': len, 'str': str, 'int': int, 'float': float,
        'bool': bool, 'list': list, 'dict': dict, 'set': set,
        'tuple': tuple, 'range': range, 'enumerate': enumerate,
        'zip': zip, 'map': map, 'filter': filter,
        'sum': sum, 'min': min, 'max': max, 'abs': abs,
        'round': round, 'sorted': sorted, 'reversed': reversed,
        'any': any, 'all': all, 'isinstance': isinstance,
        'type': type, 'getattr': getattr, 'setattr': setattr,
        'hasattr': hasattr, 'dir': dir, 'hash': hash,
        'repr': repr, 'ord': ord, 'chr': chr,
        'vars': vars, 'callable': callable,
    }
    
    def __init__(self):
        self.output = io.StringIO()
        self.locals = {}
    
    def _get_builtins(self):
        builtins = dict(self.SAFE_BUILTINS)
        builtins['print'] = lambda *args, **kwargs: print(*args, file=self.output, **kwargs)
        return builtins
    
    def execute(self, code: str):
        self.output = io.StringIO()
        self.locals.clear()
        
        try:
            exec_globals = {"__builtins__": self._get_builtins()}
            with contextlib.redirect_stdout(self.output):
                exec(code, exec_globals, self.locals)
            
            return {
                "success": True,
                "output": self.output.getvalue(),
                "error": None
            }
        except Exception as e:
            return {
                "success": False,
                "output": self.output.getvalue(),
                "error": {
                    "type": type(e).__name__,
                    "message": str(e)
                }
            }
    
    def execute_function(self, code: str, func_name: str, kwargs: dict):
        self.locals.clear()
        self.output = io.StringIO()
        kwargs = kwargs or {}
        
        try:
            exec_globals = {"__builtins__": self._get_builtins()}
            
            with contextlib.redirect_stdout(self.output):
                exec(code, exec_globals, self.locals)
            
            if func_name not in self.locals:
                return {"success": False, "error": f"函数 {func_name} 不存在"}
            
            func = self.locals[func_name]
            
            self.output = io.StringIO()
            with contextlib.redirect_stdout(self.output):
                result = func(**kwargs)
            
            output = self.output.getvalue()
            return_value = str(result) if result is not None else None
            
            return {
                "success": True,
                "output": output,
                "return_value": return_value,
                "error": None
            }
        except Exception as e:
            return {
                "success": False,
                "output": self.output.getvalue(),
                "error": {
                    "type": type(e).__name__,
                    "message": str(e)
                }
            }


class CodePlugin(BasePlugin):
    """代码执行插件"""
    
    def __init__(self):
        self.sandbox = PythonSandbox()
    
    @property
    def name(self) -> str:
        return "code"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def description(self) -> str:
        return "执行 Python 代码"
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="run_code",
            description="执行 Python 代码",
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 代码"
                    }
                },
                "required": ["code"]
            }
        )
    
    def execute(self, params: dict) -> ToolResult:
        code = params.get("code")
        
        if not code:
            return ToolResult(success=False, error="缺少 code 参数")
        
        result = self.sandbox.execute(code)
        
        if result["success"]:
            output = result["output"] or "(无输出)"
            return ToolResult(success=True, data=f"✅ 执行成功\n\n{output}")
        else:
            e = result["error"]
            return ToolResult(
                success=False,
                error=f"❌ 执行失败\n\n{e['type']}: {e['message']}"
            )
