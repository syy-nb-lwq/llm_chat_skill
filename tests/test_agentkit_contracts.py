"""F1 契约层测试 —— 12 条规格必须**机器可校验**。

这些测试不调 LLM、不读磁盘，是纯数据结构的校验：
主智能体与子智能体之间的约定（验收标准、预算、授权、依赖）一旦定下，
就必须能被自动挡住不合法的情况。
"""
import pytest

from core.agentkit.brief import (
    STATUS_OK,
    BriefValidator,
    DelegationPlan,
    ReportPacket,
    TaskBrief,
)
from core.agentkit.context import (
    SOURCE_BRIEF,
    SOURCE_MEMORY,
    SOURCE_TOOL,
    ContextAssembler,
    ContextBlock,
)
from core.agentkit.identity import AgentIdentity
from core.agentkit.roles import (
    CAPABILITIES,
    Budget,
    Capability,
    RoleProfile,
    RoleRegistry,
    ToolGrant,
    default_roles,
)


# ============================================================
# 身份
# ============================================================

def test_identity_autogenerates_ids():
    ident = AgentIdentity(role_id="researcher")
    assert ident.agent_id.startswith("agent_")
    assert ident.execution_id.startswith("exec_")
    assert ident.root_agent_id == ident.agent_id
    assert ident.depth == 0


def test_identity_child_increments_depth_and_keeps_parent():
    parent = AgentIdentity(role_id="main", session_id="s1")
    child = parent.child("researcher", "研究员")
    assert child.depth == parent.depth + 1
    assert child.parent_agent_id == parent.agent_id
    assert child.root_agent_id == parent.agent_id
    assert child.agent_id != parent.agent_id
    assert child.execution_id != parent.execution_id
    assert child.session_id == "s1"


def test_identity_roundtrip():
    ident = AgentIdentity(role_id="r", session_id="s", depth=2)
    again = AgentIdentity.from_dict(ident.to_dict())
    assert again.agent_id == ident.agent_id
    assert again.depth == 2


def test_identity_from_execution_context():
    from core.identity import IdentityContext

    ctx = IdentityContext(
        session_id="s9", execution_id="e9",
        parent_execution_id="e8",
    )
    ident = AgentIdentity.from_execution_context(ctx)
    assert ident.session_id == "s9"
    assert ident.execution_id == "e9"
    assert ident.parent_agent_id == "e8"


# ============================================================
# 角色画像
# ============================================================

def test_default_roles_are_valid():
    roles = default_roles()
    assert roles, "至少要有预置角色"
    for role in roles:
        problems = role.validate()
        assert not problems, f"角色 {role.role_id} 不合法: {problems}"


def test_role_validate_catches_bad_capability():
    role = RoleProfile(
        role_id="broken", capabilities=["不存在的能力"],
        tools=ToolGrant(allow=["web_search"]), workspace="ws",
    )
    problems = role.validate()
    assert any("未知能力项" in p for p in problems)


def test_role_validate_catches_bad_scope_and_skill_format():
    role = RoleProfile(
        role_id="broken2",
        capabilities=[Capability.RESOURCE_ACQUIRE],
        tools=ToolGrant(allow=["web_search"]),
        skills=["daily_report"],          # 少了 @version
        memory_scopes=["private"],        # 非法作用域
        workspace="ws",
    )
    problems = role.validate()
    assert any("技能引用格式" in p for p in problems)
    assert any("非法记忆作用域" in p for p in problems)


def test_tool_grant_allow_deny():
    grant = ToolGrant(allow=["web_search", "web_fetch"], deny=["web_fetch"])
    assert grant.allows("web_search")
    assert not grant.allows("web_fetch")
    assert not grant.allows("python_exec")


def test_role_registry_rejects_duplicate():
    reg = RoleRegistry()
    reg.register(RoleProfile(role_id="a", capabilities=[Capability.BUDGET],
                              tools=ToolGrant(allow=["x"]), workspace="ws"))
    with pytest.raises(ValueError):
        reg.register(RoleProfile(role_id="a", capabilities=[Capability.BUDGET],
                                  tools=ToolGrant(allow=["x"]), workspace="ws"))


