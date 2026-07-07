# 07 — Infra 模块设计(LLM / Logger / Config)

> 本文档描述 `infra/` 下三个基础设施组件的现状、问题、改进。

---

## 1. 目录与职责

```
infra/
├── __init__.py
├── config.py     ← 配置加载
├── llm.py        ← LLM 客户端封装
└── logger.py     ← 日志系统(流程/数据/Agent/工具/LLM)
```

---

## 2. Config(`infra/config.py`)

### 2.1 现状

[infra/config.py](../infra/config.py) 是一个简单的 `dataclass`,通过手写解析 `.env` 加载环境变量。

### 2.2 问题

1. **自己解析 `.env`**:没 `python-dotenv`,容易对引号/空格处理出错
2. **没有 `.env.example`**:`README` 让用户 `cp .env.example .env`,但**该文件不存在**
3. **无配置校验**:只在 `validate()` 里检查 `llm_api_key`,其他字段错也照样启动
4. **无配置热更新**:改 `.env` 必须重启
5. **配置散落**:`max_iterations` / `memory_path` 等定义了但没用到

### 2.3 改进设计

#### 2.3.1 改用 `pydantic-settings`(推荐)

```python
# infra/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM
    llm_api_key:  str = Field("", description="LLM API Key")
    llm_base_url: str = Field("https://api.openai.com/v1")
    llm_model:    str = Field("gpt-4o-mini")
    temperature:  float = Field(0.7, ge=0.0, le=2.0)

    # 路径
    skills_path: Path = Field("skills")
    memory_path: Path = Field("memory")
    vector_path: Path = Field("vector_store")

    # 运行
    max_iterations: int = Field(10, ge=1, le=100)
    request_timeout_s: int = Field(60, ge=1)
    session_ttl_s: int = Field(3600, ge=60)

    # 日志
    log_level: str = Field("INFO")
    log_to_file: bool = Field(False)
    log_dir: Path = Field("logs")

    # Feature flags
    skill_dag_enabled: bool = Field(True)
    tool_cache_enabled: bool = Field(False)

    def validate(self) -> None:
        """启动时调用,失败抛 ConfigError。"""
        if not self.llm_api_key:
            raise ConfigError("LLM_API_KEY 未设置,请检查 .env 文件")


config = Config()
```

#### 2.3.2 提供 `.env.example`

```
# ===== LLM =====
LLM_API_KEY=sk-your-key-here
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
TEMPERATURE=0.7

# ===== 路径 =====
SKILLS_PATH=skills
MEMORY_PATH=memory
VECTOR_PATH=vector_store

# ===== 运行 =====
MAX_ITERATIONS=10
REQUEST_TIMEOUT_S=60
SESSION_TTL_S=3600

# ===== 日志 =====
LOG_LEVEL=INFO
LOG_TO_FILE=false
LOG_DIR=logs

# ===== Feature Flags =====
SKILL_DAG_ENABLED=true
TOOL_CACHE_ENABLED=false
```

---

## 3. LLM Client(`infra/llm.py`)

### 3.1 现状

[infra/llm.py](../infra/llm.py) 封装了 `OpenAI` SDK:

```python
class LLMClient:
    def chat(self, messages, temperature=None, stream=False) -> str: ...
    def complete(self, prompt, temperature=None) -> str: ...
```

### 3.2 问题

1. **`stream=True` 不支持**:传了也不真流式,只同步等完整响应
2. **无重试 / 限流**:429 / 超时直接抛异常,上层要重复实现
3. **无 token 计数**:无法做 Context 压缩
4. **无 prompt 缓存提示**:某些模型支持 cache_control 头,未利用
5. **只支持 OpenAI 兼容协议**:虽然 OpenAI SDK 兼容多家,但未做 Provider 抽象

### 3.3 改进设计

