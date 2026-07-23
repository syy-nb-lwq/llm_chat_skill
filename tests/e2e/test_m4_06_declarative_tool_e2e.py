"""M4-06:e2e 声明式工具全链路。

依据 ``docs/11-开发任务清单.md M4-06``:
- 用户描述只读 API
- 生成提案
- 沙箱通过(可使用 mock http server)
- 审批
- 注册
- 在 ToolHub 可被发现
- 禁用
"""
from __future__ import annotations

import asyncio
import http.server
import json
import socketserver
import threading
import time
from pathlib import Path

import pytest

from tools.proposal import (
    NetworkPolicy,
    SideEffectLevel,
    ToolEndpoint,
    ToolParamSpec,
    ToolProposal,
    ToolProposalStatus,
    ToolProposalStore,
    ToolTestCase,
)
from tools.approval import ToolApprovalService
from tools.declarative_http import DeclarativeHTTPTool
from tools.hub import ToolHub


# ===== mock http server =====


class _MockHandler(http.server.BaseHTTPRequestHandler):
    """返回固定的 JSON 响应,用于声明式工具的端到端验证。"""

    routes = {
        ("GET", "/users/octocat"): {"login": "octocat", "id": 1},
    }

    def log_message(self, format, *args):  # 抑制 stderr
        pass

    def do_GET(self):
        key = ("GET", self.path)
        body = self.routes.get(key, {"error": "not_found"})
        status = 200 if key in self.routes else 404
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def mock_server():
    """启动一个本地 mock http server,监听 0 端口(随机空闲端口)。"""
    server = socketserver.TCPServer(("127.0.0.1", 0), _MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f"http://localhost:{port}"
    server.shutdown()
    thread.join(timeout=2)


# ===== Tests =====


@pytest.fixture
def fresh_store(tmp_path):
    return ToolProposalStore(base_path=tmp_path)


def _make_proposal(base_url: str) -> ToolProposal:
    """构造一个简单的 GET 工具提案(成功用例 + 边界用例)。"""
    from urllib.parse import urlparse
    host = urlparse(base_url).hostname or "localhost"
    return ToolProposal(
        name="github_user",
        version="1.0.0",
        runtime="declarative_http",
        description="lookup github user by username",
        endpoint=ToolEndpoint(
            method="GET",
            path="/users/{username}",
            params=[ToolParamSpec(name="username", type="string",
                                  location="path", required=True)],
        ),
        permissions=["network.read"],
        network_policy=NetworkPolicy(allowed_hosts=[host, "localhost"],
                                     require_https=False),
        side_effect=SideEffectLevel.READ_ONLY.value,
        secret_refs=[],
        test_cases=[
            ToolTestCase(name="exists", input={"username": "octocat"},
                         expected_status=200, expected_keys=["login"]),
            ToolTestCase(name="missing", input={"username": "ghost"},
                         expected_status=404, expect_error=True),
        ],
    )


def test_full_lifecycle_draft_to_published(fresh_store, mock_server):
    """DRAFT → SANDBOX_OK → APPROVED → PUBLISHED,工具可在 hub 中被发现。"""
    p = _make_proposal(mock_server)
    fresh_store.save(p)

    fake_hub = ToolHub()  # 真实 hub,隔离
    svc = ToolApprovalService(
        store=fresh_store,
        tool_hub=fake_hub,
        base_url_resolver=lambda prop: mock_server,
    )

    # 1. 沙箱
    sb = svc.run_sandbox(p)
    assert sb.ok is True, sb.message
    assert sb.status == ToolProposalStatus.SANDBOX_OK.value

    # 2. 审批
    ap = svc.approve("github_user", "1.0.0", approver="admin")
    assert ap.ok is True, ap.message

    # 3. 发布
    pub = svc.publish("github_user", "1.0.0")
    assert pub.ok is True, pub.message
    assert pub.status == ToolProposalStatus.PUBLISHED.value
    # hub 中存在
    assert "github_user@1.0.0" in fake_hub.names()


def test_disabled_tool_removed_from_hub(fresh_store, mock_server):
    """禁用后,工具从 hub 移除。"""
    p = _make_proposal(mock_server)
    p.status = ToolProposalStatus.APPROVED.value  # 跳过沙箱
    fresh_store.save(p)
    fake_hub = ToolHub()
    svc = ToolApprovalService(
        store=fresh_store, tool_hub=fake_hub,
        base_url_resolver=lambda prop: mock_server,
    )
    svc.publish("github_user", "1.0.0")
    assert "github_user@1.0.0" in fake_hub.names()
    dis = svc.disable("github_user", "1.0.0")
    assert dis.ok is True
    assert "github_user@1.0.0" not in fake_hub.names()


@pytest.mark.asyncio
async def test_real_call_after_publish(fresh_store, mock_server):
    """工具发布后能真正调用并得到 mock server 的响应。"""
    p = _make_proposal(mock_server)
    p.status = ToolProposalStatus.APPROVED.value
    fresh_store.save(p)
    fake_hub = ToolHub()
    svc = ToolApprovalService(
        store=fresh_store, tool_hub=fake_hub,
        base_url_resolver=lambda prop: mock_server,
    )
    pub = svc.publish("github_user", "1.0.0")
    assert pub.ok is True

    info = fake_hub.get_tool("github_user@1.0.0")
    assert info is not None
    instance: DeclarativeHTTPTool = info.instance
    result = await instance.execute(username="octocat")
    assert result.success is True, result.error
    body = (result.data or {}).get("body") or {}
    assert body.get("login") == "octocat"


def test_audit_log_records_lifecycle(fresh_store, mock_server):
    """审计日志记录 sandbox / approve / publish / disable 全过程。"""
    p = _make_proposal(mock_server)
    fresh_store.save(p)
    svc = ToolApprovalService(
        store=fresh_store, tool_hub=ToolHub(),
        base_url_resolver=lambda prop: mock_server,
    )
    svc.run_sandbox(p)
    svc.approve("github_user", "1.0.0", approver="alice")
    svc.publish("github_user", "1.0.0")
    svc.disable("github_user", "1.0.0")
    actions = [a["action"] for a in svc.audit_log]
    # 沙箱会被调用并触发 save(overwrite),但 audit_log 不直接记录;我们检查后续
    assert "approve" in actions
    assert "publish" in actions
    assert "disable" in actions