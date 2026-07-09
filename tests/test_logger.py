"""日志系统测试"""
import logging
import pytest

from infra import logger as logger_mod


@pytest.fixture(autouse=True)
def _reset_logger_singleton(monkeypatch):
    """每个测试都重置 logger 单例,避免 handler 累加"""
    monkeypatch.setattr(logger_mod, "_logger", None)
    yield
    logger_mod._logger = None


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
