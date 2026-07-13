"""ToolHub 多源工具管理测试"""
import pytest

from tools.base import Tool, ToolResult, ToolSchema, ToolParam
from tools.hub import ToolHub, ToolInfo, get_tool_hub, reset_tool_hub
from tools.sources.base import SourceType, ToolSource
from tools.sources.python_source import PythonSource, create_python_source


@pytest.fixture(autouse=True)
def clean_hub():
    """每个测试前清理 Hub"""
    reset_tool_hub()
    yield
    reset_tool_hub()


class TestToolHubBasics:
    """ToolHub 基础功能测试"""

    def test_get_tool_hub_returns_singleton(self):
        hub1 = get_tool_hub()
        hub2 = get_tool_hub()
        assert hub1 is hub2

    def test_register_python_tool(self):
        """注册 Python 工具"""
        hub = get_tool_hub()

        class MyTool(Tool):
            name = "my_tool"
            description = "测试工具"
            def schema(self):
                return ToolSchema(name=self.name, description=self.description)
            def execute(self, **kwargs):
                return ToolResult(success=True, data={"echo": "ok"})

        hub.register_python_tool(MyTool())
        assert "my_tool" in hub.names()
        assert hub.get_tool("my_tool").name == "my_tool"

    def test_register_duplicate_raises(self):
        """重复注册工具应抛出异常"""
        hub = get_tool_hub()

        class DuplicateTool(Tool):
            name = "dup_tool"
            description = "dup"
            def schema(self):
                return ToolSchema(name=self.name, description=self.description)
            def execute(self, **kwargs):
                return ToolResult(success=True)

        hub.register_python_tool(DuplicateTool())
        with pytest.raises(ValueError, match="已存在"):
            hub.register_python_tool(DuplicateTool())

    def test_unregister_tool(self):
        """注销工具"""
        hub = get_tool_hub()

        class RemoveTool(Tool):
            name = "remove_tool"
            description = "remove"
            def schema(self):
                return ToolSchema(name=self.name, description=self.description)
            def execute(self, **kwargs):
                return ToolResult(success=True)

        hub.register_python_tool(RemoveTool())
        assert "remove_tool" in hub.names()
        assert hub.unregister_tool("remove_tool")
        assert "remove_tool" not in hub.names()

    def test_list_tools(self):
        """列出所有工具"""
        hub = get_tool_hub()

        class ToolA(Tool):
            name = "tool_a"
            description = "A"
            def schema(self):
                return ToolSchema(name=self.name, description=self.description)
            def execute(self, **kwargs):
                return ToolResult(success=True)

        class ToolB(Tool):
            name = "tool_b"
            description = "B"
            def schema(self):
                return ToolSchema(name=self.name, description=self.description)
            def execute(self, **kwargs):
                return ToolResult(success=True)

        hub.register_python_tool(ToolA())
        hub.register_python_tool(ToolB())

        tools = hub.list_tools()
        assert len(tools) >= 2
        tool_names = [t.name for t in tools]
        assert "tool_a" in tool_names
        assert "tool_b" in tool_names

    def test_schemas(self):
        """获取工具 schema"""
        hub = get_tool_hub()

        class SchemaTool(Tool):
            name = "schema_tool"
            description = "schema test"
            def schema(self):
                return ToolSchema(
                    name=self.name,
                    description=self.description,
                    params=[ToolParam("x", "string", required=True)]
                )
            def execute(self, **kwargs):
                return ToolResult(success=True)

        hub.register_python_tool(SchemaTool())
        schemas = hub.schemas()

        schema = next((s for s in schemas if s.name == "schema_tool"), None)
        assert schema is not None
        assert schema.name == "schema_tool"
        assert len(schema.params) == 1
        assert schema.params[0].name == "x"


class TestToolHubSources:
    """工具源管理测试"""

    def test_register_source(self):
        """注册工具源"""
        hub = get_tool_hub()

        source = ToolSource(
            name="test_source",
            type=SourceType.PYTHON,
            config={"directories": []},
        )
        python_source = PythonSource(source)

        hub.register_source(python_source)
        sources = hub.get_sources()
        assert any(s.name == "test_source" for s in sources)

    def test_get_source_status(self):
        """获取源状态"""
        hub = get_tool_hub()

        source = ToolSource(
            name="status_source",
            type=SourceType.PYTHON,
            enabled=True,
        )
        python_source = PythonSource(source)
        hub.register_source(python_source)

        status = hub.get_source_status()
        assert "status_source" in status
        assert status["status_source"]["enabled"] is True
        assert status["status_source"]["connected"] is False


class TestToolInfo:
    """ToolInfo 数据结构测试"""

    def test_tool_info_creation(self):
        """创建 ToolInfo"""
        schema = ToolSchema(name="info_tool", description="info")
        info = ToolInfo(
            name="info_tool",
            description="info test",
            schema=schema,
            source_name="test",
            source_type=SourceType.PYTHON,
            instance=None,
        )

        assert info.name == "info_tool"
        assert info.source_type == SourceType.PYTHON


class TestPythonSource:
    """Python 工具源测试"""

    def test_create_python_source(self):
        """创建 Python 工具源"""
        source = create_python_source(
            name="custom_python",
            directories=["/path/to/tools"],
        )

        assert source.source.name == "custom_python"
        assert source.source.type == SourceType.PYTHON
        assert source.source.config["directories"] == ["/path/to/tools"]

    def test_python_source_not_connected_initially(self):
        """初始状态未连接"""
        source = create_python_source(name="test")
        assert not source.is_connected()


class TestSourceTypes:
    """源类型枚举测试"""

    def test_source_types(self):
        """验证源类型枚举"""
        assert SourceType.PYTHON.value == "python"
        assert SourceType.MCP.value == "mcp"
        assert SourceType.HTTP.value == "http"
        assert SourceType.PLUGIN.value == "plugin"