```python
# infra/llm.py
from typing import AsyncIterator, List, Dict, Optional
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError


class LLMClient:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            timeout=config.request_timeout_s,
        )
        self.model = config.llm_model
        self.default_temperature = config.temperature

    async def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
        stream: bool = False,
    ) -> str | AsyncIterator[str]:
        """返回:
        - stream=False: 完整字符串
        - stream=True:  async iterator,每个 yield 是一个 token 字符串
        """
        ...

    async def chat_with_retry(
        self,
        messages: List[Dict[str, str]],
        *,
        max_retries: int = 3,
        **kwargs,
    ) -> str:
        """对 RateLimitError / APITimeoutError 指数退避重试。"""
        for attempt in range(max_retries):
            try:
                return await self.chat(messages, stream=False, **kwargs)
            except (RateLimitError, APITimeoutError) as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    async def stream(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]:
        kwargs = {"model": self.model, "messages": messages,
                  "temperature": temperature or self.default_temperature,
                  "stream": True}
        async for chunk in await self.client.chat.completions.create(**kwargs):
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def count_tokens(self, messages: List[Dict[str, str]]) -> int:
        """估算 token 数(tiktoken 或简单字符估算)。
        用于 Context 压缩触发判断。
        """
        try:
            import tiktoken
            enc = tiktoken.encoding_for_model(self.model)
            return sum(len(enc.encode(m["content"])) for m in messages)
        except Exception:
            # 兜底:粗估
            return sum(len(m["content"]) for m in messages) // 2
```

### 3.4 BaseAgent 集成

`BaseAgent.think()` 内部使用 `chat_with_retry`;`Orchestrator.stream()` 使用 `stream()`。

---

## 4. Logger(`infra/logger.py`)

### 4.1 现状

[infra/logger.py](../infra/logger.py) 实现:

- `LogLevel` / `LogType` 枚举(20+ 类型)
- `LogEntry` / `TraceContext` / `Logger`
- 单例(`__new__`)
- 订阅者模式(`subscribe(callback)`)
- **用 `print()` 输出**

### 4.2 问题

| # | 问题 | 后果 |
|---|---|---|
| L1 | `print` 输出 | 与 uvicorn / pytest 难以整合,无法分级别过滤 |
| L2 | `LogEntry.to_dict()` 中 `data` 字段可能含非 JSON 对象 | 前端解析炸 |
| L3 | `TraceContext.add_entry` 加锁但 `_log` 又再次加同一把锁 | 性能浪费(轻微) |
| L4 | `subscribe` 无去重 | 同一回调多次注册就多次调用 |
| L5 | 没有日志文件落地 | 服务挂了日志全没 |
| L6 | 没有按 trace_id 查询接口 | 调试不便 |

### 4.3 改进设计

#### 4.3.1 改造为标准 logging + 自定义 Handler

