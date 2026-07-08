"""日志系统 - 简洁的流转追踪"""
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from infra.config import config


# ===== 日志级别 =====

class LogLevel:
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ===== 简洁的 Logger =====

class SimpleLogger:
    """简化日志，只打印关键流转信息"""
    
    def __init__(self):
        self._subscribers: List[Callable] = []
        
    def _log(self, level: str, component: str, message: str, data: Any = None):
        """统一日志输出"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        # 只打印关键信息
        if component:
            print(f"[{timestamp}] {component}: {message}")
        else:
            print(f"[{timestamp}] {message}")
    
    def info(self, component: str, message: str):
        self._log("INFO", component, message)
    
    def error(self, component: str, message: str):
        self._log("ERROR", component, message)
    
    def warning(self, component: str, message: str):
        self._log("WARNING", component, message)
    
    def debug(self, component: str, message: str):
        self._log("DEBUG", component, message)
    
    # 便捷方法
    def log_flow(self, component: str, message: str):
        self.info(component, message)
    
    def log_data(self, component: str, direction: str, name: str, value: Any):
        if direction == "in":
            self.info(component, f"← {name}")
        else:
            self.info(component, f"→ {name}")


# 全局单例
_logger: Optional[SimpleLogger] = None


def get_logger() -> SimpleLogger:
    global _logger
    if _logger is None:
        _logger = SimpleLogger()
    return _logger
