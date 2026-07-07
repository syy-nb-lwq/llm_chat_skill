"""搜索工具 - 多个搜索源"""
import requests
from tools.base import Tool, ToolResult
from infra.logger import get_logger, LogType


class SearchTool(Tool):
    """搜索工具 - 支持多个搜索源"""
    
    name = "web_search"
    description = "搜索互联网获取相关信息"
    
    def __init__(self):
        self.logger = get_logger()
        
        # 搜索源按优先级排序
        self.backends = [
            ("jina", self._search_jina),
            ("bing", self._search_bing),
            ("serpapi", self._search_serpapi),
            ("duckduckgo", self._search_duckduckgo),
        ]
    
    def execute(self, query: str, max_results: int = 5) -> ToolResult:
        """执行搜索"""
        self.logger.log_data(self.name, "in", "query", query)
        
        for backend_name, search_func in self.backends:
            self.logger.info(LogType.TOOL_CALL, self.name, "尝试: " + backend_name, {"query": query})
            try:
                result = search_func(query, max_results)
                if result.success:
                    self.logger.info(LogType.TOOL_SUCCESS, self.name, "成功: " + backend_name)
                    self.logger.log_data(self.name, "out", "results", result.data)
                    return result
                else:
                    self.logger.warning(LogType.TOOL_ERROR, self.name, "失败: " + backend_name + " - " + result.error)
            except Exception as e:
                self.logger.error(LogType.TOOL_ERROR, self.name, "异常: " + backend_name + " - " + str(e))
                continue
        
        return ToolResult(success=False, error="所有搜索服务均不可用")
    
    def _search_jina(self, query: str, max_results: int) -> ToolResult:
        """使用 Jina AI 搜索 (国内可访问)"""
        try:
            # Jina Reader API
            url = "https://s.jina.ai/" + query
            headers = {
                "Accept": "application/json"
            }
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                results = {
                    "source": "jina",
                    "query": query,
                    "results": []
                }
                
                # 解析 Jina 返回的结果
                for item in data.get("results", [])[:max_results]:
                    snippet = item.get("description", item.get("content", ""))
                    if len(snippet) > 200:
                        snippet = snippet[:200]
                    results["results"].append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": snippet
                    })
                
                if results["results"]:
                    return ToolResult(success=True, data=results)
            
            raise Exception("Status: " + str(response.status_code))
            
        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="Jina 搜索超时")
        except Exception as e:
            return ToolResult(success=False, error="Jina 搜索失败: " + str(e))
    
    def _search_bing(self, query: str, max_results: int) -> ToolResult:
        """使用 Bing 搜索 (通过代理服务)"""
        try:
            url = "https://ddg-api.herokuapp.com/search"
            params = {"query": query, "limit": max_results}
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                results = {
                    "source": "bing",
                    "query": query,
                    "results": []
                }
                
                for item in data[:max_results]:
                    snippet = item.get("description", "")[:200]
                    results["results"].append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "snippet": snippet
                    })
                
                if results["results"]:
                    return ToolResult(success=True, data=results)
            
            raise Exception("Status: " + str(response.status_code))
            
        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="Bing 搜索超时")
        except Exception as e:
            return ToolResult(success=False, error="Bing 搜索失败: " + str(e))
    
    def _search_serpapi(self, query: str, max_results: int) -> ToolResult:
        """使用 SerpAPI (需要 API Key)"""
        try:
            import os
            api_key = os.getenv("SERPAPI_KEY")
            
            if not api_key:
                return ToolResult(success=False, error="需要配置 SERPAPI_KEY 环境变量")
            
            url = "https://serpapi.com/search"
            params = {
                "q": query,
                "api_key": api_key,
                "num": max_results
            }
            
            response = requests.get(url, params=params, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                results = {
                    "source": "serpapi",
                    "query": query,
                    "results": []
                }
                
                for item in data.get("organic_results", [])[:max_results]:
                    snippet = item.get("snippet", "")[:200]
                    results["results"].append({
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "snippet": snippet
                    })
                
                if results["results"]:
                    return ToolResult(success=True, data=results)
            
            raise Exception("Status: " + str(response.status_code))
            
        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="SerpAPI 搜索超时")
        except Exception as e:
            return ToolResult(success=False, error="SerpAPI 搜索失败: " + str(e))
    
    def _search_duckduckgo(self, query: str, max_results: int) -> ToolResult:
        """使用 DuckDuckGo HTML"""
        try:
            url = "https://html.duckduckgo.com/html/"
            data = {"q": query}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            response = requests.post(url, data=data, headers=headers, timeout=10)
            
            if response.status_code == 200:
                import re
                results = {
                    "source": "duckduckgo",
                    "query": query,
                    "results": []
                }
                
                # 简单解析 HTML
                pattern = r'<a class="result__a"[^>]*href="([^"]*)"[^>]*>([^<]*)</a>'
                matches = re.findall(pattern, response.text)
                
                for url, title in matches[:max_results]:
                    results["results"].append({
                        "title": title.strip(),
                        "url": url,
                        "snippet": ""
                    })
                
                if results["results"]:
                    return ToolResult(success=True, data=results)
            
            raise Exception("Status: " + str(response.status_code))
            
        except requests.exceptions.Timeout:
            return ToolResult(success=False, error="DuckDuckGo 搜索超时")
        except Exception as e:
            return ToolResult(success=False, error="DuckDuckGo 搜索失败: " + str(e))
