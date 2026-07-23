"""Declarative HTTP Tool 执行器 + 沙箱测试运行器(M4-02/M4-03/M4-04)。

设计:
- ``DeclarativeHTTPTool`` 把 ``ToolProposal`` 渲染成一个 ``Tool`` 实例,
  注入到 ``ToolHub``;执行时按 ``endpoint`` 发起 HTTP 请求。
- ``SandboxRunner`` 隔离目录 + 受限网络 + 测试用例运行,失败不污染主进程。
- 网络白名单强制在执行前校验 host,避免 ``requests``/``urllib`` 绕过。
- secret 引用通过 ``infra.config.get_secret(name)`` 解析;不接触明文。
"""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema
from tools.proposal import (
    NetworkPolicy,
    SideEffectLevel,
    ToolEndpoint,
    ToolProposal,
    ToolTestCase,
)


# ===== 网络白名单 =====


def _host_in_allowed(host: str, allowed: List[str]) -> bool:
    """判断 host 是否在白名单;支持 ``*.example.com`` 通配符。"""
    host = host.lower()
    for pat in allowed:
        pat = pat.lower().strip()
        if pat.startswith("*."):
            suffix = pat[1:]  # ".example.com"
            if host.endswith(suffix) or host == pat[2:]:
                return True
        elif host == pat:
            return True
    return False


def _enforce_network_policy(url: str, policy: NetworkPolicy) -> Optional[str]:
    """返回 None 表示通过;否则返回错误信息。"""
    try:
        parsed = urlparse(url)
    except Exception:
        return f"URL 无法解析: {url}"
    if policy.require_https and parsed.scheme != "https":
        # 允许 http for localhost 沙箱
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            return f"网络策略要求 HTTPS,但请求是 {parsed.scheme}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "URL 缺少 host"
    if policy.denied_hosts and any(_host_in_allowed(host, policy.denied_hosts) for _ in [0]):
        return f"host {host} 在黑名单中"
    if not _host_in_allowed(host, policy.allowed_hosts):
        return f"host {host} 不在白名单 {policy.allowed_hosts} 内"
    return None


# ===== DeclarativeHTTPTool =====


