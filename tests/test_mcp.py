"""MCP 工具协议单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tools.mcp_client import (
    ToolDefinition,
    ToolResult,
    MCPServerConfig,
    MCPClient,
    TransportType,
    get_mcp_client,
    reset_mcp_client,
)


class TestToolDefinition:
    """ToolDefinition 测试"""
    
    def test_create(self):
        """测试创建工具定义"""
        tool = ToolDefinition(
            name="test_tool",
            description="测试工具",
            input_schema={"type": "object", "properties": {}},
        )
        
        assert tool.name == "test_tool"
        assert tool.description == "测试工具"
    
    def test_to_openai_format(self):
        """测试转换为 OpenAI 格式"""
        tool = ToolDefinition(
            name="search",
            description="搜索工具",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
            },
        )
        
        fmt = tool.to_openai_format()
        
        assert fmt["type"] == "function"
        assert fmt["function"]["name"] == "search"
        assert fmt["function"]["description"] == "搜索工具"
        assert "query" in fmt["function"]["parameters"]["properties"]


class TestMCPServerConfig:
    """MCPServerConfig 测试"""
    
    def test_create(self):
        """测试创建服务器配置"""
        config = MCPServerConfig(
            name="github",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            transport=TransportType.STDIO,
        )
        
        assert config.name == "github"
        assert config.command == "npx"
        assert len(config.args) == 2


class TestMCPClient:
    """MCPClient 测试"""
    
    def setup_method(self):
        reset_mcp_client()
    
    def teardown_method(self):
        reset_mcp_client()
    
    def test_register_server(self):
        """测试注册服务器"""
        client = get_mcp_client()
        
        config = MCPServerConfig(
            name="test_server",
            command="echo",
            args=["hello"],
        )
        
        client.register_server(config)
        
        assert "test_server" in client.servers
    
    def test_list_tools_empty(self):
        """测试列出空工具"""
        client = get_mcp_client()
        tools = client.get_all_tools()
        assert tools == []
    
    def test_to_openai_functions(self):
        """测试转换为 OpenAI 函数格式"""
        client = get_mcp_client()
        
        tool = ToolDefinition(
            name="test",
            description="测试",
            input_schema={"type": "object"},
        )
        client.tools["test"] = tool
        
        funcs = client.to_openai_functions()
        
        assert len(funcs) == 1
        assert funcs[0]["function"]["name"] == "test"


class TestToolResult:
    """ToolResult 测试"""
    
    def test_success(self):
        """测试成功结果"""
        result = ToolResult(
            tool="test",
            result="success",
        )
        
        assert result.is_error is False
        assert result.result == "success"
    
    def test_error(self):
        """测试错误结果"""
        result = ToolResult(
            tool="test",
            result=None,
            is_error=True,
            error_message="错误信息",
        )
        
        assert result.is_error is True
        assert result.error_message == "错误信息"


class TestIntegration:
    """集成测试"""
    
    def setup_method(self):
        reset_mcp_client()
    
    def teardown_method(self):
        reset_mcp_client()
    
    def test_singleton(self):
        """测试单例"""
        client1 = get_mcp_client()
        client2 = get_mcp_client()
        assert client1 is client2
    
    def test_multiple_servers(self):
        """测试多服务器注册"""
        client = get_mcp_client()
        
        client.register_server(MCPServerConfig(
            name="server1",
            command="echo",
            args=["1"],
        ))
        
        client.register_server(MCPServerConfig(
            name="server2",
            command="echo",
            args=["2"],
        ))
        
        assert len(client.servers) == 2
