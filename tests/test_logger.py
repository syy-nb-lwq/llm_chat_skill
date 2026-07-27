"""日志系统测试"""
import logging

import pytest

from infra import logger as logger_mod


@pytest.fixture(autouse=True)
def _reset_logger_singleton(monkeypatch):
    """每个测试都重置 logger 单例,避免 handler 累加"""
    stdlib_logger = logging.getLogger("skill_agent")
    stdlib_logger.handlers.clear()
    monkeypatch.setattr(logger_mod, "_logger", None)
    yield
    logger_mod._logger = None
    stdlib_logger.handlers.clear()


def test_get_logger_returns_singleton():
    a = logger_mod.get_logger()
    b = logger_mod.get_logger()
    assert a is b


def test_logger_keeps_existing_handlers_when_recreated(monkeypatch):
    """重新 import 后创建 logger 不应重复添加 handler"""
    log1 = logger_mod.get_logger()
    log2 = logger_mod.get_logger()
    # stdlib logger handler 数量应稳定
    assert len(log1._logger.handlers) == len(log2._logger.handlers)


def test_logger_respects_log_level_from_config(monkeypatch):
    """config.log_level 控制 logger 级别"""
    from infra.config import Config
    fake_cfg = Config(llm_api_key="x", log_level="WARNING")
    # 替换 logger 模块里绑定的 config 引用
    monkeypatch.setattr(logger_mod, "config", fake_cfg)
    log = logger_mod.get_logger()
    assert log._logger.level == logging.WARNING


def test_logger_exposes_legacy_api():
    """保留 SimpleLogger 旧 API,防止破坏调用方"""
    log = logger_mod.get_logger()
    log.info("Comp", "info msg")
    log.error("Comp", "error msg")
    log.warning("Comp", "warning msg")
    log.debug("Comp", "debug msg")
    log.log_flow("Comp", "flow msg")
    log.log_data("Comp", "in", "name", 1)
    log.log_data("Comp", "out", "name", 2)


def test_logger_subscribe_and_unsubscribe():
    log = logger_mod.get_logger()
    received = []

    def cb(level, component, message, data):
        received.append((level, component, message, data))

    log.subscribe(cb)
    log.info("X", "hello")
    log.error("X", "boom", data={"k": 1})
    assert len(received) == 2
    assert received[0][0] == logging.INFO
    assert received[0][1] == "X"
    assert received[0][2] == "hello"
    assert received[1][3] == {"k": 1}

    log.unsubscribe(cb)
    log.info("X", "after")
    assert len(received) == 2  # 没新增


def test_logger_subscriber_exception_doesnt_break():
    log = logger_mod.get_logger()
    called = []

    def bad_cb(*a, **kw):
        called.append(1)
        raise RuntimeError("nope")

    log.subscribe(bad_cb)
    # 不应抛
    log.info("X", "y")
    assert called == [1]


# ===== P1-04: 后台任务 done_callback 测试 =====


def test_log_task_exception_logs_on_failure():
    """任务异常退出时,done_callback 应记录 error 日志(P1-04)。"""
    import asyncio

    log = logger_mod.get_logger()
    received: list = []

    def cb(level, component, message, data):
        received.append((level, component, message, data))

    log.subscribe(cb)

    async def boom():
        raise RuntimeError("intentional failure")

    async def _drive():
        task = asyncio.create_task(boom())
        task.add_done_callback(logger_mod.log_task_exception)
        try:
            await task
        except RuntimeError:
            pass

    asyncio.run(_drive())

    # 应有一条 ERROR 级别日志
    errors = [r for r in received if r[0] == logging.ERROR]
    assert len(errors) == 1, f"expected 1 ERROR log, got {errors}"
    msg = errors[0][2]
    assert "intentional failure" in msg
    assert "traceback" in (errors[0][3] or {}).keys() or "traceback" in str(errors[0][3])


def test_log_task_exception_skips_cancelled():
    """已 cancel 的任务不应记录 error(P1-04)。"""
    import asyncio

    log = logger_mod.get_logger()
    received: list = []

    def cb(level, component, message, data):
        received.append((level, component, message, data))

    log.subscribe(cb)

    async def slow():
        await asyncio.sleep(10)

    async def _drive():
        task = asyncio.create_task(slow())
        task.add_done_callback(logger_mod.log_task_exception)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())

    errors = [r for r in received if r[0] == logging.ERROR]
    assert len(errors) == 0, f"cancelled task should not log ERROR, got {errors}"


