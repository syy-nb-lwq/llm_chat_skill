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
        # 搜索后端优先级
        self.backends = [
            ("SearXNG", self._searxng),
            ("Wikipedia", self._wiki),
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
                else:
                    self.logger.warning(self.name, f"失败: {backend_name} - {result.error}")
            except Exception as e:
                self.logger.warning(self.name, f"异常: {backend_name} - {e}")
                continue
        return ToolResult(success=False, error="所有搜索引擎均失败")

    def _searxng(self, query: str) -> ToolResult:
        """使用 SearXNG 公共实例"""
        instances = [
            "https://searx.party",
            "https://searx.be",
            "https://searx.tiekoetter.com",
            "https://search.sapti.me",
            "https://searx.ninja",
            "https://searx.work",
            "https://search.kmlmgjtb.cn",
            "https://searx.publicvm.com",
        ]
        
        for instance in instances:
            try:
                url = f"{instance.rstrip('/')}/search"
                params = {
                    "q": query,
                    "format": "json",
                    "language": "zh-CN",
                    "safesearch": 0,
                }
                resp = requests.get(url, params=params, timeout=8)
                
                if resp.status_code not in (200, 202):
                    self.logger.warning(self.name, f"SearXNG {instance} 状态码: {resp.status_code}")
                    continue
                
                data = resp.json()
                results = data.get("results", [])
                
                if results:
                    items = []
                    for r in results[:5]:
                        title = r.get("title", "")
                        url_val = r.get("url", "")
                        content = r.get("content", "")
                        if title and url_val:
                            items.append(f"**{title}**\n{content}\n{url_val}")
                    
                    if items:
                        return ToolResult(
                            success=True, 
                            data={"text": "\n\n".join(items), "source": "SearXNG"}
                        )
                        
            except requests.exceptions.Timeout:
                continue
            except requests.exceptions.ConnectionError:
                continue
            except Exception as e:
                continue
        
        return ToolResult(success=False, error="所有 SearXNG 实例均失败")

    def _wiki(self, query: str) -> ToolResult:
        try:
            search_url = "https://en.wikipedia.org/w/api.php"
            search_params = {
                "action": "opensearch",
                "search": query,
                "limit": 1,
                "format": "json",
            }
            search_resp = requests.get(search_url, params=search_params, timeout=10)
            
            if search_resp.status_code not in (200, 202):
                return ToolResult(success=False, error=f"Wikipedia HTTP {search_resp.status_code}")
            
            search_data = search_resp.json()
            titles = search_data.get("1", [])
            
            if not titles:
                return ToolResult(success=False, error="Wikipedia 无结果")
            
            title = titles[0]
            summary_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
            summary_resp = requests.get(summary_url, timeout=10)
            
            if summary_resp.status_code == 200:
                data = summary_resp.json()
                extract = data.get("extract", "")
                return ToolResult(success=True, data={"text": extract, "source": "Wikipedia"})
            
            return ToolResult(success=False, error=f"Wikipedia 摘要获取失败: {summary_resp.status_code}")
            
        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="Wikipedia 超时")
        except requests.exceptions.ConnectionError:
            return ToolResult(success=False, error="Wikipedia 连接失败")
        except Exception as e:
            return ToolResult(success=False, error=f"Wikipedia 异常: {e}")
