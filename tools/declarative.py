"""声明式 HTTP 工具 —— 按提案里的 spec 发请求，**不执行任何生成的代码**。

这是 agent 自建工具的首选形态：它只是数据（URL 模板 + 参数表 + 响应路径），
执行器是固定的。所以：

- **可审查**：看一眼 spec 就知道它会请求什么
- **可自动发布**：没有代码执行面，只读请求不会改外部状态
- **不会误伤**：参数会被 URL 编码，不存在拼接注入

## spec 长什么样

```json
{
  "name": "crypto_price",
  "description": "查某个币种的实时价格",
  "kind": "http",
  "method": "GET",
  "url_template": "https://api.example.com/price?symbol={symbol}",
  "params": [{"name": "symbol", "type": "string", "required": true}],
  "response_path": "data.price"
}
```

调用 `crypto_price(symbol="BTC")` 会请求
`https://api.example.com/price?symbol=BTC`，然后从 JSON 响应里取 `data.price`。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import httpx

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema
from core.exception_policy import downgrade


#: 模板占位符
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: 单次响应最多回给 LLM 多少字符 —— 接口可能返回巨大 JSON
MAX_RESPONSE_CHARS = 20_000


def extract_by_path(payload: Any, path: str) -> Any:
    """按 `data.items` / `list[0].v` 这种路径取值。

    取不到返回 None（调用方据此决定是否回退到整个响应）。
    """
    if not path:
        return payload
    cur = payload
    for raw in path.split("."):
        if cur is None:
            return None
        part = raw.strip()
        if not part:
            continue
        m = re.match(r"^([^\[\]]*)((?:\[\d+\])*)$", part)
        if not m:
            return None
        key, idxs = m.group(1), m.group(2)
        if key:
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(key)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        for raw_idx in re.findall(r"\[(\d+)\]", idxs or ""):
            if not isinstance(cur, (list, tuple)):
                return None
            try:
                idx = int(raw_idx)
            except ValueError:
                return None
            if idx >= len(cur):
                return None
            cur = cur[idx]
    return cur


class DeclarativeHttpTool(Tool):
    """由一个提案 spec 驱动的 HTTP 工具。"""

    def __init__(self, proposal):
        self.proposal = proposal
        self.name = proposal.name
        self.description = proposal.description
        self.logger = get_logger()
        self._client: Optional[httpx.AsyncClient] = None

    # ===== schema =====

    def schema(self) -> ToolSchema:
        params: List[ToolParam] = []
        for p in self.proposal.params or []:
            if not isinstance(p, dict):
                continue
            params.append(
                ToolParam(
                    name=str(p.get("name") or ""),
                    type=str(p.get("type") or "string"),
                    description=str(p.get("description") or ""),
                    required=bool(p.get("required", True)),
                    default=p.get("default"),
                    enum=p.get("enum"),
                )
            )
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=params,
            returns={"result": "接口返回（可能是裁剪后的 JSON 或文本）"},
        )

    # ===== 执行 =====

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def execute(self, **kwargs) -> ToolResult:
        spec = self.proposal
        try:
            ok, err = self.validate_params(kwargs)
            if not ok:
                return ToolResult(success=False, error=err)
        except Exception as exc:
            downgrade(exc, where='tools/declarative.py')
        # 缺省值补齐
        values: Dict[str, Any] = {}
        for p in spec.params or []:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "")
            if not pname:
                continue
            if pname in kwargs and kwargs[pname] is not None:
                values[pname] = kwargs[pname]
            elif p.get("default") is not None:
                values[pname] = p.get("default")

        # 构造 URL / 参数（**一律 URL 编码，不做字符串拼接**）
        url, query, path_params = self._build_request(spec, values)
        if url is None:
            return ToolResult(success=False, error=path_params or "URL 构造失败")

        headers = {str(k): str(v) for k, v in (spec.headers or {}).items()}
        body = None
        if spec.method in ("POST", "PUT", "PATCH"):
            body = {k: v for k, v in values.items() if k not in set(_PLACEHOLDER_RE.findall(spec.url_template or ""))}

        client = await self._get_client()
        try:
            resp = await client.request(
                spec.method,
                url,
                params=query or None,
                headers=headers or None,
                json=body,
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return ToolResult(success=False, error=f"请求失败：{type(exc).__name__}: {exc}")
        except Exception as exc:
            return ToolResult(success=False, error=f"请求异常：{exc}")

        if resp.status_code >= 400:
            return ToolResult(
                success=False,
                error=f"接口返回 HTTP {resp.status_code}：{resp.text[:300]}",
                meta={"status_code": resp.status_code, "url": url},
            )

        # 解析响应
        payload: Any
        content_type = (resp.headers.get("content-type") or "").lower()
        if "json" in content_type:
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
        else:
            payload = resp.text

        # 按 response_path 裁剪
        picked = extract_by_path(payload, spec.response_path or "")
        if picked is not None:
            payload = picked
        elif spec.response_path:
            # 路径没命中 —— 把整个响应当兜底，并说明
            payload = {
                "warning": f"response_path {spec.response_path!r} 没取到，以下是完整响应",
                "raw": payload,
            }

        return ToolResult(
            success=True,
            data=self._shape(payload),
            meta={"status_code": resp.status_code, "url": url, "tool": self.name},
        )

    # ===== 内部 =====

    def _build_request(self, spec, values: Dict[str, Any]):
        """返回 (url, query_dict, error)。"""
        template = spec.url_template or ""

        # 1) 路径/查询里的 {param}
        def repl(m):
            key = m.group(1)
            if key in values:
                return str(values[key])
            return m.group(0)

        url = _PLACEHOLDER_RE.sub(repl, template)
        missing = [
            m.group(1) for m in _PLACEHOLDER_RE.finditer(url)
        ]
        if missing:
            return None, None, f"缺少参数：{sorted(set(missing))}"

        # 2) 没在模板里出现的参数 → 走 query(GET) 或 body(写方法)
        used = set(_PLACEHOLDER_RE.findall(template))
        leftovers = {
            k: v for k, v in values.items()
            if k not in used and not isinstance(v, (dict, list))
        }
        query: Dict[str, Any] = {}
        if spec.method in ("GET", "HEAD", "DELETE"):
            query = leftovers
        return url, query, None

    @staticmethod
    def _shape(payload: Any) -> Any:
        """把响应整理成"LLM 好读、体积可控"的形式。"""
        if isinstance(payload, str):
            return {"text": payload[:MAX_RESPONSE_CHARS]}
        try:
            dumped = json.dumps(payload, ensure_ascii=False)
        except Exception:
            return {"text": str(payload)[:MAX_RESPONSE_CHARS]}
        if len(dumped) <= MAX_RESPONSE_CHARS:
            return payload
        return {
            "truncated": True,
            "note": f"响应超过 {MAX_RESPONSE_CHARS} 字符，已截断；可用 response_path 精确取值",
            "preview": dumped[:MAX_RESPONSE_CHARS],
        }
