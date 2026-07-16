"""Asynchronous web search tool."""
from typing import Optional

import httpx

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


class SearchTool(Tool):
    """Search public engines for lightweight factual lookup."""

    name = "web_search"
    description = "Search public web information"
    params = [
        ToolParam(name="query", type="string", required=True, description="Search query"),
    ]

    def __init__(self):
        self.logger = get_logger()
        self.backends = [
            ("SearXNG", self._searxng),
            ("Wikipedia", self._wiki),
        ]
        self._client: Optional[httpx.AsyncClient] = None

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=self.params,
            returns={"results": "string"},
            examples=[{"query": "Python tutorial"}],
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=8.0),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, query: str) -> ToolResult:
        for backend_name, search_func in self.backends:
            self.logger.info(self.name, f"trying backend {backend_name}")
            try:
                result = await search_func(query)
            except Exception as exc:
                self.logger.warning(self.name, f"{backend_name} exception: {exc}")
                continue
            if result.success:
                result.meta = {"source": backend_name}
                return result
            self.logger.warning(self.name, f"{backend_name} failed: {result.error}")
        return ToolResult(success=False, error="all search backends failed")

    async def _searxng(self, query: str) -> ToolResult:
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
        client = await self._get_client()

        for instance in instances:
            url = f"{instance.rstrip('/')}/search"
            try:
                resp = await client.get(
                    url,
                    params={
                        "q": query,
                        "format": "json",
                        "language": "zh-CN",
                        "safesearch": 0,
                    },
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                continue
            except Exception:
                continue

            if resp.status_code not in (200, 202):
                continue

            try:
                data = resp.json()
            except Exception:
                continue

            results = data.get("results", [])
            if not results:
                continue

            items = []
            for result in results[:5]:
                title = result.get("title", "")
                url_value = result.get("url", "")
                content = result.get("content", "")
                if title and url_value:
                    items.append(f"**{title}**\n{content}\n{url_value}")

            if items:
                return ToolResult(
                    success=True,
                    data={"text": "\n\n".join(items), "source": "SearXNG"},
                )

        return ToolResult(success=False, error="all SearXNG instances failed")

    async def _wiki(self, query: str) -> ToolResult:
        client = await self._get_client()
        try:
            search_resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": 1,
                    "format": "json",
                },
            )
        except httpx.TimeoutException:
            return ToolResult(success=False, error="Wikipedia timeout")
        except httpx.ConnectError:
            return ToolResult(success=False, error="Wikipedia connection failed")
        except Exception as exc:
            return ToolResult(success=False, error=f"Wikipedia error: {exc}")

        if search_resp.status_code not in (200, 202):
            return ToolResult(success=False, error=f"Wikipedia HTTP {search_resp.status_code}")

        try:
            search_data = search_resp.json()
        except Exception as exc:
            return ToolResult(success=False, error=f"Wikipedia JSON error: {exc}")

        titles = search_data[1] if len(search_data) > 1 else []
        if not titles:
            return ToolResult(success=False, error="Wikipedia no result")

        title = titles[0]
        try:
            summary_resp = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
            )
        except httpx.TimeoutException:
            return ToolResult(success=False, error="Wikipedia summary timeout")
        except httpx.ConnectError:
            return ToolResult(success=False, error="Wikipedia summary connection failed")
        except Exception as exc:
            return ToolResult(success=False, error=f"Wikipedia summary error: {exc}")

        if summary_resp.status_code != 200:
            return ToolResult(
                success=False,
                error=f"Wikipedia summary HTTP {summary_resp.status_code}",
            )

        try:
            data = summary_resp.json()
        except Exception as exc:
            return ToolResult(success=False, error=f"Wikipedia summary JSON error: {exc}")

        return ToolResult(
            success=True,
            data={"text": data.get("extract", ""), "source": "Wikipedia"},
        )
