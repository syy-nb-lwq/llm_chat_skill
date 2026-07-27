"""日志系统 - 基于 stdlib logging,支持 level/文件/格式化(P1-05: 结构化 JSON 日志)"""
import asyncio
import contextvars
import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from infra.config import config

# ===== UTF-8 stdout 包装器 (M0-05) =====
# 在 Windows 中文/日文等非 UTF-8 系统的默认代码页 (cp936 / cp932 / cp1252) 下,
# logging.StreamHandler 直接写入 sys.stdout 会把中文切成 mojibake。
# 用一个"重新编码字节再回写"的包装类,使 stdout 行为统一为 UTF-8。

class _Utf8Stream:
    """把字符串先编码为 UTF-8,再写入底层二进制流。

    这样 logging.StreamHandler 在写入前调用 self.stream.write(record.msg + "\n"),
    会经过 UTF-8 编码,避免 GBK/CP932 等窄代码页下中文被替换为 "?"。
    """

    def __init__(self, raw, encoding: str = "utf-8"):
        self._raw = raw
        self._encoding = encoding

    def write(self, s: str) -> int:
        if isinstance(s, str):
            data = s.encode(self._encoding, errors="replace")
        else:
            data = s
        return self._raw.write(data)

    def flush(self) -> None:
        try:
            self._raw.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._raw.isatty())
        except Exception:
            return False

    @property
    def closed(self) -> bool:
        return getattr(self._raw, "closed", False)


# ===== 日志级别映射 =====

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


class LogLevel:
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ===== P1-05: 结构化日志上下文 =====
# 用 contextvars 在请求/任务范围内绑定 execution_id / trace_id,
# logger._log 会自动读取并注入到日志记录中,无需每处调用都传参。
_log_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "_log_context", default={}
)


def bind_log_context(**fields: Any) -> contextvars.Token:
    """绑定日志上下文字段(如 execution_id / trace_id),返回 token 用于 reset。

    典型用法:
        token = bind_log_context(execution_id="exec_xxx", trace_id="trace_yyy")
        try:
            ...  # 此范围内的所有日志都会带上这两个字段
        finally:
            reset_log_context(token)
    """
    current = _log_context.get()
    merged = {**current, **fields}
    return _log_context.set(merged)


def reset_log_context(token: contextvars.Token) -> None:
    """恢复到之前的日志上下文(与 bind_log_context 配对)。"""
    _log_context.reset(token)


def get_log_context() -> dict:
    """读取当前日志上下文(主要用于测试)。"""
    return dict(_log_context.get())


# ===== P1-05: JSON Formatter =====


class JsonFormatter(logging.Formatter):
    """把 LogRecord 格式化为单行 JSON(P1-05)。

    输出字段:
    - ts: ISO8601 时间戳(带毫秒)
    - level: 日志级别
    - component: 组件名(从 extra.component 读)
    - msg: 日志消息
    - execution_id: 执行 ID(可选,从 extra 或上下文读)
    - trace_id: 追踪 ID(可选,从 extra 或上下文读)
    - data: 附加数据(可选,从 extra.data 读)
    """

    # 标准 LogRecord 已有属性,不能在 extra 里覆盖
    _RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": getattr(record, "component", ""),
            "msg": record.getMessage(),
        }
        # 从 LogRecord extra 读结构化字段
        execution_id = getattr(record, "execution_id", None)
        if execution_id:
            payload["execution_id"] = execution_id
        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            payload["trace_id"] = trace_id
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = data
        # 异常信息
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


# ===== 简洁的 Logger =====

