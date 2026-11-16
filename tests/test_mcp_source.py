"""MCP 工具源的测试。

重点不在"MCP 协议怎么用",而在**接线和配置健壮性**:

1. 命令解析 —— `npx`/`uvx` 这类 MCP 服务器不能被当 python 脚本跑
2. 配置解析 —— 坏 JSON 不能拖垮启动;单个服务器坏了不影响其它
3. 生命周期 —— 连接失败要能被状态机记录,而不是静默消失
"""
import asyncio
import json

import pytest

from tools.sources.base import SourceType
from tools.sources.mcp_source import (
    MCPSource,
    create_mcp_source,
    parse_mcp_servers,
    parse_server_command,
)


# ============================================================
# 1. 命令解析
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    # 最常见的形态:MCP 服务器是 npx/uvx 起的,不是 .py
    ("npx -y @modelcontextprotocol/server-filesystem D:\\workspace",
     ["npx", "-y", "@modelcontextprotocol/server-filesystem", "D:\\workspace"]),
    ("uvx mcp-server-git --repository .",
     ["uvx", "mcp-server-git", "--repository", "."]),
])
def test_parse_server_command(raw, expected):
    assert parse_server_command(raw) == expected


def test_parse_server_command_uses_interpreter_for_py():
    """`.py` 结尾才用当前解释器 —— 原实现把所有非 .exe 都当 python 跑。"""
    import sys

    cmd = parse_server_command("my_server.py --port 8080")
    assert cmd[0] == sys.executable
    assert cmd[1].endswith("my_server.py")
    assert "--port" in cmd


def test_parse_server_command_npx_is_not_python():
    """回归:`npx -y xxx` 绝不能变成 `python npx -y xxx`。"""
    import sys

    cmd = parse_server_command("npx -y @modelcontextprotocol/server-filesystem")
    assert cmd[0] != sys.executable
    assert cmd[0] == "npx"


def test_parse_server_command_keeps_quoted_path():
    """Windows 路径里的空格不能被拆开。"""
    cmd = parse_server_command('"C:/Program Files/node.exe" server.js')
    assert cmd[0] == "C:/Program Files/node.exe"
    assert cmd[1] == "server.js"


def test_parse_server_command_empty_raises():
    with pytest.raises(ValueError):
        parse_server_command("")


# ============================================================
# 2. 配置解析
# ============================================================

def test_parse_mcp_servers_empty():
    assert parse_mcp_servers("") == []
    assert parse_mcp_servers("   ") == []
    assert parse_mcp_servers(None) == []


def test_parse_mcp_servers_bad_json_is_ignored():
    """坏配置不能抛异常 —— 它发生在启动路径上,抛异常 = 起不来。"""
    assert parse_mcp_servers("{not json") == []
    assert parse_mcp_servers("[unclosed") == []


def test_parse_mcp_servers_single_object():
    sources = parse_mcp_servers(
        json.dumps({"name": "fs", "type": "stdio", "server_path": "npx -y x"})
    )
    assert len(sources) == 1
    assert sources[0].name == "fs"
    assert sources[0].type == SourceType.MCP


def test_parse_mcp_servers_multiple():
    raw = json.dumps([
        {"name": "fs", "type": "stdio", "server_path": "npx -y fs"},
        {"name": "remote", "type": "http", "server_url": "https://e.com/mcp"},
    ])
    sources = parse_mcp_servers(raw)
    assert [s.name for s in sources] == ["fs", "remote"]
    assert sources[1].config["server_url"] == "https://e.com/mcp"


def test_parse_mcp_servers_skips_incomplete():
    """缺关键字段的条目要跳过,不能产生一个连不上的源。"""
    raw = json.dumps([
        {"name": "no_path", "type": "stdio"},
        {"name": "no_url", "type": "http"},
        {"name": "bad_type", "type": "grpc", "server_path": "x"},
        {"name": "ok", "type": "stdio", "server_path": "npx -y x"},
    ])
    sources = parse_mcp_servers(raw)
    assert [s.name for s in sources] == ["ok"]