def test_capability_matrix_covers_all_capabilities():
    from core.agentkit.roles import ROLE_CAPABILITY_MATRIX

    for role, row in ROLE_CAPABILITY_MATRIX.items():
        missing = set(CAPABILITIES) - set(row)
        assert not missing, f"角色 {role} 的能力矩阵缺: {missing}"


# ============================================================
# 预算
# ============================================================

def test_budget_fits_within():
    parent = Budget(max_rounds=4, max_seconds=120, max_tool_calls=20)
    ok, reason = Budget(max_rounds=4, max_seconds=100, max_tool_calls=20).fits_within(parent)
    assert ok, reason

    ok, reason = Budget(max_rounds=5).fits_within(parent)
    assert not ok and "max_rounds" in reason

    ok, reason = Budget(max_seconds=121).fits_within(parent)
    assert not ok and "max_seconds" in reason


def test_budget_zero_tokens_means_unlimited():
    parent = Budget(max_tokens=0)
    ok, _ = Budget(max_tokens=99999).fits_within(parent)
    assert ok, "max_tokens=0 表示不限，不该判为超父"


# ============================================================
# 简报 / 报告包 / 编排图
# ============================================================

def _brief(**kw):
    data = dict(
        task_id="t1",
        goal="查 2026 年微短剧监管要求",
        role_id="researcher",
        acceptance_criteria=["列出关键条款并附来源链接"],
        tools=["web_search", "web_fetch"],
        budget=Budget(max_rounds=2, max_seconds=60, max_tool_calls=5),
    )
    data.update(kw)
    return TaskBrief(**data)


def test_valid_brief_passes_validation():
    problems = BriefValidator.validate_task(_brief())
    assert not problems, problems


def test_brief_without_acceptance_criteria_is_rejected():
    problems = BriefValidator.validate_task(_brief(acceptance_criteria=[]))
    assert any("缺少验收标准" in p for p in problems)


def test_brief_without_goal_is_rejected():
    problems = BriefValidator.validate_task(_brief(goal="  "))
    assert any("goal 为空" in p for p in problems)


def test_brief_with_tool_outside_role_is_rejected():
    role = RoleProfile(
        role_id="researcher",
        capabilities=[Capability.RESOURCE_ACQUIRE],
        tools=ToolGrant(allow=["web_search", "web_fetch"]),
        workspace="ws",
    )
    problems = BriefValidator.validate_task(
        _brief(tools=["web_search", "python_exec"]), role=role
    )
    assert any("越权工具" in p and "python_exec" in p for p in problems)


def test_brief_budget_over_parent_is_rejected():
    parent = Budget(max_rounds=2, max_seconds=60, max_tool_calls=5)
    problems = BriefValidator.validate_task(
        _brief(budget=Budget(max_rounds=9, max_seconds=60, max_tool_calls=5)),
        parent_budget=parent,
    )
    assert any("预算超父" in p for p in problems)


def test_plan_detects_dangling_dependency():
    plan = DelegationPlan(tasks=[_brief(depends_on=["t404"])])
    problems = plan.validate()
    assert any("依赖不存在的任务" in p for p in problems)


def test_plan_detects_dependency_cycle():
    a = _brief(task_id="a", depends_on=["b"])
    b = _brief(task_id="b", depends_on=["a"])
    plan = DelegationPlan(tasks=[a, b])
    problems = plan.validate()
    assert any("依赖成环" in p for p in problems)


def test_plan_edges_derived_from_depends_on():
    a = _brief(task_id="a")
    b = _brief(task_id="b", depends_on=["a"])
    plan = DelegationPlan(tasks=[a, b])
    assert ("a", "b") in plan.edges()
    assert not plan.validate()


