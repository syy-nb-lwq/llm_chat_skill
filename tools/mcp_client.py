"""MCP Client - Model Context Protocol 客户端

支持连接外部 MCP 服务器,动态发现和调用工具。
参考: https://modelcontextprotocol.io/
"""
import asyncio
import json
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Union
from pathlib import Path

from infra.logger import get_logger


class TransportType(Enum):
    """传输类型"""
    STDIO = "stdio"      # 标准输入输出
    SSE = "sse"         # Server-Sent Events
    HTTP = "http"        # HTTP
    WEBSOCKET = "websocket"  # WebSocket


@dataclass
class MCPError(Exception):
    """MCP 错误"""
    code: int
    message: str
    data: Optional[Any] = None


@dataclass
class ToolDefinition:
    """工具定义"""
    name: str
    description: str
    input_schema: Dict[str, Any]
    
    def to_openai_format(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass
class ToolResult:
    """工具调用结果"""
    tool: str
    result: Any
    is_error: bool = False
    error_message: Optional[str] = None


@dataclass
class MCPServerConfig:
    """MCP 服务器配置"""
    name: str
    command: str                    # 启动命令
    args: List[str] = field(default_factory=list)  # 命令参数
    env: Dict[str, str] = field(default_factory=dict)  # 环境变量
    transport: TransportType = TransportType.STDIO  # 传输类型


class BaseTransport(ABC):
    """传输层抽象"""
    
    @abstractmethod
    async def connect(self) -> None:
        """连接"""
        ...
    
    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接"""
        ...
    
    @abstractmethod
    async def send(self, message: Dict[str, Any]) -> None:
        """发送消息"""
        ...
    
    @abstractmethod
    async def receive(self) -> AsyncIterator[Dict[str, Any]]:
        """接收消息"""
        ...


class StdioTransport(BaseTransport):
    """STDIO 传输层"""
    
    def __init__(
        self,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
    ):
        self.command = command
        self.args = args
        self.env = env or {}
        self.process: Optional[subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self.logger = get_logger()
    
    async def connect(self) -> None:
        """启动进程并连接"""
        # 构建环境
        full_env = {**os.environ, **self.env} if self.env else None
        
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
            )
            self._reader = self.process.stdout
            self._writer = self.process.stdin
            self.logger.info("StdioTransport", f"启动 MCP 服务器: {self.command}")
        except Exception as e:
            raise RuntimeError(f"启动 MCP 服务器失败: {e}")
    
    async def disconnect(self) -> None:
        """断开连接并终止进程"""
        if self.process:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
            self.process = None
        self._reader = None
        self._writer = None
    
    async def send(self, message: Dict[str, Any]) -> None:
        """发送 JSON-RPC 消息"""
        if not self._writer:
            raise RuntimeError("未连接")
        
        content = json.dumps(message) + "\n"
        self._writer.write(content.encode())
        await self._writer.drain()
    
    async def receive(self) -> AsyncIterator[Dict[str, Any]]:
        """接收 JSON-RPC 消息"""
        if not self._reader:
            raise RuntimeError("未连接")
        
        while True:
            try:
                line = await self._reader.readline()
                if not line:
                    break
                yield json.loads(line.decode())
            except Exception as e:
                self.logger.error("StdioTransport", f"接收消息失败: {e}")
                break


import os  # 延迟导入避免循环


class MCPClient:
    """MCP 客户端"""
    
    def __init__(self):
        self.servers: Dict[str, MCPServerConfig] = {}
        self.transports: Dict[str, BaseTransport] = {}
        self.tools: Dict[str, ToolDefinition] = {}  # name -> ToolDefinition
        self.logger = get_logger()
        self._initialized = False
    
    def register_server(self, config: MCPServerConfig) -> None:
        """注册 MCP 服务器"""
        self.servers[config.name] = config
        self.logger.info("MCPClient", f"注册服务器: {config.name}")
    
    async def connect(self, server_name: str) -> None:
        """连接到 MCP 服务器"""
        if server_name not in self.servers:
            raise ValueError(f"服务器未注册: {server_name}")
        
        config = self.servers[server_name]
        
        # 创建传输层
        if config.transport == TransportType.STDIO:
            transport = StdioTransport(
                command=config.command,
                args=config.args,
                env=config.env,
            )
        else:
            raise NotImplementedError(f"不支持的传输类型: {config.transport}")
        
        await transport.connect()
        self.transports[server_name] = transport
        
        # 初始化
        await self._initialize(server_name, transport)
    
    async def _initialize(self, server_name: str, transport: BaseTransport) -> None:
        """初始化连接,获取服务器能力"""
        # 发送 initialize 请求
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "clientInfo": {
                    "name": "skill-agent",
                    "version": "1.0.0",
                },
            },
        }
        
        await transport.send(init_request)
        
        # 接收响应
        async for msg in transport.receive():
            if msg.get("id") == 1:
                if "error" in msg:
                    raise MCPError(
                        msg["error"].get("code", -1),
                        msg["error"].get("message", "初始化失败"),
                    )
                self.logger.info("MCPClient", f"{server_name} 初始化成功")
                break
        
        # 发送 initialized 通知
        await transport.send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        
        self._initialized = True
    
    async def disconnect(self, server_name: str) -> None:
        """断开 MCP 服务器连接"""
        if server_name in self.transports:
            await self.transports[server_name].disconnect()
            del self.transports[server_name]
    
    async def list_tools(self, server_name: str) -> List[ToolDefinition]:
        """列出服务器上的工具"""
        if server_name not in self.transports:
            raise RuntimeError(f"未连接到服务器: {server_name}")
        
        transport = self.transports[server_name]
        
        # 发送 tools/list 请求
        request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        
        await transport.send(request)
        
        # 接收响应
        tools = []
        async for msg in transport.receive():
            if msg.get("id") == 2:
                if "error" in msg:
                    raise MCPError(
                        msg["error"].get("code", -1),
                        msg["error"].get("message", "列出工具失败"),
                    )
                
                # 解析工具列表
                tool_list = msg.get("result", {}).get("tools", [])
                for t in tool_list:
                    tool_def = ToolDefinition(
                        name=t["name"],
                        description=t.get("description", ""),
                        input_schema=t.get("inputSchema", {}),
                    )
                    tools.append(tool_def)
                    self.tools[tool_def.name] = tool_def
                break
        
        return tools
    
    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> ToolResult:
        """调用工具"""
        # 找到工具属于哪个服务器
        server_name = None
        for name, transport in self.transports.items():
            if tool_name in [t.name for t in await self.list_tools(name)]:
                server_name = name
                break
        
        if not server_name:
            raise ValueError(f"工具未找到: {tool_name}")
        
        transport = self.transports[server_name]
        
        # 发送 tools/call 请求
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        
        await transport.send(request)
        
        # 接收响应
        async for msg in transport.receive():
            if msg.get("id") == 3:
                if "error" in msg:
                    return ToolResult(
                        tool=tool_name,
                        result=None,
                        is_error=True,
                        error_message=msg["error"].get("message", "调用失败"),
                    )
                
                result = msg.get("result", {})
                content = result.get("content", [])
                
                # 提取文本内容
                text = ""
                for item in content:
                    if item.get("type") == "text":
                        text += item.get("text", "")
                
                return ToolResult(tool=tool_name, result=text)
    
    def get_all_tools(self) -> List[ToolDefinition]:
        """获取所有已发现的工具"""
        return list(self.tools.values())
    
    def to_openai_functions(self) -> List[Dict[str, Any]]:
        """转换为 OpenAI function calling 格式"""
        return [t.to_openai_format() for t in self.tools.values()]


# ---- 全局单例 ----
_mcp_client: Optional[MCPClient] = None


def get_mcp_client() -> MCPClient:
    """获取 MCP 客户端全局实例"""
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MCPClient()
    return _mcp_client


def reset_mcp_client() -> None:
    """重置 MCP 客户端"""
    global _mcp_client
    _mcp_client = None
