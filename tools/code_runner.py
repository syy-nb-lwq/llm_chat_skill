"""代码执行工具 - 完整版"""
import sys
import io
import traceback
import contextlib
from typing import Any, Dict


class SafeExecutor:
    """安全的代码执行器"""
    
    # 所有可用的内置函数
    SAFE_BUILTINS = {
        'len': len,
        'str': str,
        'int': int,
        'float': float,
        'bool': bool,
        'list': list,
        'dict': dict,
        'set': set,
        'tuple': tuple,
        'range': range,
        'enumerate': enumerate,
        'zip': zip,
        'map': map,
        'filter': filter,
        'sum': sum,
        'min': min,
        'max': max,
        'abs': abs,
        'round': round,
        'sorted': sorted,
        'reversed': reversed,
        'any': any,
        'all': all,
        'isinstance': isinstance,
        'type': type,
        'getattr': getattr,
        'setattr': setattr,
        'hasattr': hasattr,
        'dir': dir,
        'id': id,
        'hash': hash,
        'repr': repr,
        'ord': ord,
        'chr': chr,
        'hex': hex,
        'oct': oct,
        'bin': bin,
        'format': format,
        'slice': slice,
        'vars': vars,
        'callable': callable,
    }
    
    def __init__(self):
        self.output = io.StringIO()
        self.locals = {}
    
    def _get_builtins(self):
        """获取内置函数"""
        builtins = {}
        for name, func in self.SAFE_BUILTINS.items():
            builtins[name] = func
        # 添加 print
        builtins['print'] = lambda *args, **kwargs: print(*args, file=self.output, **kwargs)
        return builtins
    
    def execute(self, code: str) -> Dict[str, Any]:
        """执行代码"""
        self.output = io.StringIO()
        self.locals.clear()
        
        builtins = self._get_builtins()
        
        try:
            with contextlib.redirect_stdout(self.output):
                exec(code, {"__builtins__": builtins}, self.locals)
            
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
                    "message": str(e),
                    "traceback": traceback.format_exc()
                }
            }
    
    def execute_function(self, func_code: str, func_name: str, kwargs: dict = None) -> Dict[str, Any]:
        """执行函数"""
        self.locals.clear()
        self.output = io.StringIO()
        kwargs = kwargs or {}
        
        builtins = self._get_builtins()
        
        try:
            # 定义函数
            with contextlib.redirect_stdout(self.output):
                exec(func_code, {"__builtins__": builtins}, self.locals)
            
            if func_name not in self.locals:
                return {
                    "success": False,
                    "error": {"message": f"函数 {func_name} 未定义"}
                }
            
            func = self.locals[func_name]
            
            # 执行函数
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
                    "message": str(e),
                    "traceback": traceback.format_exc()
                }
            }


def run_code(code: str) -> str:
    """执行代码"""
    executor = SafeExecutor()
    result = executor.execute(code)
    
    if result["success"]:
        return f"✅ 执行成功\n\n{result['output'] or '(无输出)'}"
    else:
        e = result["error"]
        return f"❌ 执行失败\n\n{e['type']}: {e['message']}"


def run_function(func_code: str, func_name: str, kwargs: dict = None) -> str:
    """执行函数"""
    executor = SafeExecutor()
    kwargs = kwargs or {}
    
    result = executor.execute_function(func_code, func_name, kwargs)
    
    if result["success"]:
        output = result.get("output", "") or ""
        return_value = result.get("return_value")
        
        if return_value:
            return return_value
        elif output:
            return output
        return "✅ 执行成功"
    else:
        e = result["error"]
        return f"❌ 执行失败\n\n{e['type']}: {e['message']}"
