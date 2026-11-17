"""网页抓取工具 —— 把一个 URL 抓成 Markdown。

## 为什么单独一个工具

`web_search` 给的是**链接 + 摘要**。但用户真正要读的往往是**正文**
("这个文档的第三节写了什么")。只有摘要是不够的。

DeepSeek 的 **Fetch API**(`POST /v1/tools/fetch`)正好干这个:
抓单个网页,以 Markdown 返回。

两个配一起才完整:
    web_search(找) → 拿到 url → web_fetch(读) → 拿到正文

## 拿不到 DeepSeek 时怎么办

退到直接 HTTP 抓 + 去标签转文本。质量不如官方(没有正文抽取),
但至少不是"抓不到"。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import httpx

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MAX_CONTENT_CHARS = 40_000


def _deepseek_fetch_config() -> Optional[Dict[str, Any]]:
    """DeepSeek Fetch API 的接入配置;不可用返回 None。"""
    try:
        from infra.config import config

        base = (config.openai_base_url or "").lower()
        key = (config.openai_api_key or "").strip()
        if not key or "deepseek" not in base:
            return None
        origin = base.rstrip("/")
        if origin.endswith("/v1"):
            origin = origin[: -len("/v1")]
        return {
            "url": f"{origin}/v1/tools/fetch",
            "api_key": key,
        }
    except Exception:
        return None


def _html_to_text(raw: str) -> str:
    """把 HTML 粗转成可读文本(兜底路径用)。"""
    import html as H

    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = H.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


class WebFetchTool(Tool):
    """抓取网页正文(转 Markdown)。"""

    name = "web_fetch"
    description = (
        "抓取一个网页的正文,返回 Markdown 文本。"
        "配合 web_search 使用:先搜出链接,再用它读正文。"
        "适合:读文档站、读 API 文档、读新闻全文。"
    )

    def __init__(self, max_chars: int = MAX_CONTENT_CHARS):
        self.logger = get_logger()
        self.max_chars = max_chars
        self._client: Optional[httpx.AsyncClient] = None

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="url", type="string", required=True, description="网页 URL"),
            ],
            returns={
                "title": "页面标题",
                "content": "正文(Markdown)",
                "url": "实际抓取的 URL",
            },
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_UA},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, url: str) -> ToolResult:
        url = (url or "").strip()
        if not url:
            return ToolResult(
                success=False,
                error=(
                    "url 不能为空。"
                    "如果要用上一个工具的搜索结果,请写占位符而不是文字描述,"
                    "例如 `${t1.data.results[0].url}`。"
                ),
            )
        if not url.startswith(("http://", "https://")):
            # **给可恢复的错误** —— 实测里 planner 会把占位符写成
            # "从搜索结果中选择最相关的一个网页链接" 这种文字,
            # 只说"不合法"它不知道该怎么改。
            return ToolResult(
                success=False,
                error=(
                    f"url 必须以 http:// 或 https:// 开头,收到的是: {url[:80]!r}。\n"
                    "如果这里应该引用上一个工具的搜索结果,请改成占位符:"
                    "`${t1.data.results[0].url}`(t1 是搜索任务的 id),"
                    "并在依赖上加 `\"depends_on\": [\"t1\"]`。"
                ),
            )

        ds = _deepseek_fetch_config()
        if ds:
            result = await self._fetch_deepseek(ds, url)
            if result is not None:
                return result
            self.logger.info(self.name, "DeepSeek 抓取失败,退回直连")

        return await self._fetch_direct(url)

    async def _fetch_deepseek(
        self, cfg: Dict[str, Any], url: str
    ) -> Optional[ToolResult]:
        client = await self._get_client()
        try:
            resp = await client.post(
                cfg["url"],
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={"url": url},
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            self.logger.warning(self.name, f"DeepSeek 抓取异常: {exc}")
            return None
        if resp.status_code != 200:
            self.logger.warning(self.name, f"DeepSeek 抓取 HTTP {resp.status_code}")
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        content = data.get("content") or ""
        if not content:
            return None
        return ToolResult(
            success=True,
            data={
                "title": data.get("title") or url,
                "content": content[: self.max_chars],
                "url": url,
                "source": "deepseek_fetch",
            },
        )

    async def _fetch_direct(self, url: str) -> ToolResult:
        client = await self._get_client()
        try:
            resp = await client.get(url)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return ToolResult(success=False, error=f"抓取失败: {exc}")
        if resp.status_code != 200:
            return ToolResult(success=False, error=f"抓取失败: HTTP {resp.status_code}")

        content_type = (resp.headers.get("content-type") or "").lower()
        if "json" in content_type:
            text = resp.text
        elif "html" in content_type or "<" in resp.text[:2000]:
            text = _html_to_text(resp.text)
        else:
            text = resp.text

        if not text.strip():
            return ToolResult(success=False, error="页面是空的")

        return ToolResult(
            success=True,
            data={
                "title": url,
                "content": text[: self.max_chars],
                "url": str(resp.url),
                "source": "direct",
            },
        )
