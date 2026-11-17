"""MCP 工具源 - 接入 MCP (Model Context Protocol) 协议的工具"""
import asyncio
import json
import shlex
import sys
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from tools.base import ToolParam, ToolResult, ToolSchema
from tools.sources.base import SourceType, ToolSource, ToolSourceBase


def parse_server_command(server_path: str) -> List[str]:
    """把配置里的 `server_path` 解析成可执行命令。

    配置形如:
        `npx -y @modelcontextprotocol/server-filesystem D:\\workspace`
        `uvx mcp-server-git --repository .`
        `python C:\\tools\\my_server.py --port 8080`
        `"C:\\Program Files\\node.exe" server.js`

    **踩过的坑**:原先写的是
        `sys.executable if not server_path.endswith(".exe") else server_path`
    于是 `npx -y xxx` 会变成 `python npx -y xxx` —— python 去执行一个叫 npx 的
    脚本,必然失败。MCP 服务器绝大多数是 `npx`/`uvx` 起的,**不是** .py 文件,
    所以不能默认用 python 解释器。

    规则:
    1. 首个 token 以 `.py` 结尾 → 用当前解释器跑它
    2. 否则 → 首个 token 就是可执行文件本身
    3. 引号内的空格要保留(Windows 路径常见)
    """
    raw = (server_path or "").strip()
    if not raw:
        raise ValueError("server_path 不能为空")

    # posix=False:保留引号,避免 Windows 反斜杠被当转义符
    tokens = shlex.split(raw, posix=False)
    cleaned: List[str] = []
    for t in tokens:
        t = t.strip()
        if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
            t = t[1:-1]
        if t:
            cleaned.append(t)

    if not cleaned:
        raise ValueError(f"无法解析 server_path: {server_path!r}")

    if cleaned[0].lower().endswith(".py"):
        return [sys.executable, *cleaned]
    return cleaned


def build_tool_schema(name: str, description: str, input_schema: Dict) -> ToolSchema:
    """把 MCP 的 inputSchema 转成项目的 ToolSchema。

    MCP 协议文档: https://modelcontextprotocol.io/
    """
    params = []
    properties = (input_schema or {}).get("properties", {}) or {}
    required = (input_schema or {}).get("required", []) or []

    for param_name, param_info in properties.items():
        params.append(ToolParam(
            name=param_name,
            type=(param_info or {}).get("type", "string"),
            description=(param_info or {}).get("description", ""),
            required=param_name in required,
        ))

    return ToolSchema(
        name=name,
        description=description,
        params=params,
    )


class MCPTool:
    """MCP 工具封装"""

    def __init__(self, name: str, description: str, input_schema: Dict, source: "MCPSource"):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._source = source

    def schema(self) -> ToolSchema:
        """转换为标准 ToolSchema"""
        return build_tool_schema(self.name, self.description, self.input_schema)

    async def execute(self, **kwargs) -> ToolResult:
        """执行 MCP 工具"""
        return await self._source.call_tool(self.name, kwargs)


