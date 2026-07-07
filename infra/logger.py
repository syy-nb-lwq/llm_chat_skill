"""日志系统 - 数据流转和流程流转追踪"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime
import json
import threading


class LogLevel(Enum):
    """日志级别"""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogType(Enum):
    """日志类型"""
    # 流程日志
    FLOW_START = "flow_start"           # 流程开始
    FLOW_END = "flow_end"               # 流程结束
    FLOW_STEP = "flow_step"             # 流程步骤
    FLOW_BRANCH = "flow_branch"         # 流程分支
    
    # 数据日志
    DATA_INPUT = "data_input"           # 输入数据
    DATA_OUTPUT = "data_output"         # 输出数据
    DATA_TRANSFORM = "data_transform"   # 数据转换
    DATA_STORE = "data_store"           # 数据存储
    
    # Agent 日志
    AGENT_INTENT = "agent_intent"       # 意图识别
    AGENT_PLAN = "agent_plan"           # 任务规划
    AGENT_TOOL = "agent_tool"           # 工具调用
    AGENT_RESULT = "agent_result"        # 工具结果
    
    # 工具日志
    TOOL_CALL = "tool_call"            # 工具调用
    TOOL_SUCCESS = "tool_success"       # 工具成功
    TOOL_ERROR = "tool_error"           # 工具错误
    
    # LLM 日志
    LLM_REQUEST = "llm_request"         # LLM 请求
    LLM_RESPONSE = "llm_response"       # LLM 响应
    LLM_ERROR = "llm_error"            # LLM 错误


@dataclass
class LogEntry:
    """日志条目"""
    id: str
    timestamp: str
    level: LogLevel
    type: LogType
    component: str          # 组件名称
    message: str           # 日志消息
    data: Optional[Dict] = None  # 附加数据
    trace_id: str = ""     # 追踪 ID
    parent_id: str = ""     # 父日志 ID
    duration_ms: float = 0 # 耗时（毫秒）
    
    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "level": self.level.value,
            "type": self.type.value,
            "component": self.component,
            "message": self.message,
            "data": self.data,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms
        }


class TraceContext:
    """追踪上下文"""
    
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.start_time = datetime.now()
        self.entries: List[LogEntry] = []
        self._counter = 0
        self._lock = threading.Lock()
    
    def next_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"{self.trace_id}-{self._counter}"
    
    def add_entry(self, entry: LogEntry):
        with self._lock:
            self.entries.append(entry)
    
    def get_summary(self) -> Dict:
        duration = (datetime.now() - self.start_time).total_seconds() * 1000
        return {
            "trace_id": self.trace_id,
            "duration_ms": duration,
            "total_steps": len(self.entries),
            "steps": [e.to_dict() for e in self.entries]
        }


class Logger:
    """日志收集器"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._traces: Dict[str, TraceContext] = {}
        self._subscribers: List[Callable[[LogEntry], None]] = []
        self._current_trace: Optional[TraceContext] = None
        self._lock = threading.Lock()
        self._counter = 0
        self._initialized = True
    
    def subscribe(self, callback: Callable[[LogEntry], None]):
        """订阅日志"""
        self._subscribers.append(callback)
    
    def unsubscribe(self, callback: Callable[[LogEntry], None]):
        """取消订阅"""
        if callback in self._subscribers:
            self._subscribers.remove(callback)
    
    def _notify(self, entry: LogEntry):
        """通知订阅者"""
        for callback in self._subscribers:
            try:
                callback(entry)
            except Exception:
                pass
    
    def _next_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"log-{datetime.now().strftime('%Y%m%d%H%M%S')}-{self._counter}"
    
    def start_trace(self, name: str = "default") -> TraceContext:
        """开始一个新的追踪"""
        trace_id = f"trace-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        trace = TraceContext(trace_id)
        with self._lock:
            self._traces[trace_id] = trace
        self._current_trace = trace
        self.info(LogType.FLOW_START, "Logger", f"开始追踪: {name}", {"trace_name": name, "trace_id": trace_id})
        return trace
    
    def end_trace(self) -> Optional[Dict]:
        """结束当前追踪"""
        if self._current_trace:
            trace_id = self._current_trace.trace_id
            self.info(LogType.FLOW_END, "Logger", f"结束追踪: {trace_id}")
            summary = self._current_trace.get_summary()
            self._current_trace = None
            return summary
        return None
    
    def get_trace(self, trace_id: str) -> Optional[TraceContext]:
        """获取追踪"""
        return self._traces.get(trace_id)
    
    def _log(
        self,
        level: LogLevel,
        log_type: LogType,
        component: str,
        message: str,
        data: Optional[Dict] = None,
        duration_ms: float = 0
    ):
        """内部日志方法"""
        entry = LogEntry(
            id=self._next_id(),
            timestamp=datetime.now().isoformat(),
            level=level,
            type=log_type,
            component=component,
            message=message,
            data=data,
            trace_id=self._current_trace.trace_id if self._current_trace else "",
            parent_id=self._current_trace.entries[-1].id if self._current_trace and self._current_trace.entries else "",
            duration_ms=duration_ms
        )
        
        with self._lock:
            if self._current_trace:
                self._current_trace.add_entry(entry)
        
        self._notify(entry)
        
        # 打印到控制台
        prefix = {
            LogLevel.DEBUG: "🔍",
            LogLevel.INFO: "ℹ️",
            LogLevel.WARNING: "⚠️",
            LogLevel.ERROR: "❌"
        }.get(level, "•")
        
        print(f"{prefix} [{log_type.value}] [{component}] {message}")
        if data:
            print(f"   数据: {json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
    
    # 便捷方法
    def debug(self, log_type: LogType, component: str, message: str, data: Optional[Dict] = None):
        self._log(LogLevel.DEBUG, log_type, component, message, data)
    
    def info(self, log_type: LogType, component: str, message: str, data: Optional[Dict] = None):
        self._log(LogLevel.INFO, log_type, component, message, data)
    
    def warning(self, log_type: LogType, component: str, message: str, data: Optional[Dict] = None):
        self._log(LogLevel.WARNING, log_type, component, message, data)
    
    def error(self, log_type: LogType, component: str, message: str, data: Optional[Dict] = None):
        self._log(LogLevel.ERROR, log_type, component, message, data)
    
    # 流程追踪方法
    def log_flow(self, component: str, step: str, data: Optional[Dict] = None):
        """记录流程步骤"""
        self.info(LogType.FLOW_STEP, component, step, data)
    
    def log_data(self, component: str, direction: str, data_name: str, data_value: Any):
        """记录数据流转"""
        log_type = LogType.DATA_INPUT if direction == "in" else LogType.DATA_OUTPUT
        self.info(log_type, component, f"数据{direction}: {data_name}", {"data_name": data_name, "preview": str(data_value)[:200]})
    
    def log_agent(self, agent_name: str, action: str, intent: Optional[str] = None, tasks: Optional[List] = None):
        """记录 Agent 行为"""
        if action == "intent":
            self.info(LogType.AGENT_INTENT, agent_name, f"识别意图: {intent}", {"intent": intent})
        elif action == "plan":
            self.info(LogType.AGENT_PLAN, agent_name, f"规划任务: {len(tasks) if tasks else 0} 个任务", {"tasks": tasks})
        elif action == "tool":
            self.info(LogType.AGENT_TOOL, agent_name, f"调用工具: {intent}", {"tool": intent})
    
    def log_tool(self, tool_name: str, params: Dict, result: Any, success: bool):
        """记录工具调用"""
        if success:
            self.info(LogType.TOOL_SUCCESS, tool_name, f"工具执行成功", {
                "params": params,
                "result_preview": str(result)[:200]
            })
        else:
            self.error(LogType.TOOL_ERROR, tool_name, f"工具执行失败: {result}", {"params": params})
    
    def log_llm(self, component: str, prompt: str, response: str, success: bool = True):
        """记录 LLM 调用"""
        if success:
            self.info(LogType.LLM_RESPONSE, component, "LLM 响应", {
                "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
                "response_preview": response[:200] + "..." if len(response) > 200 else response
            })
        else:
            self.error(LogType.LLM_ERROR, component, f"LLM 调用失败: {response}", {
                "prompt_preview": prompt[:100]
            })


# 全局日志实例
logger = Logger()


def get_logger() -> Logger:
    """获取日志实例"""
    return logger
