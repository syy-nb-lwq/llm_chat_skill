"""工具提案 —— 让 agent 自己造工具。

## 这是什么

这个模块让 **agent 提出一个新工具**，校验、测试、然后注册进 `ToolHub`，
从此 planner 就能像用内置工具一样用它。

也就是说：**能力可以自己长出来**，不用每次都改代码。

## 两种工具，两条路

| 类型 | 适合什么 | 怎么执行 | 风险 |
|---|---|---|---|
| `http`（声明式） | 调任何 REST API（查天气/查汇率/查公告…） | 按 spec 拼请求、抽响应 | 低（无代码执行） |
| `script` | 纯计算、格式转换（没有现成 API） | 跑一段脚本（走 python_exec 沙箱） | 中 |

**声明式是首选**：它只是数据（URL 模板 + 参数表），不执行任意代码，
所以能安全地自动发布。大部分"接一个新接口"的需求都落在这一类。

## 为什么不能想造就造

一个能自己造工具的 agent，等价于一个能改自己权限的进程。
所以这里有三道闸：

1. **静态校验**：名字合法、不撞已有工具、URL 必须 https、
   占位符和参数表必须对得上、脚本要过 AST 黑名单
2. **风险分级**：
   - `GET` + 无敏感头 → `low` → **可自动发布**
   - `POST/PUT/DELETE` 或带 `Authorization` 头 → `medium/high` → **必须人工批准**
3. **可选测试**：提案里带 `test_cases` 就先真跑一遍，跑不通不发布

> 判据：**只读的东西可以自动发布，会改外部状态的东西必须人点头。**

## 与 docs/11 M4 的对应

本模块实现 M4-01（提案数据模型）和 M4-02（声明式 HTTP 提案）；
M4-03（沙箱测试运行器）复用 `python_exec` 的隔离执行；
M4-04（secret 引用）走"头部含密钥就不许自动发布"这条规则。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from infra.logger import get_logger


#: 工具名规则：小写字母开头，允许数字和下划线
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")

#: 视为"会改外部状态"的方法
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 出现在头部就说明要用密钥 —— 有密钥就不许自动发布
SECRET_HEADER_MARKERS = ("authorization", "cookie", "x-api-key", "token", "secret")

#: URL 模板里的占位符
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

#: 提案状态
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"

#: 内联代码长度上限
MAX_CODE_CHARS = 60_000

#: 风险等级
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

#: 工具可见性（docs/18 §10.2）
#:
#: `global` —— 全局可见（**当前默认**：自造的工具默认进全局 ToolHub）
#: `owner`  —— 只有 owner_agent_id 相同的智能体可见
#: `tenant` —— 同租户可见（tenant 暂用 user 近似）
VISIBILITY_GLOBAL = "global"
VISIBILITY_OWNER = "owner"
VISIBILITY_TENANT = "tenant"


class ToolSpecError(Exception):
    """提案不合法。"""


def normalize_params(raw) -> List[Dict[str, Any]]:
    """把参数表归一化成 `[{name,type,description,required}]`。

    **为什么要容错**：实测 planner 会写成
    `{"owner": "仓库所有者", "repo": "仓库名"}`
    —— 也就是"参数名 → 描述"的字典，而不是标准列表。
    直接拒掉太苛刻了（它表达的意思完全正确），所以这里两种都收。
    """
    if not raw:
        return []
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                out.append(dict(item))
            elif isinstance(item, str):
                out.append({"name": item, "type": "string", "required": True})
        return out
    if isinstance(raw, dict):
        out = []
        for key, val in raw.items():
            name = str(key).strip()
            if not name:
                continue
            if isinstance(val, dict):
                entry = {"name": name}
                entry.update({k: v for k, v in val.items() if k != "name"})
                entry.setdefault("type", "string")
                entry.setdefault("required", True)
            else:
                entry = {
                    "name": name,
                    "type": "string",
                    "description": str(val or ""),
                    "required": True,
                }
            out.append(entry)
        return out
    return []


@dataclass
class ToolProposal:
    """一个工具提案。

    字段说明（只列关键的）：

    - `kind`: `http` 声明式 / `script` 脚本
    - `method` + `url_template`: 声明式工具怎么发请求
      （`url_template` 里用 `{param}` 引用参数）
    - `params`: 参数表，形如
      `[{"name":"city","type":"string","description":"城市","required":true}]`
    - `response_path`: 可选，从 JSON 响应里取哪一段（`data.items` / `list[0].v`）
    - `script_path`: 脚本类工具指向 workspace 里的脚本文件
    - `rationale`: 为什么需要这个工具（人审时看这句）
    """

    name: str
    description: str
    kind: str = "http"
    # --- 声明式 HTTP ---
    method: str = "GET"
    url_template: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    params: List[Dict[str, Any]] = field(default_factory=list)
    response_path: str = ""
    # --- 脚本 / 代码 ---
    script_path: str = ""
    #: kind=code 时的内联 Python 代码（agent 最自然的产出形态）
    implementation: str = ""
    # --- 元信息 ---
    rationale: str = ""
    test_cases: List[Dict[str, Any]] = field(default_factory=list)
    side_effect: str = "read"          # read | write
    risk: str = RISK_LOW
    status: str = STATUS_PENDING
    id: str = ""
    created_at: str = ""
    reviewed_by: str = ""
    source: str = "agent"
    validation: Dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""
    # --- 归属 ---
    #
    # 之前**没有归属**：提案存进 `memory/tools/proposals/*.json`，
    # 发布后进**全局单例** ToolHub，谁造的、能不能禁用，全都不知道。
    #
    # 现在的决定（docs/18 §10.2）：默认 `visibility="global"` —— 自造的工具
    # 默认全局可见。但 owner 必须落盘：
    #   - 能回答"这个工具是谁造的"
    #   - 能一键禁用某个 owner 的全部工具
    #   - 将来要收紧成按用户可见时，**只改过滤逻辑，不做数据迁移**
    owner_agent_id: str = ""
    visibility: str = VISIBILITY_GLOBAL   # global | owner | tenant

    def __post_init__(self):
        if not self.id:
            self.id = f"tool_{uuid.uuid4().hex[:12]}"
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        self.name = (self.name or "").strip()
        self.method = (self.method or "GET").strip().upper()
        self.kind = (self.kind or "http").strip().lower()
        # 有 implementation 就按代码工具处理（模型经常忘记写 kind）
        if self.implementation and self.kind in ("", "http") and not (self.url_template or "").strip():
            self.kind = "code"
        self.params = normalize_params(self.params)
        # owner 缺省时从当前执行上下文补（谁在造就是谁的）
        if not self.owner_agent_id:
            try:
                from core.exec_context import get_exec_context

                ctx = get_exec_context()
                self.owner_agent_id = ctx.agent_id or ""
            except Exception:
                pass
        self.visibility = (self.visibility or VISIBILITY_GLOBAL).strip().lower()

    def is_visible_to(self, agent_id: str = "") -> bool:
        """这个工具对某个调用者可见吗？

        本地单用户，没有 owner/tenant 维度 —— 可见性只按 agent_id 判断。
        - `global`：都可见（当前默认）
        - `owner`/`tenant`：只对创建它的智能体可见
        """
        vis = (self.visibility or VISIBILITY_GLOBAL).strip().lower()
        if vis == VISIBILITY_GLOBAL:
            return True
        if vis in (VISIBILITY_OWNER, VISIBILITY_TENANT):
            return bool(agent_id) and agent_id == self.owner_agent_id
        return True


    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolProposal":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


# ============================================================
# 静态校验
# ============================================================

def validate_proposal(
    proposal: ToolProposal,
    existing_names: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """静态校验。返回 `(errors, warnings)`。

    errors 非空 → **不能发布**;warnings 只是提醒。
    """
    errors: List[str] = []
    warnings: List[str] = []

    # --- 名字 ---
    if not proposal.name:
        errors.append("name 不能为空")
    elif not _NAME_RE.match(proposal.name):
        errors.append(
            f"name {proposal.name!r} 不合法：要小写字母开头、只含小写字母/数字/下划线、3~40 字符"
        )
    if existing_names and proposal.name in existing_names:
        errors.append(f"工具名 {proposal.name!r} 已存在，换一个")

    # --- 描述 ---
    if not (proposal.description or "").strip():
        errors.append("description 不能为空（planner 靠它决定要不要用这个工具）")
    elif len(proposal.description) > 300:
        warnings.append("description 过长（建议 100 字以内，planner 更容易判断）")

    if not (proposal.rationale or "").strip():
        warnings.append("缺少 rationale（为什么需要这个工具），人审时会不好判断")

    # --- 类型 ---
    if proposal.kind not in ("http", "script", "code"):
        errors.append(f"kind 只能是 http / script / code，收到 {proposal.kind!r}")

    if proposal.kind == "http":
        errors.extend(_validate_http(proposal, warnings))
    elif proposal.kind in ("script", "code"):
        errors.extend(_validate_script(proposal, warnings))

    # --- 参数表自检 ---
    seen = set()
    for p in proposal.params or []:
        if not isinstance(p, dict):
            errors.append(f"params 里的项必须是对象，收到 {type(p).__name__}")
            continue
        pname = str(p.get("name") or "").strip()
        if not pname:
            errors.append("params 里有一项缺少 name")
            continue
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", pname):
            errors.append(f"参数名 {pname!r} 不合法（只能是字母/数字/下划线）")
        if pname in seen:
            errors.append(f"参数名 {pname!r} 重复")
        seen.add(pname)
        if p.get("type") not in (None, "string", "integer", "number", "boolean", "array", "object"):
            errors.append(f"参数 {pname!r} 的 type 不支持：{p.get('type')!r}")

    return errors, warnings


def _validate_http(proposal: ToolProposal, warnings: List[str]) -> List[str]:
    errors: List[str] = []

    method = proposal.method
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        errors.append(f"method {method!r} 不支持")

    url = (proposal.url_template or "").strip()
    if not url:
        # **给出可执行的替代方案** —— 实测 planner 想做"算 BMI"这种纯计算工具时，
        # 会误写成 http 类（它以为工具都是调接口的）。
        # 只说"缺 url_template"它不知道该怎么改，于是原地卡死。
        errors.append(
            "http 类工具必须给 url_template。"
            "如果你要做的是**纯计算/不调接口**的工具，请改成 kind=\"code\" 并把代码放进 "
            "implementation（参数用 PARAMS['名'] 取，结果用 return 返回）。"
        )
    else:
        # **只允许 https**：http 明文会把参数（可能含敏感信息）暴露在链路里
        if not url.lower().startswith("https://"):
            errors.append(
                f"url_template 必须是 https:// 开头（收到 {url[:40]!r}）"
            )
        declared = {str(p.get("name")) for p in (proposal.params or []) if isinstance(p, dict)}
        used = set(_PLACEHOLDER_RE.findall(url))
        unknown = used - declared
        if unknown:
            errors.append(
                f"url_template 用了没声明的参数：{sorted(unknown)}"
                f"（请补进 params，或从模板里去掉）"
            )
        unused = declared - used
        if unused and method == "GET":
            warnings.append(f"参数 {sorted(unused)} 没有用在 url_template 里（GET 会忽略它们）")

    # 头部里有密钥 → 不能自动发布
    for hname in (proposal.headers or {}):
        low = str(hname).lower()
        if any(marker in low for marker in SECRET_HEADER_MARKERS):
            warnings.append(
                f"头部 {hname!r} 像是密钥 —— 这类工具不会自动发布，需要人工批准"
            )

    if method in WRITE_METHODS:
        warnings.append(f"{method} 会改外部状态 —— 需要人工批准")

    return errors


def _validate_script(proposal: ToolProposal, warnings: List[str]) -> List[str]:
    """校验脚本类 / 代码类工具。

    - `kind=script`: 指向工作区里已有脚本
    - `kind=code`: 提案里带内联代码（模型最自然的产出形态）
    """
    errors: List[str] = []

    from tools.python_exec import Severity, blocking_findings, check_code

    if proposal.kind == "code":
        code = proposal.implementation or ""
        if not code.strip():
            errors.append("kind=code 必须给 implementation（要执行的 Python 代码）")
            return errors
        if len(code) > MAX_CODE_CHARS:
            errors.append(f"implementation 太长（{len(code)} 字符，上限 {MAX_CODE_CHARS}）")
            return errors
        findings = check_code(code)
        hard = blocking_findings(findings)
        if hard:
            errors.append("代码没通过安全检查：" + "; ".join(f.message for f in hard))
        warnings.extend(f.message for f in findings if f.severity is Severity.WARN)
        if "PARAMS" not in code and proposal.params:
            warnings.append(
                "代码里没用到 PARAMS —— 参数会以全局变量 PARAMS(dict) 注入，"
                "不用它就等于没有参数"
            )
        if "return" not in code and "print(" not in code:
            warnings.append(
                "代码既没有 return 也没有 print —— 调用时会拿不到任何输出"
                "（建议 `return {...}` 或 `print(...)`）"
            )
        return errors

    # kind == "script"
    rel = (proposal.script_path or "").strip()
    if not rel:
        errors.append("script 类工具必须给 script_path（或改用 implementation 内联代码）")
        return errors

    from tools.python_exec import _resolve_workspace_script

    target = _resolve_workspace_script(rel)
    if target is None:
        errors.append(
            f"脚本不存在或不在工作区内：{rel!r}"
            f"（先用 write_script 把脚本存进来，或改用 implementation）"
        )
        return errors

    try:
        code = target.read_text(encoding="utf-8")
    except Exception as exc:
        errors.append(f"读取脚本失败：{exc}")
        return errors

    findings = check_code(code)
    hard = blocking_findings(findings)
    if hard:
        errors.append("脚本没通过安全检查：" + "; ".join(f.message for f in hard))
    warnings.extend(f.message for f in findings if f.severity is Severity.WARN)

    if "PARAMS" not in code and proposal.params:
        warnings.append(
            "脚本里没用到 PARAMS —— 参数会通过全局变量 PARAMS(dict) 注入，"
            "不用它就等于没有参数"
        )

    return errors


# ============================================================
# 风险分级
# ============================================================

def assess_risk(proposal: ToolProposal) -> str:
    """算风险等级。

    **规则**：

    - HTTP 写方法 / 带密钥头 → `high`（会改外部世界，必须人批）
    - `script`（引用工作区已存脚本）→ `medium`（必须人批）
    - `code`（内联代码）→ **看 `CODE_EXEC_ENABLED`**：
      - 开着 → `low`（**和 `python_exec` 同一风险类**，见下）
      - 关着 → `high`
    - 其余只读声明式 → `low`

    ## 为什么 `code` 可以是 `low`

    这是个需要想清楚的判断：

    `CODE_EXEC_ENABLED=true` 意味着 agent **已经能执行任意 Python 代码**
    （`python_exec` 就是干这个的）。所以再给它一个"代码工具"，
    **并没有扩大攻击面** —— 同一类能力，只是从"一次性"变成"可复用"。

    把代码工具卡在人工审批后面，收益是零（它已经能跑代码了），
    代价是用户每次想沉淀一个能力都要等人 —— 那"能力自增长"就是假的。

    反过来，`CODE_EXEC_ENABLED=false` 时 agent 本来就不能执行代码，
    这时代码工具**根本跑不起来**，所以判 `high`（等于拒绝）。
    """
    if proposal.kind in ("script", "code"):
        if proposal.kind == "code":
            return RISK_LOW if _code_exec_enabled() else RISK_HIGH
        return RISK_MEDIUM

    if proposal.method in WRITE_METHODS:
        return RISK_HIGH

    for hname in (proposal.headers or {}):
        low = str(hname).lower()
        if any(marker in low for marker in SECRET_HEADER_MARKERS):
            return RISK_HIGH

    return RISK_LOW


def _code_exec_enabled() -> bool:
    """代码执行开关（与 `python_exec` 用同一个）。"""
    try:
        from infra.config import config

        return bool(getattr(config, "code_exec_enabled", False))
    except Exception:
        return False


def can_auto_publish(proposal: ToolProposal) -> bool:
    """能不能不问人直接发布。

    只有"只读 + 无密钥 + 校验通过"才允许。
    这条线是 **agent 能不能自助** 与 **会不会出事** 的分界。
    """
    return proposal.risk == RISK_LOW and not proposal.blocked_reason


# ============================================================
# 存储
# ============================================================

class ToolProposalStore:
    """提案的持久化（一个提案一个 JSON 文件）。"""

    def __init__(self, base_path: Optional[Path] = None):
        if base_path is None:
            base_path = Path(__file__).parent.parent / "memory" / "tools"
        self.base = Path(base_path)
        self.proposals_dir = self.base / "proposals"
        self.logger = get_logger()

    def _ensure(self) -> None:
        try:
            self.proposals_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.logger.warning("ToolProposal", f"建目录失败 {self.proposals_dir}: {exc}")

    def _path(self, proposal_id: str) -> Path:
        return self.proposals_dir / f"{proposal_id}.json"

    def save(self, proposal: ToolProposal) -> Path:
        self._ensure()
        path = self._path(proposal.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.error("ToolProposal", f"保存提案失败 {path}: {exc}")
        return path

    def get(self, proposal_id: str) -> Optional[ToolProposal]:
        path = self._path(proposal_id)
        if not path.exists():
            return None
        try:
            return ToolProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            self.logger.warning("ToolProposal", f"读取提案失败 {path}: {exc}")
            return None

    def list(self, status: Optional[str] = None) -> List[ToolProposal]:
        if not self.proposals_dir.exists():
            return []
        out: List[ToolProposal] = []
        for path in sorted(self.proposals_dir.glob("*.json"), reverse=True):
            try:
                p = ToolProposal.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if status is None or p.status == status:
                out.append(p)
        out.sort(key=lambda p: p.created_at, reverse=True)
        return out

    def published(self) -> List[ToolProposal]:
        """所有已发布的提案（启动时用来恢复工具）。"""
        return self.list(STATUS_PUBLISHED)

    def delete(self, proposal_id: str) -> bool:
        path = self._path(proposal_id)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except Exception:
            return False


# ============================================================
# 构造工具实例
# ============================================================

def build_tool(proposal: ToolProposal):
    """按提案造出可注册的 Tool 实例。"""
    if proposal.kind == "http":
        from tools.declarative import DeclarativeHttpTool

        return DeclarativeHttpTool(proposal)
    if proposal.kind in ("script", "code"):
        from tools.script_tool import ScriptTool

        if proposal.kind == "code":
            # 内联代码要先落盘成脚本，再由 ScriptTool 跑（复用同一套沙箱）
            rel = materialize_code(proposal)
            proposal.script_path = rel
        return ScriptTool(proposal)
    raise ToolSpecError(f"不支持的 kind: {proposal.kind!r}")


def materialize_code(proposal: ToolProposal) -> str:
    """把内联代码写成工作区里的脚本文件，返回**相对路径**。

    为什么落盘而不是每次现传：脚本要能被用户查看/改，
    而且 `ScriptTool` 走的就是"读工作区脚本"这条路 —— 两边一致。

    目录用 `tools/`，避免和用户自己的脚本混在一起。
    """
    from tools.script_writer import workspace_dir

    rel = f"tools/{proposal.name}.py"
    target = workspace_dir() / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# 由 agent 创建的工具: {proposal.name}\n"
            f"# 用途: {proposal.description[:120]}\n"
            f"# 参数通过全局 PARAMS(dict) 注入\n\n"
        )
        target.write_text(header + (proposal.implementation or ""), encoding="utf-8")
    except Exception as exc:
        raise ToolSpecError(f"写入工具脚本失败: {exc}")
    return rel


def _register_with_owner(hub, tool, proposal: "ToolProposal") -> None:
    """把工具注册进 hub，并带上归属信息。

    **兼容旧 hub**：`register_python_tool` 现在多三个可选参数（owner/visibility），
    但 hub 是可插拔的（测试替身、第三方实现、MCP 桥接），不能假设它一定支持。
    所以先看签名，不支持就退回老调用方式。
    """
    supports_owner = True
    try:
        import inspect

        params = inspect.signature(hub.register_python_tool).parameters
        supports_owner = "owner_agent_id" in params
    except (TypeError, ValueError, AttributeError):
        supports_owner = False

    if supports_owner:
        hub.register_python_tool(
            tool,
            owner_agent_id=proposal.owner_agent_id,
            visibility=proposal.visibility,
        )
    else:
        hub.register_python_tool(tool)


def publish(proposal: ToolProposal, hub, *, reviewer: str = "auto") -> Tuple[bool, str]:
    """把提案注册进 ToolHub。

    Returns:
        (是否成功, 说明)
    """
    try:
        tool = build_tool(proposal)
    except Exception as exc:
        return False, f"构造工具失败：{exc}"

    try:
        _register_with_owner(hub, tool, proposal)
    except ValueError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"注册失败：{exc}"

    proposal.status = STATUS_PUBLISHED
    proposal.reviewed_by = reviewer
    proposal.blocked_reason = ""
    return True, f"已发布工具 {proposal.name}"


def unpublish(proposal: ToolProposal, hub) -> bool:
    """撤下工具（提案状态回到 rejected）。"""
    ok = hub.unregister_tool(proposal.name)
    if ok:
        proposal.status = STATUS_REJECTED
    return ok


def restore_published(hub) -> int:
    """启动时把已发布的工具重新注册进 hub。

    agent 造的工具必须**跨重启存活** —— 否则它每次重启就"忘掉"自己学会的能力，
    那"能力自增长"就是假的。

    Returns:
        成功恢复的数量
    """
    logger = get_logger()
    store = get_proposal_store()
    restored = 0
    for proposal in store.published():
        if proposal.name in hub.names():
            # 已经被内置/扫描器注册了，跳过
            continue
        try:
            tool = build_tool(proposal)
            _register_with_owner(hub, tool, proposal)
            restored += 1
        except Exception as exc:
            logger.warning(
                "ToolProposal",
                f"恢复工具 {proposal.name} 失败（已跳过）：{exc}",
            )
    if restored:
        logger.info("ToolProposal", f"恢复了 {restored} 个自制工具")
    return restored


# ============================================================
# 审批
# ============================================================

def approve(proposal_id: str, hub, reviewer: str = "human") -> Tuple[bool, str]:
    """人工批准一个待审提案。"""
    store = get_proposal_store()
    proposal = store.get(proposal_id)
    if proposal is None:
        return False, f"提案不存在: {proposal_id}"

    if proposal.status == STATUS_PUBLISHED:
        return True, "已经是发布状态"

    # 批准前再校验一次 —— 从提交到批准之间，环境可能变了
    errors, _ = validate_proposal(proposal, existing_names=hub.names())
    if errors:
        proposal.blocked_reason = "批准时校验失败：" + "; ".join(errors)
        store.save(proposal)
        return False, proposal.blocked_reason

    proposal.risk = assess_risk(proposal)
    ok, msg = publish(proposal, hub, reviewer=reviewer)
    store.save(proposal)
    return ok, msg


def reject(proposal_id: str, reviewer: str = "human", reason: str = "") -> bool:
    """人工拒绝一个提案。"""
    store = get_proposal_store()
    proposal = store.get(proposal_id)
    if proposal is None:
        return False
    proposal.status = STATUS_REJECTED
    proposal.reviewed_by = reviewer
    proposal.blocked_reason = reason or "人工拒绝"
    store.save(proposal)
    return True


# ============================================================
# 单例
# ============================================================

_store: Optional[ToolProposalStore] = None


def get_proposal_store() -> ToolProposalStore:
    global _store
    if _store is None:
        _store = ToolProposalStore()
    return _store


def reset_proposal_store() -> None:
    global _store
    _store = None
