"""Tool Proposal 数据模型(M4-01)。

依据 ``docs/10-目标架构评审与演进方案.md §6.4 §7.4 §10 M4`` 与
``docs/11-开发任务清单.md M4-01/M4-02/M4-04``。

设计要点:
- **不可变版本**: 工具版本一旦发布就不允许原地修改;更新必须新建版本。
- **声明式优先**: 通过 YAML 描述 HTTP/MCP 工具的接口、权限、网络白名单、
  secret 引用;运行时不依赖 LLM 实时生成 Python 代码。
- **副作用分级**: ``read_only`` / ``local_write`` / ``network_write`` /
  ``destructive`` 四级,审批策略分别对应:自动通过 / 普通审批 / 高风险审批 /
  禁止自动发布。
- **secret 引用**: 工具 YAML 只存 ``secret_refs``(指向 ``infra/config``
  中的密钥名),不在 YAML 内出现明文。
- **网络白名单**: ``network_policy.allowed_hosts`` 显式声明可访问域名。
- **测试用例**: ``test_cases`` 列出正例/反例,沙箱运行时验证。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class SideEffectLevel(str, Enum):
    """副作用等级(M4-01)。"""
    READ_ONLY = "read_only"               # 只读:可自动发布
    LOCAL_WRITE = "local_write"           # 写本地文件:需普通审批
    NETWORK_WRITE = "network_write"       # 写远端:需高风险审批
    DESTRUCTIVE = "destructive"           # 删除/不可逆:禁止自动发布


class ToolProposalStatus(str, Enum):
    """提案生命周期状态(M4-01/M4-05)。"""
    DRAFT = "draft"           # 用户/Agent 创建的草案
    SANDBOX_OK = "sandbox_ok" # 沙箱测试通过,等待审批
    SANDBOX_FAILED = "sandbox_failed"
    APPROVED = "approved"     # 已审批,等待生效
    PUBLISHED = "published"   # 已注册到 ToolHub,可用
    REJECTED = "rejected"
    DISABLED = "disabled"     # 审批通过后被禁用


# 副作用等级 → 是否允许自动发布
AUTO_PUBLISH_ALLOWED = {
    SideEffectLevel.READ_ONLY.value,
}


@dataclass
class NetworkPolicy:
    """网络访问策略(M4-04)。"""
    allowed_hosts: List[str] = field(default_factory=list)   # 域名白名单(支持 "*.example.com")
    denied_hosts: List[str] = field(default_factory=list)
    require_https: bool = True


@dataclass
class ToolParamSpec:
    """参数定义(M4-01)。"""
    name: str
    type: str                        # string/number/integer/boolean/object/array
    description: str = ""
    required: bool = True
    default: Any = None
    enum: Optional[List[Any]] = None
    location: str = "body"           # query/path/header/body


@dataclass
class ToolEndpoint:
    """声明式 HTTP 工具的一个 endpoint(M4-02)。"""
    method: str                       # GET/POST/PUT/DELETE/PATCH
    path: str                         # 例如 "/users/{id}"
    summary: str = ""
    params: List[ToolParamSpec] = field(default_factory=list)
    returns: Dict[str, str] = field(default_factory=dict)


@dataclass
class ToolTestCase:
    """测试样例(M4-01/M4-03)。"""
    name: str
    input: Dict[str, Any] = field(default_factory=dict)
    expected_status: int = 200
    expected_keys: List[str] = field(default_factory=list)
    expect_error: bool = False


@dataclass
class ToolProposal:
    """一个工具提案的完整描述(M4-01)。

    YAML 示例:

    .. code-block:: yaml

        name: github_user_lookup
        version: 1.0.0
        runtime: declarative_http
        endpoint:
          method: GET
          path: /users/{username}
          params:
            - {name: username, type: string, location: path, required: true}
        permissions: [network.read]
        network_policy:
          allowed_hosts: [api.github.com]
        side_effect: read_only
        secret_refs: []
        test_cases:
          - name: real_user
            input: {username: octocat}
            expected_keys: [login]
    """
    name: str
    version: str = "1.0.0"
    runtime: str = "declarative_http"        # declarative_http / mcp / python (M4-01 默认仅 declarative_http/mcp)
    description: str = ""
    endpoint: Optional[ToolEndpoint] = None
    permissions: List[str] = field(default_factory=list)
    network_policy: NetworkPolicy = field(default_factory=NetworkPolicy)
    side_effect: str = SideEffectLevel.READ_ONLY.value
    secret_refs: List[str] = field(default_factory=list)   # 引用名,非明文
    test_cases: List[ToolTestCase] = field(default_factory=list)
    status: str = ToolProposalStatus.DRAFT.value
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    proposal_id: str = ""
    notes: str = ""

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.proposal_id:
            self.proposal_id = f"tprop-{uuid.uuid4().hex[:10]}"
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        if isinstance(self.network_policy, dict):
            self.network_policy = NetworkPolicy(**self.network_policy)
        if isinstance(self.endpoint, dict):
            self.endpoint = ToolEndpoint(**{
                **self.endpoint,
                "params": [ToolParamSpec(**p) if isinstance(p, dict) else p
                           for p in self.endpoint.get("params", [])],
            })
        elif self.endpoint is not None and not isinstance(self.endpoint, ToolEndpoint):
            self.endpoint = ToolEndpoint(**asdict(self.endpoint))

    # ===== 序列化 =====

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_yaml_dict(self) -> Dict[str, Any]:
        """给 ``yaml.safe_dump`` 用的纯 dict(``ToolEndpoint`` 已嵌套好)。"""
        return self.to_dict()

    # ===== 校验 =====

    def validate(self) -> List[str]:
        """静态校验:M4-01/M4-05 必需的字段及一致性。

        返回错误列表;空列表表示通过。
        """
        issues: List[str] = []
        if not re.match(r"^[A-Za-z][A-Za-z0-9_\-]*$", self.name or ""):
            issues.append(f"name 非法: {self.name!r}")
        if not re.match(r"^\d+\.\d+\.\d+$", self.version or ""):
            issues.append(f"version 必须为 semver: {self.version!r}")
        if self.runtime not in ("declarative_http", "mcp"):
            issues.append(
                f"runtime {self.runtime!r} 暂不支持(仅 declarative_http / mcp)"
            )
        if self.runtime == "declarative_http" and not self.endpoint:
            issues.append("declarative_http 工具必须声明 endpoint")
        if self.endpoint:
            if self.endpoint.method.upper() not in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                issues.append(f"endpoint.method 非法: {self.endpoint.method}")
            if not self.endpoint.path.startswith("/"):
                issues.append(f"endpoint.path 必须以 / 开头: {self.endpoint.path}")
        if self.side_effect not in [e.value for e in SideEffectLevel]:
            issues.append(f"side_effect 非法: {self.side_effect}")
        # secret 引用必须命名空间化,且不能含明文
        for ref in self.secret_refs or []:
            if not re.match(r"^[a-z][a-z0-9_\-]*(\.[a-z][a-z0-9_\-]*)+$", ref):
                issues.append(f"secret_ref 必须是命名空间引用(如 'github.token'): {ref!r}")
        # 网络白名单必须包含至少一个 host(若工具需要网络)
        if self.runtime in ("declarative_http", "mcp"):
            if not self.network_policy.allowed_hosts:
                issues.append("必须声明至少一个 allowed_host")
        return issues

    def is_auto_publishable(self) -> bool:
        """M4-05:只有 read_only 工具且测试通过才能自动发布。"""
        return self.side_effect in AUTO_PUBLISH_ALLOWED


# ===== 提案存储(M4-01) =====


class ToolProposalStore:
    """Tool Proposal 的内存 + 文件存储(M4-01/M4-05)。

    存储位置: ``<base_path>/tools/proposals/<name>@<version>.yaml``
    """

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "tools"
        self.base = Path(base_path)
        self.dir = self.base / "proposals"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str, version: str) -> Path:
        return self.dir / f"{name}@{version}.yaml"

    def save(self, proposal: ToolProposal, *, overwrite: bool = False) -> Path:
        """保存一个提案。

        - ``overwrite=False``(默认):同 ``name@version`` 视为版本冲突,抛 ``FileExistsError``。
          这是 ``create new version`` 的强制约束,避免原地覆盖。
        - ``overwrite=True``:仅用于状态机迁移(sandbox→approved→published 等)。
          不允许新增字段,只覆盖 status / updated_at 等元数据。
        """
        import yaml
        proposal.updated_at = datetime.now().isoformat()
        path = self._path(proposal.name, proposal.version)
        if path.exists() and not overwrite:
            raise FileExistsError(f"提案已存在: {path}")
        path.write_text(
            yaml.safe_dump(proposal.to_yaml_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def get(self, name: str, version: str) -> Optional[ToolProposal]:
        import yaml
        path = self._path(name, version)
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        return _proposal_from_dict(data)

    def list_versions(self, name: str) -> List[str]:
        prefix = f"{name}@"
        return sorted(
            p.stem[len(prefix):]
            for p in self.dir.glob(f"{name}@*.yaml")
        )

    def list_all(self) -> List[ToolProposal]:
        out: List[ToolProposal] = []
        for p in self.dir.glob("*@*.yaml"):
            try:
                import yaml
                out.append(_proposal_from_dict(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))
            except Exception:
                continue
        return out

    def delete(self, name: str, version: str) -> bool:
        path = self._path(name, version)
        if path.exists():
            path.unlink()
            return True
        return False


def _proposal_from_dict(d: Dict[str, Any]) -> ToolProposal:
    """从 dict 反序列化为 ToolProposal(dataclass 自动处理嵌套)。"""
    return ToolProposal(**d)


# ===== 单例 =====

_store: Optional[ToolProposalStore] = None


def get_tool_proposal_store() -> ToolProposalStore:
    global _store
    if _store is None:
        _store = ToolProposalStore()
    return _store


def reset_tool_proposal_store() -> None:
    global _store
    _store = None