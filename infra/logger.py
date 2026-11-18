"""日志系统 - 基于 stdlib logging,支持 level/文件/格式化"""
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from infra.config import config


# ===== UTF-8 stdout 包装器 (M0-05) =====
# 在 Windows 中文/日文等非 UTF-8 系统的默认代码页 (cp936 / cp932 / cp1252) 下,
# logging.StreamHandler 直接写入 sys.stdout 会把中文切成 mojibake。
# 用一个"重新编码字节再回写"的包装类,使 stdout 行为统一为 UTF-8。

# ===== 日志脱敏 =====
#
# 为什么要有这个：实机踩过 —— agent 把用户的 API key
# （`sk-api-...`）直接写进了 python_exec 的代码里，
# 而 `执行工具: {...params...}` 会**把参数原样打进日志**。
# 结果密钥以明文躺在 backend.log 里（12 处），还可能被一起提交/发出去。
#
# 脱敏不是"防止 agent 用密钥"（那是另一层：子进程 env 白名单），
# 而是**不让密钥在日志里沉淀**。

_RE_SK = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")
_RE_BEARER = re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9_\-\.]{12,})")
_RE_KV = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?key|access[_-]?token|secret[_-]?key"
    r"|secret|password|passwd)\s*[=:]\s*)([\"']?)([A-Za-z0-9_\-\.]{10,})"
)


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return value[:6] + "***"


def redact_secrets(text: Any) -> str:
    """把文本里的密钥打码。**永远不抛异常**（日志不能因为它挂掉）。"""
    try:
        s = "" if text is None else str(text)
        if not s:
            return s
        s = _RE_SK.sub(lambda m: _mask(m.group(0)), s)
        s = _RE_BEARER.sub(lambda m: f"{m.group(1)} {_mask(m.group(2))}", s)
        s = _RE_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{_mask(m.group(3))}", s)
        return s
    except Exception:
        return "<脱敏失败>"


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


# ===== 简洁的 Logger =====

class SimpleLogger:
    """基于 stdlib logging 的轻量 logger。

    保留与之前一致的 API (`info/error/warning/debug/log_flow/log_data`),
    同时支持:
    - log_level (从 config 读)
    - 可选文件输出 (config.log_to_file)
    - 订阅回调 (前端 PubSub 桥接可挂在此)
    """

    def __init__(self):
        self._subscribers: List[Callable] = []
        self._logger = logging.getLogger("skill_agent")
        self._logger.setLevel(_LEVEL_MAP.get(config.log_level.upper(), logging.INFO))
        # 防止重复添加 handler(单测或多处 import 时)
        self._logger.propagate = False
        if not self._logger.handlers:
            fmt = logging.Formatter(
                fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
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

    def _log(self, level: int, component: str, message: str, data: Any = None):
        if component:
            text = f"{component}: {message}"
        else:
            text = message
        if data is not None:
            text = f"{text} | {data}"
        self._logger.log(level, text)
        # 通知订阅者(供 PubSub 推送等)
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