```python
# infra/logger.py
import logging
import json
import threading
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
from dataclasses import dataclass, field

from infra.config import config


# ----- 类型保持兼容 -----
class LogLevel(Enum):
    DEBUG = "debug"; INFO = "info"; WARNING = "warning"; ERROR = "error"

class LogType(Enum):
    FLOW_START = "flow_start"; FLOW_END = "flow_end"; FLOW_STEP = "flow_step"; FLOW_BRANCH = "flow_branch"
    DATA_INPUT = "data_input"; DATA_OUTPUT = "data_output"; DATA_TRANSFORM = "data_transform"; DATA_STORE = "data_store"
    AGENT_INTENT = "agent_intent"; AGENT_PLAN = "agent_plan"; AGENT_TOOL = "agent_tool"; AGENT_RESULT = "agent_result"
    TOOL_CALL = "tool_call"; TOOL_SUCCESS = "tool_success"; TOOL_ERROR = "tool_error"
    LLM_REQUEST = "llm_request"; LLM_RESPONSE = "llm_response"; LLM_ERROR = "llm_error"


# ----- 标准 logging 体系 -----
_std_logger = logging.getLogger("skill_agent")
_std_logger.setLevel(config.log_level.upper())
_std_logger.propagate = False

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
))
_std_logger.addHandler(_console_handler)

if config.log_to_file:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(config.log_dir / "skill_agent.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        '{"ts": "%(asctime)s", "level": "%(levelname)s", "msg": %(message)s}'
    ))
    _std_logger.addHandler(file_handler)


# ----- LogEntry(给订阅者用) -----
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
            "data": self.data or {},
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "duration_ms": self.duration_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


# ----- Trace 管理 -----
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
        return {
            "trace_id": self.trace_id,
            "duration_ms": (datetime.now() - self.start_time).total_seconds() * 1000,
            "total_steps": len(self.entries),
        }


# ----- 主 Logger(单例,兼容旧 API) -----
class Logger:
    _instance: Optional["Logger"] = None
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
        self._current_trace = None
        self.info(LogType.FLOW_END, "Logger", "结束追踪", summary)
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

        # 标准 logging 落地
        std_level = {
            LogLevel.DEBUG: logging.DEBUG, LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING, LogLevel.ERROR: logging.ERROR,
        }[level]
        _std_logger.log(std_level, entry.to_json())

    # ----- 便捷方法(保持旧 API) -----
    def debug(self, log_type, component, message, data=None):   self._log(LogLevel.DEBUG,   log_type, component, message, data)
    def info(self,  log_type, component, message, data=None):   self._log(LogLevel.INFO,    log_type, component, message, data)
    def warning(self,log_type, component, message, data=None):  self._log(LogLevel.WARNING, log_type, component, message, data)
    def error(self, log_type, component, message, data=None):   self._log(LogLevel.ERROR,   log_type, component, message, data)

    def log_flow(self, component, step, data=None):
        self.info(LogType.FLOW_STEP, component, step, data)

    def log_data(self, component, direction, data_name, data_value):
        log_type = LogType.DATA_INPUT if direction == "in" else LogType.DATA_OUTPUT
        self.info(log_type, component, f"数据{direction}: {data_name}",
                  {"data_name": data_name, "preview": str(data_value)[:200]})

    def log_tool_call(self,   tool, params):  self.info(LogType.TOOL_CALL, tool, "工具调用", {"params": params})
    def log_tool_success(self,tool, result):  self.info(LogType.TOOL_SUCCESS, tool, "工具成功", {"preview": str(result)[:200]})
    def log_tool_error(self,  tool, error):   self.error(LogType.TOOL_ERROR, tool, "工具失败", {"error": error})


def get_logger() -> Logger:
    return Logger()
```

#### 4.3.2 关键改进点

| 改动 | 解决的问题 |
|---|---|
| 标准 logging + 文件 handler | 日志可落地、可过滤 |
| `subscribe` 去重 | 避免重复推送 |
| `_sub_lock` + 通知时拷贝列表 | 回调里再 subscribe 不会死锁 |
| `data` 默认空 dict | 前端解析更稳 |
| `to_json()` + 兜底 `default=str` | 复杂对象也能序列化 |
| `get_trace()` 暴露查询 | 调试 / 复盘 |

---

## 5. 数据安全与可观测性

### 5.1 敏感字段脱敏

工具 / LLM 调用时 `data` 可能含 `api_key` / `password`:

```python
_SENSITIVE_KEYS = {"api_key", "token", "password", "secret", "authorization"}

def _sanitize(data):
    if isinstance(data, dict):
        return {k: ("***" if k.lower() in _SENSITIVE_KEYS else _sanitize(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_sanitize(v) for v in data]
    return data
```

在 `_log` / `to_dict` 时统一调用。

### 5.2 Trace 持久化(可选)

```python
class TraceStore:
    """把 TraceContext 落 SQLite,事后可查询。
    短期内存即可,后期接入。
    """
    def save(self, trace: TraceContext): ...
    def query(self, trace_id: str) -> Optional[Dict]: ...
```

---

## 6. 测试要点

| 组件 | 测试 |
|---|---|
| Config | `.env` 缺失 / 非法值 / 默认值 |
| LLMClient | mock OpenAI 客户端 / 重试 / 流式 |
| Logger | subscribe 去重 / TraceContext / 敏感字段脱敏 |
| 全链路 | 一次 chat 后 trace 包含所有 LLM / 工具调用 |

---

## 7. 迁移 checklist

- [ ] `requirements.txt` 增加 `pydantic-settings`、`tiktoken`
- [ ] 改写 `infra/config.py`
- [ ] 补 `.env.example`
- [ ] `LLMClient` 改 `AsyncOpenAI`,加 `chat_with_retry` / `stream` / `count_tokens`
- [ ] `Logger` 改用标准 logging,加文件 handler / 敏感脱敏
- [ ] 所有 `print(...)` 替换为 `logger.xxx(...)`
- [ ] 写 infra 单元测试