"""网页插件"""
import requests
import trafilatura
from bs4 import BeautifulSoup

from core.plugin import BasePlugin, ToolSchema, ToolResult


class WebPlugin(BasePlugin):
    """网页抓取插件"""
    
    @property
    def name(self) -> str:
        return "web"
    
    @property
    def version(self) -> str:
        return "1.0.0"
    
    @property
    def description(self) -> str:
        return "抓取网页内容，返回标题和正文"
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="fetch_webpage",
            description="抓取网页内容，返回标题和正文",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "网页 URL"
                    }
                },
                "required": ["url"]
            }
        )
    
    def execute(self, params: dict) -> ToolResult:
        url = params.get("url")
        
        if not url:
            return ToolResult(success=False, error="缺少 url 参数")
        
        try:
            # 方式1: trafilatura
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    output_format='markdown',
                    include_tables=True,
                    include_images=False
                )
                soup = BeautifulSoup(downloaded, 'html.parser')
                title = soup.find('title')
                title_text = title.get_text(strip=True) if title else ""
                
                return ToolResult(
                    success=True,
                    data=f"标题: {title_text}\n\n内容:\n{text or downloaded[:5000]}"
                )
            
            # 方式2: requests + BeautifulSoup
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            html = response.text
            text = trafilatura.extract(html, output_format='markdown', include_tables=True)
            
            if not text:
                soup = BeautifulSoup(html, 'html.parser')
                for script in soup(["script", "style"]):
                    script.decompose()
                text = soup.get_text(separator='\n', strip=True)[:5000]
            
            soup = BeautifulSoup(html, 'html.parser')
            title = soup.find('title')
            title_text = title.get_text(strip=True) if title else ""
            
            return ToolResult(
                success=True,
                data=f"标题: {title_text}\n\n内容:\n{text}"
            )
            
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                return ToolResult(success=False, error="403 - 该网站限制爬虫访问")
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=str(e))
