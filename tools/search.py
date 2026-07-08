"""Web Search Tool"""
import requests
from tools.base import Tool, ToolResult, ToolSchema, ToolParam
from infra.logger import get_logger


class SearchTool(Tool):
    """网络搜索工具"""

    name = "web_search"
    description = "搜索网络信息"

    params = [
        ToolParam(name="query", type="string", required=True, description="搜索关键词"),
    ]

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=self.params,
            returns={"results": "string"},
            examples=[{"query": "Python 教程"}],
        )

    def __init__(self):
        self.logger = get_logger()
        self.backends = [
            ("DuckDuckGo", self._ddg),
            ("Wikipedia", self._wiki),
            ("Bing", self._bing),
        ]

    async def execute(self, query: str) -> ToolResult:
        for backend_name, search_func in self.backends:
            self.logger.info(self.name, f"尝试: {backend_name}")
            try:
                result = search_func(query)
                if result.success:
                    self.logger.info(self.name, f"成功: {backend_name}")
                    result.meta = {"source": backend_name}
                    return result
            except Exception as e:
                self.logger.warning(self.name, f"失败: {backend_name} - {e}")
                continue
        return ToolResult(success=False, error="所有搜索引擎均失败")

    def _ddg(self, query: str) -> ToolResult:
        try:
            url = "https://api.duckduckgo.com/"
            params = {"q": query, "format": "json", "no_html": 1}
            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()
            if data.get("AbstractText"):
                return ToolResult(success=True, data={"text": data["AbstractText"], "source": "DuckDuckGo"})
            if data.get("RelatedTopics"):
                topics = [t.get("Text", "") for t in data["RelatedTopics"][:5] if t.get("Text")]
                return ToolResult(success=True, data={"text": "\n".join(topics), "source": "DuckDuckGo"})
            return ToolResult(success=False, error="无结果")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _wiki(self, query: str) -> ToolResult:
        try:
            url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{query}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return ToolResult(success=True, data={"text": data.get("extract", "无摘要"), "source": "Wikipedia"})
            return ToolResult(success=False, error=f"状态码: {resp.status_code}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def _bing(self, query: str) -> ToolResult:
        return ToolResult(success=False, error="Bing API 需要密钥")
