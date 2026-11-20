"""元工具 —— **agent 用来给自己造工具的工具**。

## 这是整条自增长链路的人口

```
用户: "帮我查一下 XX 币的价格"           ← 现有工具做不到
  ↓
agent 想: "这需要一个查价格的接口"
  ↓
调 create_tool(name=..., url_template=..., params=[...])   ← 本模块
  ↓
静态校验 → 风险分级 → 真跑测试用例
  ↓
只读 + 无密钥 + 测试通过  →  **立刻发布**，本轮就能用
写操作 / 脚本 / 带密钥    →  进待审队列，人批准后才能用
  ↓
下次再问同一件事,planner 直接选新工具
```

## 为什么先校验再发布

一次失败的尝试不能让整轮任务崩掉 —— 所以 `create_tool` 会把
**具体哪里错了**返回给 agent，让它改一版再提交（**可迭代**）。
这正是"造工具"该有的样子：试、错、改。

## 为什么写操作必须人批

一个能自己造工具的 agent 等价于一个能改自己权限的进程。
只读接口自动发布是安全的（最坏情况是查了个公开数据）；
`POST/PATCH/DELETE` 和带密钥的调用会**改外部世界**，必须有人点头。

> 判据：**只读的可以自助，会改外部状态的必须人批。**
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema
from core.exception_policy import downgrade


class CreateToolTool(Tool):
    """造一个新工具（校验 + 测试 + 发布/待审）。"""

    name = "create_tool"
    description = (
        "创建一个新的可复用工具，注册后就能像内置工具一样反复调用。\n"
        "两种类型：\n"
        "  - kind=http（首选）：给一个 REST 接口的 URL 模板 + 参数表，"
        "不需要写代码。适合查天气、查汇率、查公告这类。\n"
        "  - kind=code（最灵活）：直接给 implementation 写 Python 代码，"
        "参数通过全局 PARAMS(dict) 读取，用 return 或 print 给结果。"
        "适合没有现成接口、需要自己算/自己拼请求的场景。\n"
        "  - kind=script：把一个已保存的脚本包装成工具（先用 write_script 存脚本）。\n"
        "只读的 HTTP 工具会**立即生效**；写操作或脚本需要人工批准。\n"
        "如果校验不通过，会返回具体原因，改一版再调用即可。"
    )
    required_flag = "tool_creation_enabled"

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="name", type="string", required=True,
                          description="工具名（小写字母开头，只含字母/数字/下划线，3~40 字符）"),
                ToolParam(name="description", type="string", required=True,
                          description="工具做什么（planner 靠这句决定要不要用它，要具体）"),
                ToolParam(name="kind", type="string", required=False, default="http",
                          enum=["http", "code", "script"], description="工具类型"),
                ToolParam(name="method", type="string", required=False, default="GET",
                          description="HTTP 方法（GET 只读；POST/PUT/PATCH/DELETE 需人批）"),
                ToolParam(name="url_template", type="string", required=False,
                          description="URL 模板，必须是 https://，参数用 {name} 占位"),
                ToolParam(name="params", type="array", required=False,
                          description=('参数表：[{"name":"city","type":"string",'
                                       '"description":"城市","required":true}]。'
                                       '也接受 {"city":"城市"} 这种简写。')),
                ToolParam(name="implementation", type="string", required=False,
                          description=('kind=code 时必填：Python 代码。'
                                       '用 PARAMS.get("x") 取参数，用 return 或 print 给结果。')),
                ToolParam(name="headers", type="object", required=False,
                          description="请求头（含密钥的工具不会自动发布）"),
                ToolParam(name="response_path", type="string", required=False,
                          description="从 JSON 响应里取哪一段，如 data.price / list[0].v"),
                ToolParam(name="script_path", type="string", required=False,
                          description="script 类：工作区里的脚本路径（write_script 返回的 path）"),
                ToolParam(name="rationale", type="string", required=False,
                          description="为什么需要这个工具（人审时看这句）"),
                ToolParam(name="test_cases", type="array", required=False,
                          description='测试用例：[{"params":{...},"expect_success":true}]，会真跑'),
            ],
            returns={
                "published": "是否已立即生效",
                "name": "工具名",
                "risk": "风险等级",
                "next": "下一步该做什么",
            },
        )

    async def execute(self, **kwargs) -> ToolResult:
        from tools.hub import get_tool_hub
        from tools.proposal import (
            ToolProposal,
            assess_risk,
            can_auto_publish,
            get_proposal_store,
            publish,
            validate_proposal,
        )

        hub = get_tool_hub()

        proposal = ToolProposal(
            name=str(kwargs.get("name") or ""),
            description=str(kwargs.get("description") or ""),
            kind=str(kwargs.get("kind") or "http"),
            method=str(kwargs.get("method") or "GET"),
            url_template=str(kwargs.get("url_template") or ""),
            headers=kwargs.get("headers") or {},
            params=kwargs.get("params") or kwargs.get("parameters") or [],
            implementation=str(kwargs.get("implementation") or ""),
            response_path=str(kwargs.get("response_path") or ""),
            script_path=str(kwargs.get("script_path") or ""),
            rationale=str(kwargs.get("rationale") or ""),
            test_cases=kwargs.get("test_cases") or [],
            source="agent",
        )

        # --- 1. 静态校验 ---
        errors, warnings = validate_proposal(proposal, existing_names=hub.names())
        proposal.validation = {"errors": errors, "warnings": warnings}

        if errors:
            # **把错误原样返回,让 agent 自己改** —— 这是可迭代的关键
            return ToolResult(
                success=False,
                error=(
                    "工具提案没通过校验，请修正后重新调用：\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + ("\n\n提醒：\n" + "\n".join(f"- {w}" for w in warnings) if warnings else "")
                ),
                data={
                    "name": proposal.name,
                    "errors": errors,
                    "warnings": warnings,
                },
            )

        # --- 2. 风险分级 ---
        proposal.risk = assess_risk(proposal)
        proposal.side_effect = "write" if proposal.method in (
            "POST", "PUT", "PATCH", "DELETE"
        ) else "read"

        store = get_proposal_store()

        # --- 3. 测试用例(可选) ---
        test_report = await self._run_tests(proposal)
        if test_report.get("failed"):
            # 测试没过 → 存成待审(带失败原因),不发布
            proposal.status = "pending"
            proposal.blocked_reason = "测试用例未通过"
            proposal.validation["tests"] = test_report
            store.save(proposal)
            return ToolResult(
                success=False,
                error=(
                    "测试用例没跑通，已存为待审但不发布：\n"
                    + "\n".join(f"- {f}" for f in test_report["failed"])
                    + "\n\n请修正 spec 后重新提交。"
                ),
                data={"name": proposal.name, "tests": test_report},
            )
        if test_report.get("ran"):
            proposal.validation["tests"] = test_report

        # --- 4. 发布 or 待审 ---
        if can_auto_publish(proposal):
            ok, msg = publish(proposal, hub, reviewer="auto")
            store.save(proposal)
            if not ok:
                return ToolResult(success=False, error=msg, data={"name": proposal.name})
            self.logger.info("CreateTool", f"自动发布工具: {proposal.name}")
            self._audit_tool_event(
                "tool_published", proposal, extra={"reviewer": "auto"}
            )
            return ToolResult(
                success=True,
                data={
                    "published": True,
                    "name": proposal.name,
                    "risk": proposal.risk,
                    "warnings": warnings,
                    "tests": test_report or None,
                    "visibility": proposal.visibility,
                    "next": "工具已生效，现在可以直接调用它完成任务。",
                },
            )

        # 需要人工批准
        proposal.status = "pending"
        proposal.blocked_reason = (
            f"风险等级 {proposal.risk}（"
            + ("脚本类" if proposal.kind == "script"
               else f"{proposal.method} 会改外部状态")
            + "），需要人工批准"
        )
        store.save(proposal)
        self.logger.info("CreateTool", f"待审工具: {proposal.name} ({proposal.risk})")
        self._audit_tool_event("tool_proposed", proposal, extra={"reviewer": ""})
        return ToolResult(
            success=True,
            data={
                "published": False,
                "name": proposal.name,
                "risk": proposal.risk,
                "proposal_id": proposal.id,
                "warnings": warnings,
                "visibility": proposal.visibility,
                "next": (
                    "已提交待审。这只工具需要人工批准（会改外部状态或要执行代码），"
                    "批准前 planner 看不到它。告诉用户需要批准，"
                    "或者先用只读的方式完成任务。"
                ),
            },
        )

    def _audit_tool_event(self, event: str, proposal, *, extra: dict | None = None) -> None:
        """工具提案/发布写审计（写失败不影响业务）。"""
        try:
            from core.audit import append_jsonl, current_identity, event as mk_event

            payload = mk_event(
                event,
                **current_identity(),
                name=getattr(proposal, "name", ""),
                proposal_id=getattr(proposal, "id", ""),
                kind=getattr(proposal, "kind", ""),
                risk=getattr(proposal, "risk", ""),
                owner_agent_id=getattr(proposal, "owner_agent_id", ""),
                visibility=getattr(proposal, "visibility", ""),
                **(extra or {}),
            )
            append_jsonl("tools.jsonl", payload)
        except Exception as exc:
            downgrade(exc, where='tools/meta_tools.py')
    async def _run_tests(self, proposal) -> Dict[str, Any]:
        """跑提案里带的测试用例。返回 {ran, passed, failed}。"""
        cases = proposal.test_cases or []
        if not cases:
            return {"ran": 0, "passed": 0, "failed": []}

        from tools.proposal import build_tool

        report: Dict[str, Any] = {"ran": 0, "passed": 0, "failed": [], "details": []}
        try:
            tool = build_tool(proposal)
        except Exception as exc:
            report["failed"].append(f"无法构造工具：{exc}")
            return report

        for idx, case in enumerate(cases, 1):
            if not isinstance(case, dict):
                continue
            params = case.get("params") or {}
            expect_success = bool(case.get("expect_success", True))
            report["ran"] += 1
            try:
                result = await tool.execute(**params)
            except Exception as exc:
                report["failed"].append(f"用例 {idx} 抛异常：{exc}")
                continue

            if result.success == expect_success:
                report["passed"] += 1
            else:
                detail = (result.error or "")[:160]
                report["failed"].append(
                    f"用例 {idx} 失败（期望 success={expect_success}，实际 {result.success}）"
                    + (f"：{detail}" if detail else "")
                )
            report["details"].append({
                "case": idx,
                "success": result.success,
                "error": (result.error or "")[:200],
            })

        # 测试完把临时客户端关掉
        try:
            await tool.aclose()
        except Exception as exc:
            downgrade(exc, where='tools/meta_tools.py')
        return report


class ListCreatedToolsTool(Tool):
    """列出 agent 自己造过的工具(含待审)。"""

    name = "list_created_tools"
    description = (
        "列出所有由 agent 创建的工具提案：已发布的、待审的、被拒的。"
        "想知道「我现在有哪些自制工具」或「哪个还在等批准」时用它。"
    )
    required_flag = "tool_creation_enabled"

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="status", type="string", required=False,
                          enum=["pending", "published", "rejected"],
                          description="只看某个状态（不给则全列）"),
            ],
            returns={"tools": "[{name, status, risk, kind, description}]"},
        )

    async def execute(self, status: Optional[str] = None) -> ToolResult:
        from tools.proposal import get_proposal_store

        store = get_proposal_store()
        items = store.list(status or None)
        return ToolResult(
            success=True,
            data={
                "count": len(items),
                "tools": [
                    {
                        "name": p.name,
                        "status": p.status,
                        "risk": p.risk,
                        "kind": p.kind,
                        "description": p.description,
                        "blocked_reason": p.blocked_reason,
                    }
                    for p in items
                ],
            },
        )


# ============================================================
# 自诊断：让 agent 自己说出来"我缺什么"、"哪个工具坏了"
# ============================================================

class ReportCapabilityGapTool(Tool):
    """如实申报"我需要一个能力，但现在没有"。"""

    name = "report_capability_gap"
    description = (
        "当你**因为缺少某种能力**而无法完成用户的任务时，用这个工具如实申报。\n"
        "典型场景：任务需要一个接口/库/程序，但工具表里没有，你也造不出来。\n"
        "**不要**用它代替做事；也不要默默降级交付（比如把一段视频改名成'成片'）。\n"
        "宁可如实说'缺 X'，也不要给用户一个假的东西。"
    )
    required_flag = "tool_creation_enabled"

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="need", type="string", required=True,
                          description="缺的是什么能力（一句话说清，例如「拼接视频」）"),
                ToolParam(name="tried", type="string", required=False,
                          description="你尝试过什么"),
                ToolParam(name="blocked_by", type="string", required=False,
                          description="被什么挡住了（例如「没有 ffmpeg」、「接口未授权」）"),
            ],
            returns={"recorded": "是否已记录", "suggestion": "下一步建议"},
        )

    async def execute(self, need: str = "", tried: str = "",
                      blocked_by: str = "") -> ToolResult:
        from core.tool_health import get_health_store

        text = (need or "").strip()
        if not text:
            return ToolResult(success=False, error="need 不能为空：说明你缺什么能力")

        store = get_health_store()
        store.record_gap(text, tried=tried or "", blocked_by=blocked_by or "")
        self.logger.info("CapabilityGap", f"申报能力缺口: {text[:80]}")

        return ToolResult(
            success=True,
            data={
                "recorded": True,
                "need": text,
                "suggestion": (
                    "已记录到能力缺口清单。如果这个能力可以用一段代码或一个 REST 接口实现，"
                    "现在就用 create_tool 造出来；如果造不出来（缺密钥/缺可执行程序/缺授权），"
                    "就如实告诉用户缺什么，不要用近似的东西冒充交付物。"
                ),
            },
        )


class ToolHealthTool(Tool):
    """查工具健康度（哪些工具最近老失败）。"""

    name = "tool_health"
    description = (
        "查看各工具的真实调用记录：成功/失败次数、失败率、最近的错误。\n"
        "想知道「某个工具是不是坏了」、「该不该换个做法」时用它。"
    )

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[ToolParam(
                name="only_unhealthy", type="boolean", required=False, default=False,
                description="只看不健康的工具",
            )],
            returns={"tools": "[{tool, total, failed, fail_rate, reason, recent_errors}]"},
        )

    async def execute(self, only_unhealthy: bool = False) -> ToolResult:
        from core.tool_health import get_health_store

        store = get_health_store()
        items = store.unhealthy() if only_unhealthy else store.snapshot()
        return ToolResult(
            success=True,
            data={
                "count": len(items),
                "tools": [h.to_dict() for h in items],
                "gaps": store.list_gaps(limit=10),
            },
        )


class RepairToolTool(Tool):
    """让 agent 主动修一个自己造的工具。"""

    name = "repair_tool"
    description = (
        "修理一个**自己造的**、反复失败的工具：会读真实失败记录、生成修正版、"
        "校验并跑测试，通过后替换旧版。\n"
        "只能修用 create_tool 造的工具；内置工具修不了（源码不归 agent 管），"
        "那种情况请换做法或用 report_capability_gap 申报。"
    )
    required_flag = "tool_repair_enabled"

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[ToolParam(
                name="tool_name", type="string", required=True,
                description="要修的工具名",
            )],
            returns={"published": "是否已发布修正版", "reason": "结果说明"},
        )

    async def execute(self, tool_name: str = "") -> ToolResult:
        from core.tool_repair import repair_tool

        name = (tool_name or "").strip()
        if not name:
            return ToolResult(success=False, error="tool_name 不能为空")

        r = await repair_tool(name)
        return ToolResult(
            success=bool(r.published),
            data=r.to_dict(),
            error="" if r.published else (r.reason or "未能修复"),
        )
