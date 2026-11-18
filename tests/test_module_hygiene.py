"""全仓库语法 + 关键模块可导入性。

## 为什么需要这个测试

实机踩过：`core/event_bus.py` 的 `set_emitter` 少了一个 `]`，
是**语法错误**。但没人发现，原因是：

1. 它只在 `agent.handle()` 里被**懒加载**（`try/except` 包着）
2. 懒加载失败被 `except Exception: pass` 吞掉
3. 现有测试全部通过 —— 因为压根没人 import 它

于是子智能体的事件**从来没被推送出去**，而所有测试都是绿的。

教训：**语法错误必须有一个不依赖"谁记得 import"的闸门。**
这里用 `ast.parse` 扫全仓库 —— 纯解析，不执行，快且无副作用。
"""
import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent

#: 不参与扫描的目录
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".pytest_cache",
    "dist", "build", "memory", "vector_store", ".vscode", ".idea",
}


def _iter_py_files():
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & _SKIP_DIRS:
            continue
        yield path


def test_all_python_files_parse():
    """全仓库每个 .py 都必须能解析。

    这条测试的价值在于：它不关心"谁记得 import 谁"。
    """
    broken = []
    for path in _iter_py_files():
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
        except UnicodeDecodeError as exc:
            broken.append(f"{path.relative_to(ROOT)} 编码错误: {exc}")
    assert not broken, "有文件语法错误:\n" + "\n".join(broken)


@pytest.mark.parametrize("module", [
    "core.event_bus",
    "core.goal_runner",
    "core.subagents",
    "core.capabilities",
    "core.correction",
    "core.skill_quality",
    "core.trace",
    "core.llm_judge",
    "core.intent_labels",
    "tools.proposal",
    "tools.declarative",
    "tools.script_tool",
    "tools.meta_tools",
    "tools.subagent_tool",
    "tools.document",
    "tools.python_exec",
    "tools.script_writer",
    "tools.web_fetch",
    "agents.orchestrator",
    "agents.learning",
])
def test_key_modules_import(module):
    """关键模块必须真的能 import。

    `ast.parse` 只查语法；这条查**导入期**错误（缺依赖、循环 import、
    import 了不存在的名字）。
    """
    import importlib

    importlib.import_module(module)


def test_event_bus_roundtrip():
    """event_bus 真的能用（不只是能 import）。"""
    import asyncio

    from core.event_bus import clear_emitter, emit, get_emitter, set_emitter

    received = []

    async def on_event(name, payload):
        received.append((name, payload))

    async def run():
        clear_emitter()
        # 没 emitter 时 emit 不能抛
        await emit("x", {"a": 1})
        assert received == []

        set_emitter(on_event)
        assert get_emitter() is on_event
        await emit("y", {"b": 2})

        clear_emitter()
        await emit("z", {})

    asyncio.run(run())
    assert received == [("y", {"b": 2})]


def test_event_bus_swallows_emitter_errors():
    """观测坏了不能拖垮任务 —— emit 失败必须静默。"""
    import asyncio

    from core.event_bus import clear_emitter, emit, set_emitter

    async def boom(name, payload):
        raise RuntimeError("emitter 炸了")

    async def run():
        set_emitter(boom)
        await emit("x", {})        # 不抛
        clear_emitter()

    asyncio.run(run())


def test_redact_secrets_masks_keys():
    """密钥不能以明文进日志(实机踩过: agent 把用户的 key 写进代码,
    而"执行工具: {params}"会把参数原样打进日志)。"""
    from infra.logger import redact_secrets

    assert "sk-api-4mlcprXetest1234567890" not in redact_secrets(
        "{'code': \"KEY = 'sk-api-4mlcprXetest1234567890'\"}"
    )
    assert "abcdefghijklmnop123456" not in redact_secrets(
        "Authorization: Bearer abcdefghijklmnop123456"
    )
    assert "SECRETVALUE12345" not in redact_secrets("api_key=SECRETVALUE12345")


def test_redact_secrets_leaves_normal_text():
    from infra.logger import redact_secrets

    text = "帮我查一下 Python 3.13 的新特性, 不需要密钥"
    assert redact_secrets(text) == text


def test_redact_never_raises():
    from infra.logger import redact_secrets

    assert redact_secrets(None) == ""
    assert isinstance(redact_secrets({"a": 1}), str)
