"""MCP 工具源 - 接入 MCP (Model Context Protocol) 协议的工具"""
import asyncio
import json
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from tools.base import ToolParam, ToolResult, ToolSchema
from tools.sources.base import SourceType, ToolSource, ToolSourceBase


class MCPTool:
    """MCP 工具封装"""

    def __init__(self, name: str, description: str, input_schema: Dict, source: "MCPSource"):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._source = source

    def schema(self) -> ToolSchema:
        """转换为标准 ToolSchema"""
        params = []
        properties = self.input_schema.get("properties", {})
        required = self.input_schema.get("required", [])

        for param_name, param_info in properties.items():
            params.append(ToolParam(
                name=param_name,
                type=param_info.get("type", "string"),
                description=param_info.get("description", ""),
                required=param_name in required,
            ))

        return ToolSchema(
            name=self.name,
            description=self.description,
            params=params,
        )

    async def execute(self, **kwargs) -> ToolResult:
        """执行 MCP 工具"""
        return await self._source.call_tool(self.name, kwargs)


class MCPSource(ToolSourceBase):
    """MCP 工具源

    通过 stdio 或 HTTP 连接 MCP 服务器,获取可用工具列表并执行。
    
    MCP 协议文档: https://modelcontextprotocol.io/
    """

    def __init__(self, source: ToolSource):
        super().__init__(source)
        self._connected = False
        self._process = None
        self._reader = None
        self._writer = None
        self._request_id = 0

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

        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._process.kill()
            self._process = None

        if self._reader:
            self._reader.close()
            self._writer.close()
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
        """获取工具 schema"""
        tool = self._tools.get(tool_name)
        if tool:
            return tool.schema()
        return None

    async def _connect_stdio(self, server_path: str) -> bool:
        """通过 stdio 连接 MCP 服务器"""
        import os
        import sys

        # 启动 MCP 服务器进程
        self._process = await asyncio.create_subprocess_exec(
            sys.executable if not server_path.endswith(".exe") else server_path,
            *server_path.split() if " " in server_path else [server_path],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = self._process.stdout
        self._writer = self._process.stdin

        # 发送 initialize 请求
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "skill-agent", "version": "1.0"}
        })

        # 读取响应
        response = await self._read_response()
        if response.get("error"):
            raise Exception(f"MCP 初始化失败: {response['error']}")

        # 发送 initialized 通知
        await self._send_notification("initialized", {})

        # 获取工具列表
        await self._discover_tools()
        self._connected = True
        return True

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
        """发送 JSON-RPC 请求"""
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        
        request_str = json.dumps(request) + "\n"
        self._writer.write(request_str.encode())
        await self._writer.drain()
        
        return await self._read_response()

    async def _send_notification(self, method: str, params: Dict):
        """发送 JSON-RPC 通知(无响应)"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        notification_str = json.dumps(notification) + "\n"
        self._writer.write(notification_str.encode())
        await self._writer.drain()

    async def _read_response(self) -> Dict:
        """读取响应"""
        line = await self._reader.readline()
        if not line:
            return {}
        return json.loads(line.decode())


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
