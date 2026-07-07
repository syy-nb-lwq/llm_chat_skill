"""日志系统 - 数据流转和流程流转追踪"""
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from infra.config import config


# ===== 类型(保持兼容) =====

class LogLevel(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogType(Enum):
    FLOW_START = "flow_start"
    FLOW_END = "flow_end"
    FLOW_STEP = "flow_step"
    FLOW_BRANCH = "flow_branch"
    DATA_INPUT = "data_input"
    DATA_OUTPUT = "data_output"
    DATA_TRANSFORM = "data_transform"
    DATA_STORE = "data_store"
    AGENT_INTENT = "agent_intent"
    AGENT_PLAN = "agent_plan"
    AGENT_TOOL = "agent_tool"
    AGENT_RESULT = "agent_result"
    TOOL_CALL = "tool_call"
    TOOL_SUCCESS = "tool_success"
    TOOL_ERROR = "tool_error"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    LLM_ERROR = "llm_error"


# ===== 标准 logging 体系 =====

_std_logger = logging.getLogger("skill_agent")
_std_logger.setLevel(getattr(logging, config.log_level.upper(), logging.INFO))
_std_logger.propagate = False
# 避免重复添加 handler(单进程 reload 时常见)
_std_logger.handlers.clear()

_console = logging.StreamHandler()
_console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_std_logger.addHandler(_console)

if config.log_to_file:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    _file = logging.FileHandler(config.log_dir / "skill_agent.log", encoding="utf-8")
    _file.setFormatter(logging.Formatter(
        '{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}'
    ))
    _std_logger.addHandler(_file)


# ===== 敏感字段脱敏 =====

_SENSITIVE_KEYS = {"api_key", "apikey", "token", "password", "secret", "authorization", "x-api-key"}


def _sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in _SENSITIVE_KEYS else _sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# ===== LogEntry =====

@dataclass
class LogEntry:
    id: str
    timestamp: str
    level: LogLevel
    type: LogType
    component: str
    message: str
    data: Optional[Dict] = None
    trace_id: str = ""
    parent_id: str = ""
    duration_ms: float = 0

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "level": self.level.value,
            "type": self.type.value,
            "component": self.component,
            "message": self.message,
            "data": _sanitize(self.data) if self.data else {},
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms,
        }


# ===== Trace 管理 =====

class TraceContext:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.start_time = datetime.now()
        self.entries: List[LogEntry] = []
        self._lock = threading.Lock()

    def add(self, entry: LogEntry):
        with self._lock:
            self.entries.append(entry)

    def summary(self) -> Dict:
        duration = (datetime.now() - self.start_time).total_seconds() * 1000
        return {
            "trace_id": self.trace_id,
            "duration_ms": duration,
            "total_steps": len(self.entries),
            "steps": [e.to_dict() for e in self.entries],
        }


# ===== 主 Logger(单例,兼容旧 API) =====

class Logger:
    _instance: Optional["Logger"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._traces: Dict[str, TraceContext] = {}
        self._subscribers: List[Callable[[LogEntry], None]] = []
        self._sub_lock = threading.Lock()
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._current_trace: Optional[TraceContext] = None
        self._initialized = True

    # ----- 订阅(去重) -----
    def subscribe(self, callback: Callable[[LogEntry], None]):
        with self._sub_lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback):
        with self._sub_lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _notify(self, entry: LogEntry):
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(entry)
            except Exception:
                pass

    # ----- Trace -----
    def start_trace(self, name: str = "default") -> TraceContext:
        trace_id = f"trace-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        trace = TraceContext(trace_id)
        self._traces[trace_id] = trace
        self._current_trace = trace
        self.info(LogType.FLOW_START, "Logger", f"开始追踪: {name}", {"trace_name": name})
        return trace

    def end_trace(self) -> Optional[Dict]:
        if not self._current_trace:
            return None
        summary = self._current_trace.summary()
        self.info(LogType.FLOW_END, "Logger", "结束追踪", summary)
        self._current_trace = None
        return summary

    def get_trace(self, trace_id: str) -> Optional[TraceContext]:
        return self._traces.get(trace_id)

    # ----- 记录 -----
    def _next_id(self) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"log-{datetime.now().strftime('%Y%m%d%H%M%S')}-{self._counter}"

    def _log(self, level: LogLevel, log_type: LogType, component: str,
             message: str, data: Optional[Dict] = None, duration_ms: float = 0):
        entry = LogEntry(
            id=self._next_id(),
            timestamp=datetime.now().isoformat(),
            level=level,
            type=log_type,
            component=component,
            message=message,
            data=data,
            trace_id=self._current_trace.trace_id if self._current_trace else "",
            parent_id=(self._current_trace.entries[-1].id
                       if self._current_trace and self._current_trace.entries else ""),
            duration_ms=duration_ms,
        )
        if self._current_trace:
            self._current_trace.add(entry)
        self._notify(entry)

        # 标准 logging
        std_level = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
        }[level]
        try:
            payload = json.dumps(entry.to_dict(), ensure_ascii=False, default=str)
        except Exception:
            payload = json.dumps({"message": message, "component": component})
        _std_logger.log(std_level, payload)

    # ----- 便捷方法 -----
    def debug(self, log_type, component, message, data=None):
        self._log(LogLevel.DEBUG, log_type, component, message, data)

    def info(self, log_type, component, message, data=None):
        self._log(LogLevel.INFO, log_type, component, message, data)

    def warning(self, log_type, component, message, data=None):
        self._log(LogLevel.WARNING, log_type, component, message, data)

    def error(self, log_type, component, message, data=None):
        self._log(LogLevel.ERROR, log_type, component, message, data)

    def log_flow(self, component, step, data=None):
        self.info(LogType.FLOW_STEP, component, step, data)

    def log_data(self, component, direction, data_name, data_value):
        log_type = LogType.DATA_INPUT if direction == "in" else LogType.DATA_OUTPUT
        self.info(log_type, component, f"数据{direction}: {data_name}",
                  {"data_name": data_name, "preview": str(data_value)[:200]})

    def log_tool_call(self, tool, params):
        self.info(LogType.TOOL_CALL, tool, "工具调用", {"params": params})

    def log_tool_success(self, tool, result):
        self.info(LogType.TOOL_SUCCESS, tool, "工具成功", {"preview": str(result)[:200]})

    def log_tool_error(self, tool, error):
        self.error(LogType.TOOL_ERROR, tool, "工具失败", {"error": error})


def get_logger() -> Logger:
    return Logger()