def test_log_task_exception_skips_success():
    """正常完成的任务不应记录 error(P1-04)。"""
    import asyncio

    log = logger_mod.get_logger()
    received: list = []

    def cb(level, component, message, data):
        received.append((level, component, message, data))

    log.subscribe(cb)

    async def ok():
        return 42

    async def _drive():
        task = asyncio.create_task(ok())
        task.add_done_callback(logger_mod.log_task_exception)
        await task

    asyncio.run(_drive())

    errors = [r for r in received if r[0] == logging.ERROR]
    assert len(errors) == 0, f"successful task should not log ERROR, got {errors}"


# ===== P1-05: 结构化日志测试 =====


class _ListHandler(logging.Handler):
    """收集日志到列表的测试用 handler。"""
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(self.format(record))


def test_json_formatter_outputs_json(monkeypatch):
    """log_format=json 时,日志输出应为单行 JSON(P1-05)。"""
    import json as _json

    from infra.config import Config
    fake_cfg = Config(llm_api_key="x", log_level="INFO", log_format="json")
    monkeypatch.setattr(logger_mod, "config", fake_cfg)
    log = logger_mod.get_logger()
    handler = _ListHandler()
    handler.setFormatter(logger_mod.JsonFormatter())
    log._logger.addHandler(handler)
    log.info("TestComp", "hello world", data={"k": "v"})
    line = handler.records[0]
    payload = _json.loads(line)  # 能解析为 JSON 即为单行 JSON
    assert payload["level"] == "INFO"
    assert payload["component"] == "TestComp"
    assert payload["msg"] == "hello world"
    assert payload["data"] == {"k": "v"}
    assert "ts" in payload


def test_json_formatter_includes_execution_id_from_context(monkeypatch):
    """JSON 模式下,bind_log_context 设置的 execution_id 应出现在日志中(P1-05)。"""
    import json as _json

    from infra.config import Config
    fake_cfg = Config(llm_api_key="x", log_level="INFO", log_format="json")
    monkeypatch.setattr(logger_mod, "config", fake_cfg)
    log = logger_mod.get_logger()
    handler = _ListHandler()
    handler.setFormatter(logger_mod.JsonFormatter())
    log._logger.addHandler(handler)

    token = logger_mod.bind_log_context(
        execution_id="exec_test_123",
        trace_id="trace_test_456",
    )
    try:
        log.info("Agent", "processing")
    finally:
        logger_mod.reset_log_context(token)

    payload = _json.loads(handler.records[0])
    assert payload["execution_id"] == "exec_test_123"
    assert payload["trace_id"] == "trace_test_456"


def test_text_format_remains_backward_compatible(monkeypatch):
    """log_format=text(默认)时,日志格式应与旧格式一致(P1-05 向后兼容)。"""
    from infra.config import Config
    fake_cfg = Config(llm_api_key="x", log_level="INFO", log_format="text")
    monkeypatch.setattr(logger_mod, "config", fake_cfg)
    log = logger_mod.get_logger()
    handler = _ListHandler()
    handler.setFormatter(logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    log._logger.addHandler(handler)
    log.info("Comp", "msg", data={"x": 1})
    line = handler.records[0]
    # 旧格式: [HH:MM:SS] LEVEL name: Comp: msg | {'x': 1}
    assert line.startswith("[")
    assert "INFO" in line
    assert "Comp: msg" in line
    # 不应是 JSON
    assert not line.startswith("{")


def test_bind_log_context_isolates_concurrent_tasks():
    """contextvars 应在不同 async 任务间隔离(P1-05)。"""
    import asyncio

    async def task_a():
        token = logger_mod.bind_log_context(execution_id="A")
        await asyncio.sleep(0.01)
        ctx = logger_mod.get_log_context()
        logger_mod.reset_log_context(token)
        return ctx.get("execution_id")

    async def task_b():
        token = logger_mod.bind_log_context(execution_id="B")
        await asyncio.sleep(0.01)
        ctx = logger_mod.get_log_context()
        logger_mod.reset_log_context(token)
        return ctx.get("execution_id")

    async def _main():
        return await asyncio.gather(task_a(), task_b())

    results = asyncio.run(_main())
    assert set(results) == {"A", "B"}