class SimpleLogger:
    """基于 stdlib logging 的轻量 logger(P1-05: 支持 JSON 结构化输出)。

    保留与之前一致的 API (`info/error/warning/debug/log_flow/log_data`),
    同时支持:
    - log_level (从 config 读)
    - log_format: "text"(默认,向后兼容) 或 "json"(P1-05 结构化)
    - 可选文件输出 (config.log_to_file)
    - 订阅回调 (前端 PubSub 桥接可挂在此)
    - contextvars 自动注入 execution_id / trace_id
    """

    def __init__(self):
        self._subscribers: list[Callable] = []
        self._logger = logging.getLogger("skill_agent")
        self._logger.setLevel(_LEVEL_MAP.get(config.log_level.upper(), logging.INFO))
        # 防止重复添加 handler(单测或多处 import 时)
        self._logger.propagate = False
        if not self._logger.handlers:
            fmt = self._make_formatter()
            stream_target = sys.stdout.buffer if hasattr(sys.stdout, "buffer") else sys.stdout
            stream = logging.StreamHandler(_Utf8Stream(stream_target))
            stream.setFormatter(fmt)
            self._logger.addHandler(stream)
            if config.log_to_file:
                try:
                    log_dir = Path(config.log_dir)
                    log_dir.mkdir(parents=True, exist_ok=True)
                    fh = logging.FileHandler(log_dir / "skill_agent.log", encoding="utf-8")
                    fh.setFormatter(fmt)
                    self._logger.addHandler(fh)
                except Exception:
                    # 文件 handler 创建失败不影响 stdout
                    pass

    @staticmethod
    def _make_formatter() -> logging.Formatter:
        """根据 config.log_format 选择 formatter(P1-05)。"""
        if str(config.log_format).lower() == "json":
            return JsonFormatter()
        return logging.Formatter(
            fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def _log(
        self,
        level: int,
        component: str,
        message: str,
        data: Any = None,
        *,
        execution_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ):
        # 构造 extra,把结构化字段挂到 LogRecord 上供 formatter 读取
        extra: dict[str, Any] = {"component": component}
        # 优先用显式传入的,否则从 contextvars 读
        ctx = _log_context.get()
        eid = execution_id or ctx.get("execution_id")
        tid = trace_id or ctx.get("trace_id")
        if eid:
            extra["execution_id"] = eid
        if tid:
            extra["trace_id"] = tid
        if data is not None:
            extra["data"] = data

        # text 模式:把 component/data 拼进 message(向后兼容旧格式)
        # json 模式:formatter 从 extra 读,无需拼接
        if str(config.log_format).lower() == "json":
            text = message
        else:
            text = f"{component}: {message}" if component else message
            if data is not None:
                text = f"{text} | {data}"

        self._logger.log(level, text, extra=extra)
        # 通知订阅者(供 PubSub 推送等)— 保持 (level, component, message, data) 签名不变
        for cb in list(self._subscribers):
            try:
                cb(level, component, message, data)
            except Exception:
                pass

    def info(self, component: str, message: str, data: Any = None):
        self._log(logging.INFO, component, message, data)

    def error(self, component: str, message: str, data: Any = None):
        self._log(logging.ERROR, component, message, data)

    def warning(self, component: str, message: str, data: Any = None):
        self._log(logging.WARNING, component, message, data)

    def debug(self, component: str, message: str, data: Any = None):
        self._log(logging.DEBUG, component, message, data)

    # 便捷方法(保留旧 API)
    def log_flow(self, component: str, message: str):
        self.info(component, message)

    def log_data(self, component: str, direction: str, name: str, value: Any):
        arrow = "←" if direction == "in" else "→"
        self.info(component, f"{arrow} {name}")

    # 订阅(供 PubSub 等)
    def subscribe(self, callback: Callable):
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        if callback in self._subscribers:
            self._subscribers.remove(callback)


# 全局单例
_logger: Optional[SimpleLogger] = None


def get_logger() -> SimpleLogger:
    global _logger
    if _logger is None:
        _logger = SimpleLogger()
    return _logger


# ===== P1-04: 后台任务 done_callback =====
# 所有 asyncio.create_task 都应挂上此回调,确保后台协程的异常不会"静默吞掉"。
# 用法: task = asyncio.create_task(coro); task.add_done_callback(log_task_exception)


def log_task_exception(task: asyncio.Task, component: str = "BackgroundTask") -> None:
    """`add_done_callback` 回调:任务异常退出时记录错误日志(P1-04)。

    - 已 cancel 的任务视为正常关闭,不记录
    - 正常完成的任务不记录
    - 抛异常的任务用 logger.error 记录,包含 repr(exc) 和 traceback
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    import traceback
    get_logger().error(
        component,
        f"background task {getattr(task, 'get_name', lambda: None)() or '<unnamed>'} 异常退出: {exc!r}",
        data={"traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))},
    )
