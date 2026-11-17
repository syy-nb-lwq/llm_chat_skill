"""联网搜索工具。

## 后端顺序

1. **DeepSeek Search API**(主力)—— `POST /v1/tools/search`
   DeepSeek 自研搜索引擎,和模型同一个 key。返回结构化结果(title/url/content),
   中文查询质量明显好于爬公共搜索引擎。
2. **公开搜索引擎 HTML**(兜底)—— 没配 DeepSeek key、或者 DeepSeek 不可用时用。

## 踩过的坑(实机)

初版只有两个后端:SearXNG 公共实例 + Wikipedia。
在这台机器上**两个全挂**:

- SearXNG 公共实例:大多连不上;能连的(searx.be)**关掉了 JSON 输出**,
  返回 HTML;searx.party / sapti.me 直接 429
- Wikipedia:403,明确说"请遵守我们的机器人政策"
  —— 即使用浏览器 UA 也一样,它按 IP 拦

结果:agent 想搜东西,拿到的是 "all search backends failed",然后就放弃了。

**教训**:公共实例的可用性是**会变的**,不能写死在代码里当唯一出路。
所以:多来源兜底 + 后端列表可配置 + 每个后端失败都记日志(能看出是谁挂了)。

## 另外

DeepSeek 还有 **Fetch API**(`POST /v1/tools/fetch`),抓网页转 Markdown ——
见 `tools/web_fetch.py`。搜索给链接,抓取给正文,两个配一起才能"读网页"。
"""
from __future__ import annotations

import html
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema


#: 默认 UA —— 很多站点对空 UA / python-requests UA 直接拒
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: 后端名 → 配置。`kind` 决定用哪个解析器。
DEFAULT_BACKENDS: List[Dict[str, Any]] = [
    {"name": "duckduckgo", "kind": "html", "url": "https://html.duckduckgo.com/html/",
     "params": {"q": "{query}"}},
    {"name": "mojeek", "kind": "html", "url": "https://www.mojeek.com/search",
     "params": {"q": "{query}"}},
    {"name": "bing", "kind": "html", "url": "https://www.bing.com/search",
     "params": {"q": "{query}"}},
    {"name": "lite_ddg", "kind": "html", "url": "https://lite.duckduckgo.com/lite/",
     "params": {"q": "{query}"}},
    {"name": "wikipedia", "kind": "wikipedia",
     "url": "https://en.wikipedia.org/w/api.php",
     "params": {"action": "opensearch", "search": "{query}", "limit": "3", "format": "json"}},
]


def deepseek_search_config() -> Optional[Dict[str, Any]]:
    """DeepSeek Search API 的接入配置;不可用时返回 None。

    **只在 base_url 指向 deepseek 时才用** —— 否则拿 OpenAI 的 key 去调
    api.deepseek.com 会 401,纯属浪费一次往返。
    """
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
            "name": "deepseek_search",
            "kind": "deepseek_search",
            "url": f"{origin}/v1/tools/search",
            "api_key": key,
        }
    except Exception:
        return None