def test_report_ok_requires_acceptance_pass():
    """状态 ok 但自检没过 → 不算成功（docs/17 P1-6 的洞）。"""
    report = ReportPacket(task_id="t1", status=STATUS_OK, acceptance_check={"passed": False})
    assert report.ok is False

    report2 = ReportPacket(task_id="t1", status=STATUS_OK, acceptance_check={"passed": True})
    assert report2.ok is True

    report3 = ReportPacket(task_id="t1", status="failed", acceptance_check={"passed": True})
    assert report3.ok is False


# ============================================================
# 上下文装配
# ============================================================

def test_block_chars_autofilled():
    block = ContextBlock(source=SOURCE_MEMORY, why="上次做过类似的", content="x" * 100)
    assert block.chars == 100


def test_block_limit_trims_and_records():
    asm = ContextAssembler()
    block = ContextBlock(source=SOURCE_MEMORY, why="相关记忆", content="x" * 5000)
    bundle = asm.assemble([block])
    assert bundle.total_chars <= 2000
    assert any(d["reason"] == "block_limit" for d in bundle.dropped)


def test_total_limit_trims_largest_priority_first():
    asm = ContextAssembler(total_limit=3000)
    blocks = [
        ContextBlock(source=SOURCE_BRIEF, why="目标", content="b" * 1000, priority=1),
        ContextBlock(source=SOURCE_TOOL, why="工具", content="t" * 2000, priority=4),
        ContextBlock(source=SOURCE_MEMORY, why="记忆", content="m" * 2000, priority=3),
    ]
    bundle = asm.assemble(blocks)
    assert bundle.total_chars <= 3000
    dropped_sources = {d["source"] for d in bundle.dropped}
    assert SOURCE_TOOL in dropped_sources, "优先级大的应该先被裁"
    # priority<=1 的目标块不能被裁
    assert any(b.source == SOURCE_BRIEF for b in bundle.blocks)


def test_never_trim_target_block():
    asm = ContextAssembler(total_limit=100)
    blocks = [
        ContextBlock(source=SOURCE_BRIEF, why="目标与验收", content="b" * 500, priority=1),
    ]
    bundle = asm.assemble(blocks)
    assert bundle.blocks, "目标与验收永不裁"
    assert bundle.total_chars == 500


def test_bundle_summary_and_to_dict():
    asm = ContextAssembler()
    bundle = asm.assemble([
        ContextBlock(source=SOURCE_MEMORY, why="相关", content="x" * 10),
    ])
    assert "上下文包" in bundle.summary()
    data = bundle.to_dict()
    assert data["blocks"][0]["source"] == SOURCE_MEMORY
    assert data["total_chars"] == 10


# ===== 接线契约：包声明的“已接主链”集合必须与实践一致 =====


def test_declared_wiring_matches_reality():
    """`core/agentkit/__init__.py` 的接线表是唯一真相，不能与实践漂移。

    历史上这里写着"不接主链"，但 subagents 早就 import 了 roles/sources ——
    声明和实践各说各话，读者无法判断这是死代码还是地基。
    这条测试把两者锁在一起：改接线必须改声明，反之亦然。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    # 主链 = 会被真实请求走到的代码（不是 tests）
    producers = [
        root / "core" / "subagents.py",
        root / "tools" / "subagent_tool.py",
    ]
    actual = set()
    for path in producers:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"core\.agentkit\.(\w+)", text):
            actual.add(m.group(1))

    # 声明的“主链直接 import”模块（与 __init__.py 的表格一致）
    # context / memory 是经 sources 间接使用，不算直接引用
    declared = {"roles", "sources"}

    assert actual == declared, (
        f"接线实践 {sorted(actual)} 与包声明 {sorted(declared)} 不一致；"
        "改主链 import 时必须同步更新 core/agentkit/__init__.py 的表格"
    )

    # 契约层模块不得被主链直接 import
    contract_only = {"identity", "brief", "blackboard", "messaging"}
    assert not (actual & contract_only), f"契约层模块被主链直接引用：{actual & contract_only}"