def test_parse_mcp_servers_respects_enabled_flag():
    raw = json.dumps([
        {"name": "off", "type": "stdio", "server_path": "x", "enabled": False},
        {"name": "on", "type": "stdio", "server_path": "x"},
    ])
    sources = parse_mcp_servers(raw)
    assert [s.name for s in sources] == ["on"]


def test_parse_mcp_servers_skips_non_dict():
    raw = json.dumps(["not a dict", {"name": "ok", "type": "stdio", "server_path": "x"}])
    sources = parse_mcp_servers(raw)
    assert len(sources) == 1


# ============================================================
# 3. 生命周期
# ============================================================

@pytest.mark.asyncio
async def test_connect_failure_is_recorded_not_raised():
    """连不上要能被状态机记录 —— 不能整个启动失败。"""
    from tools.hub import ToolHub

    hub = ToolHub()
    src = create_mcp_source(
        name="broken",
        connection_type="stdio",
        server_path="definitely-not-a-real-command-xyz",
    )
    hub.register_source(src)
    with pytest.raises(Exception):
        await hub.connect_source("broken")

    status = hub.get_source_status()["broken"]
    assert status["state"] == ToolHub.SOURCE_STATE_FAILED
    assert status["error"], "失败原因要记下来"


@pytest.mark.asyncio
async def test_connect_all_survives_one_bad_server():
    """一个服务器坏了,不能影响其它服务器的连接。"""
    from tools.hub import ToolHub

    hub = ToolHub()
    hub.register_source(
        create_mcp_source(name="bad", server_path="definitely-not-real-xyz")
    )
    hub.register_source(
        create_mcp_source(name="bad2", server_path="also-not-real-xyz")
    )
    # connect_all 内部对每个源单独 try,整体不抛
    await hub.connect_all()

    statuses = hub.get_source_status()
    assert statuses["bad"]["state"] == ToolHub.SOURCE_STATE_FAILED
    assert statuses["bad2"]["state"] == ToolHub.SOURCE_STATE_FAILED
    assert hub.health_summary()["has_failures"] is True


def test_mcp_source_config_is_carried():
    src = create_mcp_source(
        name="fs", connection_type="stdio", server_path="npx -y x"
    )
    assert src.source.type == SourceType.MCP
    assert src.source.config["type"] == "stdio"
    assert src.source.config["server_path"] == "npx -y x"


def test_mcptool_schema_conversion():
    """MCP 的 inputSchema 要能转成项目的 ToolSchema。"""
    from tools.sources.mcp_source import MCPTool

    src = create_mcp_source(name="x")
    tool = MCPTool(
        name="read_file",
        description="读文件",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "n": {"type": "integer", "description": "行数"},
            },
            "required": ["path"],
        },
        source=src,
    )
    schema = tool.schema()
    assert schema.name == "read_file"
    names = {p.name: p for p in schema.params}
    assert names["path"].required is True
    assert names["n"].required is False
    assert names["path"].type == "string"


def test_get_tool_schema_handles_raw_dict():
    """回归:`self._tools` 存的是服务器返回的原始 dict,不是 MCPTool。

    原实现直接 `tool.schema()` → `AttributeError: 'dict' object has no
    attribute 'schema'`,连接成功后加载工具时炸掉。
    """
    from tools.sources.mcp_source import MCPSource

    src = create_mcp_source(name="x")
    source = MCPSource(src)
    # 模拟 _discover_tools 塞进去的原始 dict
    source._tools["read_file"] = {
        "name": "read_file",
        "description": "读文件",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "路径"}},
            "required": ["path"],
        },
    }
    schema = source.get_tool_schema("read_file")
    assert schema is not None
    assert schema.name == "read_file"
    assert schema.params[0].name == "path"
    assert schema.params[0].required is True


def test_get_tool_schema_missing_returns_none():
    from tools.sources.mcp_source import MCPSource

    source = create_mcp_source(name="x")
    assert source.get_tool_schema("nope") is None


@pytest.mark.asyncio
async def test_read_response_times_out():
    """服务器不回话 → 超时报错,**不能挂死**。

    实机踩过:一个不响应(或回非 JSON)的 MCP 服务器会让整个 agent
    启动卡住,因为 `_read_response` 是裸的 `readline()`。
    """
    from tools.sources.mcp_source import MCPSource

    source = create_mcp_source(name="silent")
    source.read_timeout_s = 5

    class _FakeReader:
        async def readline(self):
            await asyncio.sleep(3600)   # 永远不返回
            return b""

    source._reader = _FakeReader()
    with pytest.raises(TimeoutError):
        await source._read_response()