def _strip_tags(raw: str) -> str:
    """把一段 HTML 去标签、还原实体、压空白。"""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class SearchTool(Tool):
    """Search the public web for factual lookup."""

    name = "web_search"
    description = (
        "联网搜索公开信息,返回标题+摘要+链接。"
        "适合查事实、找资料、看新闻;不适合查需要登录或私有的内容。"
    )
    params = [
        ToolParam(name="query", type="string", required=True, description="搜索词"),
        ToolParam(
            name="max_results",
            type="integer",
            required=False,
            default=None,
            description="返回条数(默认 5)",
        ),
    ]

    def __init__(self, backends: Optional[List[Dict[str, Any]]] = None):
        self.logger = get_logger()
        self.backends = backends if backends is not None else self._default_backends()
        self._client: Optional[httpx.AsyncClient] = None

    @staticmethod
    def _default_backends() -> List[Dict[str, Any]]:
        """默认后端列表:DeepSeek 搜索优先,公开搜索引擎兜底。"""
        backends: List[Dict[str, Any]] = []
        ds = deepseek_search_config()
        if ds:
            backends.append(ds)
        backends.extend(DEFAULT_BACKENDS)
        return backends

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=self.params,
            returns={"results": "string", "source": "命中的后端"},
            examples=[{"query": "Python 教程"}],
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=True,
                headers={"User-Agent": DEFAULT_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, query: str, max_results: int = 5) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult(success=False, error="query 不能为空")
        try:
            max_results = max(1, min(int(max_results or 5), 10))
        except Exception:
            max_results = 5

        client = await self._get_client()
        errors: List[str] = []

        for backend in self.backends:
            name = backend.get("name") or backend.get("kind") or "?"
            try:
                result = await self._call_backend(client, backend, query, max_results)
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                self.logger.warning(self.name, f"{name} 异常: {exc}")
                continue
            if result:
                result.meta = {"source": name, "backends_tried": len(self.backends)}
                return result
            errors.append(f"{name}: 无结果")
            self.logger.info(self.name, f"{name} 无结果,换下一个")

        return ToolResult(
            success=False,
            error=(
                "所有搜索后端都没返回结果("
                + "; ".join(errors)
                + ")。可以换个说法再试,或用 MCP 接入专门的搜索服务。"
            ),
            meta={"backends": errors},
        )

    # ===== 各后端 =====

    async def _call_backend(
        self,
        client: httpx.AsyncClient,
        backend: Dict[str, Any],
        query: str,
        max_results: int,
    ) -> Optional[ToolResult]:
        kind = backend.get("kind", "html")
        if kind == "deepseek_search":
            return await self._deepseek_search(client, backend, query, max_results)
        if kind == "wikipedia":
            return await self._wikipedia(client, backend, query)
        if kind == "searxng":
            return await self._searxng(client, backend, query, max_results)
        return await self._html(client, backend, query, max_results)

    async def _deepseek_search(
        self,
        client: httpx.AsyncClient,
        backend: Dict[str, Any],
        query: str,
        max_results: int,
    ) -> Optional[ToolResult]:
        """DeepSeek Search API(主力后端)。

        `POST {base}/v1/tools/search`,鉴权用模型那个 key。
        返回 `{"results": [{"title","url","content",...}]}`。
        """
        api_key = backend.get("api_key") or ""
        if not api_key:
            return None
        try:
            resp = await client.post(
                backend["url"],
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "count": max_results,
                    "need_content": True,
                },
            )
        except (httpx.TimeoutException, httpx.ConnectError):
            return None
        if resp.status_code != 200:
            self.logger.warning(self.name, f"DeepSeek 搜索 HTTP {resp.status_code}")
            return None
        try:
            data = resp.json()
        except Exception:
            return None

        items = []
        for r in (data.get("results") or [])[:max_results]:
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            content = (r.get("content") or "").strip()
            if not (title and url):
                continue
            items.append({"title": title, "url": url, "content": content})
        if not items:
            return None
        return ToolResult(success=True, data={"text": _render(items), "results": items})

    async def _html(
        self,
        client: httpx.AsyncClient,
        backend: Dict[str, Any],
        query: str,
        max_results: int,
    ) -> Optional[ToolResult]:
        """通用 HTML 搜索解析(不同引擎的结果结构不一样,各自一个解析器)。"""
        name = backend.get("name", "")
        params = {
            k: str(v).replace("{query}", query) for k, v in (backend.get("params") or {}).items()
        }
        resp = await client.get(backend["url"], params=params)
        if resp.status_code != 200:
            return None
        body = resp.text
        items = _PARSERS.get(name, _parse_generic_html)(body, max_results)
        if not items:
            return None
        return ToolResult(success=True, data={"text": _render(items), "results": items})

    async def _searxng(
        self,
        client: httpx.AsyncClient,
        backend: Dict[str, Any],
        query: str,
        max_results: int,
    ) -> Optional[ToolResult]:
        """SearXNG 实例(返回 JSON)。"""
        params = {
            k: str(v).replace("{query}", query) for k, v in (backend.get("params") or {}).items()
        }
        resp = await client.get(backend["url"], params=params)
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        items = []
        for r in (data.get("results") or [])[:max_results]:
            title = (r.get("title") or "").strip()
            url = (r.get("url") or "").strip()
            content = (r.get("content") or "").strip()
            if title and url:
                items.append({"title": title, "url": url, "content": content})
        if not items:
            return None
        return ToolResult(success=True, data={"text": _render(items), "results": items})

    async def _wikipedia(
        self,
        client: httpx.AsyncClient,
        backend: Dict[str, Any],
        query: str,
    ) -> Optional[ToolResult]:
        """Wikipedia opensearch。

        注意:它按 IP 拦机器人,很多出口 IP 会被 403 ——
        所以它只是**兜底**,不是主力。
        """
        params = {
            k: str(v).replace("{query}", query) for k, v in (backend.get("params") or {}).items()
        }
        resp = await client.get(backend["url"], params=params)
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        titles = data[1] if len(data) > 1 else []
        if not titles:
            return None
        title = titles[0]
        try:
            summary = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
            )
            if summary.status_code != 200:
                return None
            extract = (summary.json() or {}).get("extract", "")
        except Exception:
            return None
        if not extract:
            return None
        items = [{
            "title": title,
            "url": f"https://en.wikipedia.org/wiki/{title}",
            "content": extract,
        }]
        return ToolResult(success=True, data={"text": _render(items), "results": items})


