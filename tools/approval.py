"""Tool 审批与发布流程(M4-05)。

设计:
- ``approve_proposal(name, version, approver)``:把 ``DRAFT`` /
  ``SANDBOX_OK`` 状态推进到 ``APPROVED`` / ``PUBLISHED``。
- ``publish_proposal(name, version)``:把已审批的提案注册到 ``ToolHub``。
- ``disable_proposal(name, version)``:把已发布但需要下线的工具注销。
- 副作用等级 ``read_only`` 时,可配置自动发布(由 ``infra.config`` 控制)。
- 副作用等级 ``destructive`` 永远禁止自动发布。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from infra.logger import get_logger
from tools.declarative_http import DeclarativeHTTPTool, SandboxRunner
from tools.proposal import (
    SideEffectLevel,
    ToolProposal,
    ToolProposalStatus,
    ToolProposalStore,
    get_tool_proposal_store,
)


@dataclass
class ApprovalResult:
    ok: bool
    status: str
    message: str
    issues: List[str] = None
    sandbox_results: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.issues is None:
            self.issues = []
        if self.sandbox_results is None:
            self.sandbox_results = []


class ToolApprovalService:
    """Tool 提案审批 + 注册(M4-05)。"""

    def __init__(self,
                 store: Optional[ToolProposalStore] = None,
                 tool_hub: Any = None,
                 config: Any = None,
                 base_url_resolver: Optional[Any] = None):
        """Args:
            base_url_resolver: ``Callable[[ToolProposal], str]``,把提案映射到
              base_url(例如根据 ``secret_refs`` 或 ``infra.config`` 解析)。
              测试时可以通过这个钩子注入 mock server 地址。
        """
        self.store = store or get_tool_proposal_store()
        # 不在 import 时强制依赖 hub,允许单独测试
        self._tool_hub = tool_hub
        self.config = config
        self.base_url_resolver = base_url_resolver
        self.logger = get_logger()
        # 记录每次审批的审计日志
        self.audit_log: List[Dict[str, Any]] = []

    @property
    def tool_hub(self):
        if self._tool_hub is None:
            from tools.hub import get_tool_hub
            self._tool_hub = get_tool_hub()
        return self._tool_hub

    # ===== Sandbox =====

    def run_sandbox(self, proposal: ToolProposal) -> ApprovalResult:
        """运行沙箱测试;返回 ``SANDBOX_OK`` 或 ``SANDBOX_FAILED``。"""
        # 优先使用 base_url_resolver(测试钩子);否则从 secret_refs 推断。
        base_url = None
        if self.base_url_resolver is not None:
            try:
                base_url = self.base_url_resolver(proposal)
            except Exception as exc:
                self.logger.warning("Tools", f"base_url_resolver failed: {exc}")
        if not base_url:
            for ref in proposal.secret_refs or []:
                if ref.startswith("url."):
                    base_url = ref[4:]
                    break
        runner = SandboxRunner(proposal, base_url=base_url)
        ok, results, issues = runner.run_all()
        result_dicts = [
            {
                "name": r.name,
                "passed": r.passed,
                "expected_status": r.expected_status,
                "actual_status": r.actual_status,
                "expected_keys": r.expected_keys,
                "missing_keys": r.missing_keys,
                "elapsed_ms": round(r.elapsed_ms, 1),
                "error": r.error,
            }
            for r in results
        ]
        if not ok:
            proposal.status = ToolProposalStatus.SANDBOX_FAILED.value
            self.store.save(proposal, overwrite=True)
            return ApprovalResult(
                ok=False,
                status=ToolProposalStatus.SANDBOX_FAILED.value,
                message="沙箱测试未通过",
                issues=issues,
                sandbox_results=result_dicts,
            )
        proposal.status = ToolProposalStatus.SANDBOX_OK.value
        self.store.save(proposal, overwrite=True)
        return ApprovalResult(
            ok=True,
            status=ToolProposalStatus.SANDBOX_OK.value,
            message="沙箱测试通过,等待审批",
            sandbox_results=result_dicts,
        )

    # ===== Approval =====

    def approve(self, name: str, version: str, approver: str = "admin") -> ApprovalResult:
        prop = self.store.get(name, version)
        if not prop:
            return ApprovalResult(False, "not_found", f"找不到提案: {name}@{version}")

        if prop.status not in (
            ToolProposalStatus.DRAFT.value,
            ToolProposalStatus.SANDBOX_OK.value,
        ):
            return ApprovalResult(
                False, prop.status,
                f"提案当前状态 {prop.status},不允许审批",
            )

        # M4-05:破坏性操作永远需要人工审批(approver 必须显式)
        if prop.side_effect == SideEffectLevel.DESTRUCTIVE.value and approver == "auto":
            return ApprovalResult(
                False, prop.status,
                "destructive 工具禁止自动发布",
            )

        prop.status = ToolProposalStatus.APPROVED.value
        self.store.save(prop, overwrite=True)
        self.audit_log.append({
            "action": "approve",
            "name": name,
            "version": version,
            "approver": approver,
            "side_effect": prop.side_effect,
        })
        return ApprovalResult(
            True, prop.status,
            f"已审批 {name}@{version} (approver={approver})",
        )

    def reject(self, name: str, version: str, reason: str = "") -> ApprovalResult:
        prop = self.store.get(name, version)
        if not prop:
            return ApprovalResult(False, "not_found", f"找不到提案: {name}@{version}")
        prop.status = ToolProposalStatus.REJECTED.value
        prop.notes = (prop.notes or "") + f"\n[rejected] {reason}"
        self.store.save(prop, overwrite=True)
        self.audit_log.append({
            "action": "reject",
            "name": name,
            "version": version,
            "reason": reason,
        })
        return ApprovalResult(True, prop.status, f"已驳回 {name}@{version}: {reason}")

    # ===== Publish / Disable =====

    def publish(self, name: str, version: str) -> ApprovalResult:
        prop = self.store.get(name, version)
        if not prop:
            return ApprovalResult(False, "not_found", f"找不到提案: {name}@{version}")
        if prop.status != ToolProposalStatus.APPROVED.value:
            return ApprovalResult(
                False, prop.status,
                f"提案必须先审批,当前状态 {prop.status}",
            )
        ok = self._register_to_hub(prop)
        if not ok:
            return ApprovalResult(False, prop.status, "注册到 ToolHub 失败")
        prop.status = ToolProposalStatus.PUBLISHED.value
        self.store.save(prop, overwrite=True)
        self.audit_log.append({
            "action": "publish",
            "name": name,
            "version": version,
        })
        return ApprovalResult(True, prop.status, f"已发布 {name}@{version}")

    def disable(self, name: str, version: str) -> ApprovalResult:
        prop = self.store.get(name, version)
        if not prop:
            return ApprovalResult(False, "not_found", f"找不到提案: {name}@{version}")
        full = f"{name}@{version}"
        hub = self.tool_hub
        removed = False
        try:
            removed = hub.unregister_tool(full)
        except Exception as exc:
            self.logger.warning("Tools", f"unregister_tool failed: {exc}")
        if removed:
            prop.status = ToolProposalStatus.DISABLED.value
            self.store.save(prop, overwrite=True)
        self.audit_log.append({
            "action": "disable",
            "name": name,
            "version": version,
            "removed": removed,
        })
        return ApprovalResult(
            True, prop.status,
            f"已下线 {name}@{version}",
        )

    # ===== Helpers =====

    def _register_to_hub(self, prop: ToolProposal) -> bool:
        if prop.runtime not in ("declarative_http", "mcp"):
            self.logger.warning("Tools", f"runtime {prop.runtime} not supported")
            return False
        # 解析 base_url:优先使用 base_url_resolver(测试钩子),否则从
        # secret_refs 的 ``url.<scheme>`` 命名读取,最后回退到一个占位 host。
        base_url = None
        if self.base_url_resolver is not None:
            try:
                base_url = self.base_url_resolver(prop)
            except Exception as exc:
                self.logger.warning("Tools", f"base_url_resolver failed: {exc}")
        if not base_url:
            for ref in prop.secret_refs or []:
                if ref.startswith("url."):
                    base_url = ref[4:]
                    break
        if not base_url:
            # 占位 host,实际调用时会被网络白名单拒绝,但不影响注册本身。
            base_url = "https://example.invalid"
        tool = DeclarativeHTTPTool(prop, base_url=base_url)
        try:
            self.tool_hub.register(tool)
        except Exception as exc:
            self.logger.error("Tools", f"register failed: {exc}")
            return False
        return True


_singleton: Optional[ToolApprovalService] = None


def get_tool_approval_service() -> ToolApprovalService:
    global _singleton
    if _singleton is None:
        _singleton = ToolApprovalService()
    return _singleton


def reset_tool_approval_service() -> None:
    global _singleton
    _singleton = None