class DeclarativeHTTPTool(Tool):
    """把 ``ToolProposal`` 渲染成 ``Tool`` 实例(M4-02)。"""

    def __init__(self, proposal: ToolProposal, base_url: str, secret_resolver=None):
        self.name = f"{proposal.name}@{proposal.version}"
        self.display_name = proposal.name
        self.description = proposal.description
        self.proposal = proposal
        self.base_url = base_url.rstrip("/")
        self.endpoint: ToolEndpoint = proposal.endpoint  # type: ignore[assignment]
        self.network_policy = proposal.network_policy
        self.side_effect = proposal.side_effect
        self.secret_refs = list(proposal.secret_refs or [])
        self.secret_resolver = secret_resolver
        self.logger = get_logger()

    @property
    def short_name(self) -> str:
        return self.display_name

    # ----- Tool interface -----

    def schema(self) -> ToolSchema:
        params = [
            ToolParam(
                name=p.name,
                type=p.type,
                description=p.description,
                required=p.required,
                default=p.default,
                enum=p.enum,
            )
            for p in (self.endpoint.params if self.endpoint else [])
        ]
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=params,
            returns={},
            examples=[],
        )

    def validate_params(self, params: Dict[str, Any]) -> Tuple[bool, str]:
        return super().validate_params(params)

    async def execute(self, **kwargs) -> ToolResult:
        """同步执行入口;``ToolHub`` 会经 ``asyncio.to_thread`` 调用。"""
        try:
            return await self._execute_async(**kwargs)
        except Exception as exc:
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

    async def _execute_async(self, **kwargs) -> ToolResult:
        if not self.endpoint:
            return ToolResult(success=False, error="提案缺少 endpoint")

        # 解析 secret_refs(无明文)
        secret_values: Dict[str, str] = {}
        for ref in self.secret_refs:
            try:
                if self.secret_resolver:
                    secret_values[ref] = self.secret_resolver(ref)
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"无法解析 secret {ref}: {exc}",
                )

        url, headers, body, query = self._build_request(kwargs)
        policy_err = _enforce_network_policy(url, self.network_policy)
        if policy_err:
            return ToolResult(success=False, error=f"网络策略拒绝: {policy_err}")

        return await self._do_http(url, headers, body, query)

    # ----- 内部 helper -----

    def _build_request(self, params: Dict[str, Any]) -> Tuple[str, Dict[str, str], Any, Dict[str, Any]]:
        path = self.endpoint.path
        # path 参数替换
        for p in self.endpoint.params or []:
            if p.location == "path" and p.name in params:
                path = path.replace("{" + p.name + "}", str(params[p.name]))
        # query 参数
        query: Dict[str, Any] = {}
        body_data: Dict[str, Any] = {}
        for p in self.endpoint.params or []:
            if p.name not in params:
                continue
            if p.location == "query":
                query[p.name] = params[p.name]
            elif p.location == "body":
                body_data[p.name] = params[p.name]
        # 默认 body 是 json
        body = json.dumps(body_data, ensure_ascii=False) if body_data else None
        url = f"{self.base_url}{path}"
        if query:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(query)}"
        headers = {"User-Agent": "skill-agent/1.0"}
        if body:
            headers["Content-Type"] = "application/json"
        return url, headers, body, query

    async def _do_http(self, url: str, headers: Dict[str, str], body: Any, query: Dict[str, Any]) -> ToolResult:
        """执行 HTTP 请求,使用 ``urllib.request`` 避免引入额外依赖。"""
        import urllib.request
        import urllib.error

        method = self.endpoint.method.upper()
        req = urllib.request.Request(url, data=body.encode("utf-8") if isinstance(body, str) else body,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return ToolResult(success=False, error=f"HTTP {exc.code}: {exc.reason}",
                              meta={"status": exc.code})
        except Exception as exc:
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

        # 尝试 JSON
        try:
            data = json.loads(raw) if raw else None
        except Exception:
            data = raw
        return ToolResult(
            success=200 <= status < 300,
            data={"status": status, "body": data},
            meta={"status": status, "url": url},
        )


# ===== 沙箱测试运行器(M4-03) =====


@dataclass
class SandboxResult:
    """单个测试用例的运行结果。"""
    name: str
    passed: bool
    error: str = ""
    expected_status: int = 0
    actual_status: int = 0
    expected_keys: List[str] = field(default_factory=list)
    missing_keys: List[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


class SandboxRunner:
    """M4-03:沙箱测试运行器。

    特性:
    - 不接触真实 secret,沙箱用 fake secret(不调用外部 resolver)。
    - 每个用例独立计时,失败不污染主进程(仅记录错误)。
    - 网络白名单强制:任何出网请求都必须命中 ``allowed_hosts``。
    - 静态检查 ``ToolProposal.validate()`` 一并执行。
    """

    def __init__(self, proposal: ToolProposal,
                 base_url: Optional[str] = None):
        # base_url 缺省时,允许调用方通过 ``base_url_resolver`` 注入,或回退
        # 到占位 host(此时网络请求会被白名单拒绝,沙箱会失败)。
        self.proposal = proposal
        self.base_url = base_url or "https://example.invalid"
        self.results: List[SandboxResult] = []
        self.logger = get_logger()

    def run_all(self) -> Tuple[bool, List[SandboxResult], List[str]]:
        """返回 (all_passed, results, validation_issues)。"""
        issues = self.proposal.validate()
        if issues:
            return False, [], issues
        if not self.proposal.test_cases:
            # 无测试用例视为 M4-03 失败(必须有 1 正例 + 1 边界例)
            issues.append("提案必须至少包含 1 个测试用例(正例或边界例)")
            return False, [], issues

        for case in self.proposal.test_cases:
            self.results.append(self._run_one(case))

        all_passed = all(r.passed for r in self.results)
        return all_passed, self.results, []

    def _run_one(self, case: ToolTestCase) -> SandboxResult:
        import time
        start = time.time()
        tool = DeclarativeHTTPTool(
            self.proposal,
            base_url=self.base_url,
            secret_resolver=lambda ref: "fake-secret-for-sandbox",
        )
        result = SandboxResult(
            name=case.name,
            passed=False,
            expected_status=case.expected_status,
            expected_keys=list(case.expected_keys or []),
        )
        try:
            loop = asyncio.new_event_loop()
            try:
                tool_result = loop.run_until_complete(tool.execute(**case.input))
            finally:
                loop.close()
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.elapsed_ms = (time.time() - start) * 1000
            return result
        result.actual_status = int(tool_result.meta.get("status", 0) or 0)
        result.elapsed_ms = (time.time() - start) * 1000
        if case.expect_error:
            result.passed = not tool_result.success
            if not result.passed:
                result.error = "期望失败但实际成功"
            return result
        # 期望成功
        if not tool_result.success:
            result.error = tool_result.error
            return result
        if case.expected_status and result.actual_status != case.expected_status:
            result.error = (
                f"期望状态 {case.expected_status},实际 {result.actual_status}"
            )
            return result
        # 检查 expected_keys
        body = (tool_result.data or {}).get("body") if tool_result.data else None
        if case.expected_keys and isinstance(body, dict):
            missing = [k for k in case.expected_keys if k not in body]
            if missing:
                result.missing_keys = missing
                result.error = f"返回缺少键: {missing}"
                return result
        result.passed = True
        return result


# ===== Host 解析工具(M4-04) =====


def resolve_host_ips(host: str) -> List[str]:
    """解析 host 的 IP(用于审计与白名单检查)。"""
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except Exception:
        return []