def _render(items: List[Dict[str, str]]) -> str:
    parts = []
    for i, it in enumerate(items, 1):
        parts.append(f"[{i}] {it['title']}\n{it.get('content', '')}\n{it['url']}")
    return "\n\n".join(parts)


# ===== 各引擎的 HTML 解析器 =====

def _parse_duckduckgo(body: str, limit: int) -> List[Dict[str, str]]:
    """DuckDuckGo html 版:结果在 `result__a` / `result__snippet`。"""
    items = []
    for m in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        body, re.S,
    ):
        url, title_html = m.group(1), m.group(2)
        title = _strip_tags(title_html)
        if not title:
            continue
        snippet = ""
        tail = body[m.end(): m.end() + 1200]
        sm = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', tail, re.S)
        if sm:
            snippet = _strip_tags(sm.group(1))
        items.append({"title": title, "url": _ddg_unwrap(url), "content": snippet})
        if len(items) >= limit:
            break
    return items


def _ddg_unwrap(url: str) -> str:
    """DDG 的链接是跳转包装,取真实地址。"""
    m = re.search(r"[?&]uddg=([^&]+)", url)
    if m:
        from urllib.parse import unquote

        return unquote(m.group(1))
    return url


def _parse_mojeek(body: str, limit: int) -> List[Dict[str, str]]:
    items = []
    for m in re.finditer(
        r'<a[^>]+class="[^"]*ob[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        body, re.S,
    ):
        url, title_html = m.group(1), m.group(2)
        title = _strip_tags(title_html)
        if not title or url.startswith("#"):
            continue
        tail = body[m.end(): m.end() + 1500]
        sm = re.search(r'class="s"[^>]*>(.*?)</p>', tail, re.S)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        items.append({"title": title, "url": url, "content": snippet})
        if len(items) >= limit:
            break
    return items


def _parse_bing(body: str, limit: int) -> List[Dict[str, str]]:
    items = []
    for m in re.finditer(
        r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        body, re.S,
    ):
        url, title_html = m.group(1), m.group(2)
        title = _strip_tags(title_html)
        if not title:
            continue
        tail = body[m.end(): m.end() + 2000]
        sm = re.search(r'<p[^>]*>(.*?)</p>', tail, re.S)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        items.append({"title": title, "url": url, "content": snippet})
        if len(items) >= limit:
            break
    return items


def _parse_lite_ddg(body: str, limit: int) -> List[Dict[str, str]]:
    """lite.duckduckgo.com:表格结构,链接在 `result-link`。"""
    items = []
    for m in re.finditer(
        r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        body, re.S,
    ):
        url, title_html = m.group(1), m.group(2)
        title = _strip_tags(title_html)
        if not title:
            continue
        tail = body[m.end(): m.end() + 1000]
        sm = re.search(r'class="result-snippet"[^>]*>(.*?)</td>', tail, re.S)
        snippet = _strip_tags(sm.group(1)) if sm else ""
        items.append({"title": title, "url": _ddg_unwrap(url), "content": snippet})
        if len(items) >= limit:
            break
    return items


def _parse_generic_html(body: str, limit: int) -> List[Dict[str, str]]:
    """兜底解析:找所有指向外部站点的链接,取链接文字当标题。

    质量不如专用解析器,但总比"没有结果"强 —— 至少用户看到一堆可点的链接。
    """
    items = []
    seen = set()
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', body, re.S):
        url = m.group(1)
        title = _strip_tags(m.group(2))
        if not title or len(title) < 8:
            continue
        if url in seen:
            continue
        seen.add(url)
        items.append({"title": title[:120], "url": url, "content": ""})
        if len(items) >= limit:
            break
    return items


_PARSERS = {
    "duckduckgo": _parse_duckduckgo,
    "mojeek": _parse_mojeek,
    "bing": _parse_bing,
    "lite_ddg": _parse_lite_ddg,
}