class MCPSource(ToolSourceBase):
    """MCP 工具源

    通过 stdio 或 HTTP 连接 MCP 服务器,获取可用工具列表并执行。
    
    MCP 协议文档: https://modelcontextprotocol.io/
    """

    def __init__(self, source: ToolSource, read_timeout_s: int = 30):
        super().__init__(source)
        self._connected = False
        self._process = None
        self._reader = None
        self._writer = None
        self._request_id = 0
        #: 读取响应的超时(秒)。**必须有** —— 否则一个不回话的服务器
        #: 能让整个 agent 启动卡死(实测踩过)。
        # 下限 1 秒:超时设计本身就不应该长超过几秒——
        # 下限 5 秒时测试要等 5 秒,整套慢一百多秒。
        self.read_timeout_s = max(1, int(read_timeout_s))
        #: stderr 尾部(诊断用),由后台任务持续收集
        self._stderr_tail: List[str] = []
        self._stderr_task: Optional[asyncio.Task] = None
        # ===== 并发支持 =====
        #
        # **JSON-RPC over 单管道有两个硬性要求,少一个就会炸:**
        #
        # 1. **同一时刻只能有一个协程在读管道。**
        #    两个协程同时 `readline()` 同一个 StreamReader,asyncio 会直接抛
        #    `readuntil() called while another coroutine is already waiting for
        #    incoming data`。
        #    → 解法:一个**专职读协程**独占管道,其他协程通过 Future 拿结果。
        #
        # 2. **响应必须按 id 分发给对应的请求。**
        #    允许多个请求同时在途时,响应回来的顺序不保证和请求一致。
        #    → 解法:`_pending` 表:id → Future。
        #
        # **踩过的坑**:最早的实现是"发请求 → 直接读一行 → 当作响应",
        # 没有锁、没有 id 匹配。并行调两个 MCP 工具时 15 个调用只成了 1 个,
        # 报的错就是上面那句 asyncio 内部错误。
        # 这不只是 MCP 的问题 —— `agents/learning.py` 会用 `asyncio.create_task`
        # 并行执行工具任务,所以并行是常态,不是边缘情况。
        self._write_lock = asyncio.Lock()
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    # ===== 代理到 ToolSource =====
    #
    # `create_mcp_source()` 返回 **MCPSource 实例**,
    # 而 `parse_mcp_servers()` 返回 **ToolSource 规格** ——
    # 两种约定不一致,调用方一不小心就会拿到
    # `'MCPSource' object has no attribute 'name'`。
    # 这里把常用字段代理出来,两种拿法都能用。

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def config(self) -> Dict[str, Any]:
        return self.source.config

    @property
    def description(self) -> str:
        return self.source.description

    async def connect(self) -> bool:
        """连接 MCP 服务器
        
        支持两种连接方式:
        1. stdio: 通过子进程 stdio 通信
        2. http: 通过 HTTP API 通信
        """
        connection_type = self.source.config.get("type", "stdio")
        server_path = self.source.config.get("server_path", "")
        server_url = self.source.config.get("server_url", "")

        try:
            if connection_type == "stdio":
                return await self._connect_stdio(server_path)
            elif connection_type == "http":
                return await self._connect_http(server_url)
            else:
                raise ValueError(f"不支持的连接类型: {connection_type}")
        except Exception as e:
            self._connected = False
            raise e

    async def disconnect(self):
        """断开 MCP 连接"""
        self._tools.clear()
        self._connected = False

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None

        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

        # 让还在等响应的协程立刻知道,而不是干等到超时
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("MCP 连接已断开"))
        self._pending.clear()

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

        if self._writer:
            # 只有 StreamWriter 有 close();`_reader` 是 StreamReader,
            # 没有 close() 方法 —— 原先对两者都调 close(),
            # 每次断开 MCP 都会抛
            # `AttributeError: 'StreamReader' object has no attribute 'close'`。
            # 读侧不用管:进程已经终止,管道随之关闭。
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def list_tools(self) -> List[Any]:
        """列出所有 MCP 工具"""
        return [
            MCPTool(name=t["name"], description=t["description"], 
                   input_schema=t.get("inputSchema", {}), source=self)
            for t in self._tools.values()
        ]

    async def call_tool(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """调用 MCP 工具"""
        if not self._connected:
            return ToolResult(success=False, error="MCP 源未连接")

        tool = self._tools.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"工具不存在: {tool_name}")

        try:
            if self._process:
                # stdio 通信
                result = await self._call_stdio(tool_name, params)
            else:
                # HTTP 通信
                result = await self._call_http(tool_name, params)

            if result.get("error"):
                return ToolResult(success=False, error=result["error"])

            return ToolResult(success=True, data=result.get("content", result))
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def get_tool_schema(self, tool_name: str) -> Optional[ToolSchema]:
        """获取工具 schema。

        `self._tools` 里存的是服务器返回的**原始 dict**
        (`_discover_tools` 直接塞进去的),不是 MCPTool 对象 ——
        所以这里不能直接 `tool.schema()`,那会抛
        `'dict' object has no attribute 'schema'`。
        """
        raw = self._tools.get(tool_name)
        if raw is None:
            return None
        if isinstance(raw, dict):
            return build_tool_schema(
                raw.get("name") or tool_name,
                raw.get("description", ""),
                raw.get("inputSchema") or {},
            )
        schema_fn = getattr(raw, "schema", None)
        if callable(schema_fn):
            return schema_fn()
        return None

    async def _connect_stdio(self, server_path: str) -> bool:
        """通过 stdio 连接 MCP 服务器"""
        import os
        import sys

        # 启动 MCP 服务器进程
        cmd = parse_server_command(server_path)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = self._process.stdout
        self._writer = self._process.stdin

        # **专职读协程**:独占管道,按 id 把响应分发给等待者。
        # 其他协程一律不碰 `self._reader` —— 那是并发读的根源。
        self._reader_task = asyncio.create_task(self._pump_responses())

        # **必须持续抽干 stderr。**
        # stderr 是管道,缓冲区满了服务器就会阻塞在写 stderr 上,
        # 于是它不再读 stdin → 双方死锁。
        # npx/npm 第一次拉包时会往 stderr 打一堆日志,很容易撑满。
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 发送 initialize 请求。
        #
        # **注意这里不能再读一次响应** —— `_send_request` 内部已经读过了
        # (它 return 的就是响应)。多读一次会等一条永远不会来的消息,
        # 表现为"连接一直挂着" —— 这正是之前实测到的现象。
        response = await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "skill-agent", "version": "1.0"}
        })
        if response.get("error"):
            raise Exception(f"MCP 初始化失败: {response['error']}")

        # 发送 initialized 通知
        await self._send_notification("initialized", {})

        # 获取工具列表
        await self._discover_tools()
        self._connected = True
        return True

    async def _drain_stderr(self) -> None:
        """后台持续读 stderr,丢弃内容(只留最后几行用于诊断)。"""
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-20]
        except Exception:
            pass

    async def _pump_responses(self) -> None:
        """专职读协程:独占 stdout,按 id 把响应分发给等待者。

        **为什么必须有它**:asyncio 的 StreamReader 不允许多个协程同时读。
        并行调工具时,两个 `_send_request` 各自去读同一个 reader →
        `readuntil() called while another coroutine is already waiting`。
        实测:15 个并行调用只成了 1 个。

        这里只有**一个**协程读管道,读到的消息按 `id` 交给对应的 Future。
        没有 id 的是服务器发来的通知,忽略(目前用不上)。
        """
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except Exception:
                    self.logger.warning(
                        "MCP", f"{self.source.name} 返回非 JSON: {text[:120]!r}"
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is None:
                    # 通知/服务器主动消息 —— 目前没有用途,丢掉
                    continue
                fut = self._pending.get(msg_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("MCP", f"{self.source.name} 读取协程结束: {exc}")
        finally:
            # 服务器断开:让所有等待者立刻知道,而不是干等到超时
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(
                        ConnectionError(
                            f"MCP 服务器 {self.source.name} 关闭了连接"
                            + (
                                f"; stderr: {' | '.join(self._stderr_tail[-3:])}"
                                if self._stderr_tail
                                else ""
                            )
                        )
                    )

    async def _connect_http(self, server_url: str) -> bool:
        """通过 HTTP 连接 MCP 服务器"""
        import aiohttp
        
        self._server_url = server_url.rstrip("/")
        
        async with aiohttp.ClientSession() as session:
            # 尝试获取工具列表
            try:
                async with session.get(f"{self._server_url}/tools") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for tool in data.get("tools", []):
                            tool_name = tool.get("name")
                            if tool_name:
                                self._tools[tool_name] = {
                                    "name": tool_name,
                                    "description": tool.get("description", ""),
                                    "inputSchema": tool.get("inputSchema", {}),
                                }
                    self._connected = True
                    return True
            except Exception:
                pass

        # 简单 HTTP 模式:假设返回 JSON
        self._http_session = aiohttp.ClientSession()
        self._connected = True
        return True

    async def _discover_tools(self):
        """发现 MCP 工具"""
        try:
            response = await self._send_request("tools/list", {})
            tools = response.get("result", {}).get("tools", [])
            for tool in tools:
                tool_name = tool.get("name")
                if tool_name:
                    self._tools[tool_name] = tool
        except Exception:
            pass

    async def _call_stdio(self, tool_name: str, params: Dict) -> Dict:
        """通过 stdio 调用工具"""
        response = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": params,
        })
        return response.get("result", {})

    async def _call_http(self, tool_name: str, params: Dict) -> Dict:
        """通过 HTTP 调用工具"""
        async with self._http_session.post(
            f"{self._server_url}/tools/call",
            json={"name": tool_name, "arguments": params}
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            return {"error": f"HTTP {resp.status}"}

    async def _send_request(self, method: str, params: Dict) -> Dict:
        """发送 JSON-RPC 请求并等待**属于自己的**响应。

        **可以并发调用**:多个请求可以同时在途,响应由专职读协程按 id 分发。
        写操作用锁串行化(`write` + `drain` 必须是原子的,否则两个协程的
        JSON 会拼在一起,服务器直接解析失败)。
        """
        loop = asyncio.get_running_loop()
        async with self._write_lock:
            self._request_id += 1
            request_id = self._request_id
            fut: asyncio.Future = loop.create_future()
            self._pending[request_id] = fut

            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            request_str = json.dumps(request) + "\n"
            self._writer.write(request_str.encode())
            await self._writer.drain()

        try:
            return await asyncio.wait_for(fut, timeout=self.read_timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"MCP 服务器 {self.source.name} 对 {method} 在 "
                f"{self.read_timeout_s}s 内没有响应"
                + (f"; stderr: {' | '.join(self._stderr_tail[-3:])}" if self._stderr_tail else "")
            )
        finally:
            self._pending.pop(request_id, None)

    async def _send_notification(self, method: str, params: Dict):
        """发送 JSON-RPC 通知(无响应)"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        request_str = json.dumps(notification) + "\n"
        async with self._write_lock:
            self._writer.write(request_str.encode())
            await self._writer.drain()

    # 旧的 `_read_response()` 已删:读取现在由专职读协程 `_pump_responses()` 负责,
    # 请求方通过 `_pending` 表拿结果。
    # 保留一个"直接读一行"的方法很危险 ——
    # 它会和专职读协程抢同一个 StreamReader,并发时就会报
    # `readuntil() called while another coroutine is already waiting`.


def create_mcp_source(
    name: str = "mcp",
    connection_type: str = "stdio",
    server_path: str = "",
    server_url: str = "",
) -> MCPSource:
    """创建 MCP 工具源"""
    config = {
        "type": connection_type,
        "server_path": server_path,
        "server_url": server_url,
    }
    source = ToolSource(
        name=name,
        type=SourceType.MCP,
        config=config,
        description=f"MCP 工具源 ({connection_type})",
    )
    return MCPSource(source)


def parse_mcp_servers(raw: str) -> List[ToolSource]:
    """把 `MCP_SERVERS` 配置解析成 MCP 工具源列表。

    配置是 JSON 数组(单个服务器也允许写成对象):

    ```json
    [
      {"name": "filesystem", "type": "stdio",
       "server_path": "npx -y @modelcontextprotocol/server-filesystem D:\\workspace"},
      {"name": "git", "type": "stdio", "server_path": "uvx mcp-server-git --repository ."},
      {"name": "remote", "type": "http", "server_url": "https://example.com/mcp"}
    ]
    ```

    **坏配置不能拖垮启动** —— 解析失败只跳过那个服务器并记录原因,
    其余照常工作。多租户产品里,一个第三方 MCP 服务器挂掉
    不应该让整个 agent 起不来。
    """
    from infra.logger import get_logger

    logger = get_logger()
    text = (raw or "").strip()
    if not text:
        return []

    try:
        data = json.loads(text)
    except Exception as exc:
        logger.warning("MCP", f"MCP_SERVERS 不是合法 JSON,已忽略: {exc}")
        return []

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        logger.warning("MCP", "MCP_SERVERS 必须是数组或对象,已忽略")
        return []

    sources: List[ToolSource] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            logger.warning("MCP", f"MCP_SERVERS[{idx}] 不是对象,已跳过")
            continue
        name = str(item.get("name") or f"mcp_{idx}").strip()
        conn_type = str(item.get("type") or "stdio").strip().lower()
        if conn_type not in ("stdio", "http"):
            logger.warning("MCP", f"MCP 服务器 {name} 的 type={conn_type!r} 不支持,已跳过")
            continue
        server_path = str(item.get("server_path") or "").strip()
        server_url = str(item.get("server_url") or "").strip()
        if conn_type == "stdio" and not server_path:
            logger.warning("MCP", f"MCP 服务器 {name} 缺少 server_path,已跳过")
            continue
        if conn_type == "http" and not server_url:
            logger.warning("MCP", f"MCP 服务器 {name} 缺少 server_url,已跳过")
            continue
        if not item.get("enabled", True):
            logger.info("MCP", f"MCP 服务器 {name} 已禁用,跳过")
            continue

        sources.append(
            ToolSource(
                name=name,
                type=SourceType.MCP,
                enabled=True,
                config={
                    "type": conn_type,
                    "server_path": server_path,
                    "server_url": server_url,
                },
                description=str(item.get("description") or f"MCP 服务器 {name}"),
            )
        )
    return sources