@pytest.mark.asyncio
async def test_read_response_rejects_non_json():
    """服务器回非 JSON → 明确报错,而不是 json.loads 的原始异常。"""
    from tools.sources.mcp_source import MCPSource

    source = create_mcp_source(name="noisy")

    class _FakeReader:
        async def readline(self):
            return b"npm WARN exec something\n"

    source._reader = _FakeReader()
    with pytest.raises(ValueError) as exc:
        await source._read_response()
    assert "非 JSON" in str(exc.value)


@pytest.mark.asyncio
async def test_read_response_detects_closed_connection():
    from tools.sources.mcp_source import MCPSource

    source = create_mcp_source(name="gone")

    class _FakeReader:
        async def readline(self):
            return b""

    source._reader = _FakeReader()
    with pytest.raises(ConnectionError):
        await source._read_response()


@pytest.mark.asyncio
async def test_initialize_does_not_read_response_twice(monkeypatch):
    """回归:`initialize` 的响应只能读一次。

    原实现里 `_send_request` 内部已经读了响应,
    `_connect_stdio` 又调了一次 `_read_response()` ——
    第二次等一条永远不会来的消息,
    表现为**连接一直挂着**。
    """
    import tools.sources.mcp_source as mod

    source = create_mcp_source(name="x")

    calls = {"n": 0}

    async def _counting_read():
        calls["n"] += 1
        return {"result": {"protocolVersion": "2024-11-05"}}

    async def _send(method, params):
        # 模拟 _send_request:它自己会读一次响应
        return await _counting_read()

    async def _notify(method, params):
        return None

    async def _discover():
        return None

    class _FakeProc:
        pid = 12345
        stdin = None
        stdout = None
        stderr = None

        def terminate(self):
            pass

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec)

    source._send_request = _send
    source._send_notification = _notify
    source._discover_tools = _discover
    source._read_response = _counting_read

    await source._connect_stdio("dummy")
    assert calls["n"] == 1, f"initialize 读了 {calls['n']} 次响应(应为 1)"


@pytest.mark.asyncio
async def test_disconnect_does_not_crash_on_stream_reader():
    """回归:断开时不能对 `StreamReader` 调 `close()`。

    `close()` 是 `StreamWriter` 的方法;`_reader` 是 `StreamReader`,
    没有这个方法 —— 原先对两者都调了,每次断开 MCP 都抛
    `AttributeError: 'StreamReader' object has no attribute 'close'`。
    """
    source = create_mcp_source(name="x")

    class _FakeReader:
        # 没有 close() —— 跟真的 StreamReader 一样
        async def readline(self):
            return b""

    class _FakeWriter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class _FakeProc:
        pid = 1

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            return 0

    source._reader = _FakeReader()
    writer = _FakeWriter()
    source._writer = writer
    source._process = _FakeProc()

    await source.disconnect()      # 不抛异常即通过
    assert source._reader is None
    assert source._writer is None
    assert writer.closed is True


@pytest.mark.asyncio
async def test_disconnect_cancels_stderr_task():
    """stderr 抽干任务必须被取消,否则会留一个悬挂的 task。"""
    source = create_mcp_source(name="x")

    async def _forever():
        await asyncio.sleep(3600)

    source._stderr_task = asyncio.create_task(_forever())
    task = source._stderr_task

    class _FakeProc:
        pid = 1

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            return 0

    source._process = _FakeProc()
    await source.disconnect()
    assert source._stderr_task is None
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


def test_mcpsource_proxies_name_and_config():
    """`create_mcp_source()` 返回实例、`parse_mcp_servers()` 返回规格 ——
    两种约定不一致会让调用方拿到
    `'MCPSource' object has no attribute 'name'`。
    这里保证实例也能直接 `.name` / `.config`。
    """
    src = create_mcp_source(name="fs", server_path="npx -y x")
    assert src.name == "fs"
    assert src.config["server_path"] == "npx -y x"
