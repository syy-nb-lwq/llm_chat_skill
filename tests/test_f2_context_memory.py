"""F2 验收测试 —— 上下文按需投喂 + 记忆分层。

对应 `docs/18` §8 的 F2：

| 用例 | 对应工作项 | 断言的事实 |
|---|---|---|
| 上下文 | F2-2 | 每个块都带来源与理由；预算裁剪有记录；信息多少会改变注入量 |
| 记忆 | F2-3 | 子智能体不能直接写用户长期记忆；只能写自己的私有记忆；候选要主智能体裁决 |
| 作用域 | F2-4 | 跨 scope / 跨 agent 不可见 |
| 受限规划 | F2-1 | planner 的工具表按角色裁剪；上下文块替代全量历史 |
"""
import pytest

from core.agentkit.blackboard import Blackboard
from core.agentkit.context import (
    SOURCE_BRIEF,
    SOURCE_MEMORY,
    SOURCE_TOOL,
    ContextAssembler,
    ContextBlock,
)
from core.agentkit.memory import MemoryFacade, MemoryPermissionError
from core.agentkit.roles import default_roles
from core.exec_context import reset_exec_context, set_exec_context


@pytest.fixture
def db(tmp_path):
    from core.memory_db import MemoryDB

    return MemoryDB(db_path=tmp_path / "memories.db")


@pytest.fixture
def facade(db):
    return MemoryFacade(db=db)


# ============================================================
# 记忆分层
# ============================================================

def test_subagent_cannot_commit_user_memory(facade):
    """子智能体不能把候选直接落成用户长期记忆 —— 那是主智能体的权限。"""
    tokens = set_exec_context(session_id="s1", agent_id="sub_x")
    try:
        proposal = facade.propose_user_memory(
            "用户喜欢简洁的中文日报", reason="任务中发现"
        )
        assert proposal and proposal["status"] == "pending"
        with pytest.raises(MemoryPermissionError):
            facade.commit_user_memory(proposal["id"])
    finally:
        reset_exec_context(tokens)


def test_main_agent_can_commit_user_memory(facade):
    """主智能体裁决后，候选变成用户长期记忆。"""
    tokens = set_exec_context(session_id="s1", agent_id="sub_x")
    try:
        proposal = facade.propose_user_memory(
            "用户喜欢简洁的中文日报"
        )
    finally:
        reset_exec_context(tokens)

    tokens = set_exec_context(session_id="s1", agent_id="main")
    try:
        entry = facade.commit_user_memory(proposal["id"])
        assert entry is not None
        assert entry.scope == "user"
        assert facade.db.get_proposal(proposal["id"])["status"] == "accepted"
    finally:
        reset_exec_context(tokens)


def test_subagent_can_only_write_its_own_private_memory(facade):
    tokens = set_exec_context(session_id="s1", agent_id="sub_a")
    try:
        facade.write_agent("我踩过的坑：时间格式是毫秒")
        with pytest.raises(MemoryPermissionError):
            facade.write_agent("写别人的", agent_id="sub_b")
    finally:
        reset_exec_context(tokens)


def test_agent_memory_is_invisible_to_user_scope(facade):
    """跨作用域不可见：智能体私有记忆不会混进用户长期记忆的召回。"""
    tokens = set_exec_context(session_id="s1", agent_id="sub_a")
    try:
        facade.write_agent("私有经验：某接口要带签名")
        facade.write_agent("用户偏好：中文", type="preference")
    finally:
        reset_exec_context(tokens)

    user_scope = facade.read(
        scopes=["user"], query="私有经验", limit=10
    )
    assert not any("私有经验" in e.content for e in user_scope)

    agent_scope = facade.read_agent_memory(agent_id="sub_a")
    assert any("私有经验" in e.content for e in agent_scope)


def test_agent_memory_of_other_agent_is_invisible(facade):
    tokens = set_exec_context(session_id="s1", agent_id="sub_a")
    try:
        facade.write_agent("A 的经验")
    finally:
        reset_exec_context(tokens)

    mine = facade.read_agent_memory(agent_id="sub_a")
    other = facade.read_agent_memory(agent_id="sub_b")
    assert any("A 的经验" in e.content for e in mine)
    assert not any("A 的经验" in e.content for e in other)


def test_proposals_are_listable(facade):
    tokens = set_exec_context(agent_id="sub_a")
    try:
        facade.propose_user_memory("候选一")
        facade.propose_user_memory("候选二")
    finally:
        reset_exec_context(tokens)
    pending = facade.list_proposals(status="pending")
    assert len(pending) == 2


# ============================================================
# 黑板
# ============================================================

def test_blackboard_write_read_and_pull_by_key():
    board = Blackboard()
    board.write("policy_clauses", "第 3 条：不得含有未备案内容", producer="t1")
    board.write("cases", "案例 A / 案例 B", producer="t2")

    assert board.read("policy_clauses").startswith("第 3 条")
    # 下游只拉自己声明要的键
    pulled = board.read_many(["policy_clauses"])
    assert list(pulled.keys()) == ["policy_clauses"]
    assert "cases" not in pulled


def test_blackboard_truncates_huge_values():
    board = Blackboard(max_value_chars=100)
    entry = board.write("big", "x" * 5000)
    assert entry.truncated
    assert len(entry.value) < 200


# ============================================================
# 上下文装配
# ============================================================

def test_every_block_has_source_and_why():
    """**没有理由的上下文不允许注入** —— 这是"够用且清晰"的可检验形式。"""
    bundle = ContextAssembler().assemble([
        ContextBlock(source=SOURCE_MEMORY, why="上次做过类似的", content="x" * 10),
    ])
    for block in bundle.blocks:
        assert block.source
        assert block.why


def test_budget_trims_and_records_what_was_dropped():
    bundle = ContextAssembler(total_limit=200).assemble([
        ContextBlock(source=SOURCE_TOOL, why="工具", content="t" * 500, priority=4),
        ContextBlock(source=SOURCE_BRIEF, why="目标", content="b" * 500, priority=1),
    ])
    assert bundle.dropped, "裁掉了什么必须记下来"
    assert any(d["source"] == SOURCE_TOOL for d in bundle.dropped)
    # 目标与验收**永不裁** —— 所以总量可能仍然超限，这是设计上的取舍
    assert any(b.source == SOURCE_BRIEF for b in bundle.blocks)
    assert bundle.total_chars == 500


def test_context_builder_produces_smaller_injection_when_info_is_scarce(tmp_path, monkeypatch):
    """同一个子任务，信息多少会改变注入量 —— 这就是"按需"。"""
    from core.agentkit.sources import ContextBuilder
    from core.memory_db import MemoryDB

    db = MemoryDB(db_path=tmp_path / "memories.db")
    facade = MemoryFacade(db=db)
    # 智能体私有记忆：只有它自己能召回（本地单用户下按 agent 维度隔离）
    db.add(type="context", content="用户偏好中文简洁日报",
           scope="agent", agent_id="sub_a")

    builder = ContextBuilder(memory_facade=facade)
    role = next(r for r in default_roles() if r.role_id == "researcher")

    rich = builder.build(
        goal="整理本周日报", role=role, allowed_tools=["web_search"], agent_id="sub_a",
    )
    poor = builder.build(
        goal="整理本周日报", role=role,  # 另一个智能体 → 没有它的私有记忆
        allowed_tools=["web_search"], agent_id="sub_b",
    )
    assert rich.total_chars > poor.total_chars
    assert any(b.source == SOURCE_MEMORY for b in rich.blocks)
    assert not any(b.source == SOURCE_MEMORY for b in poor.blocks)


def test_context_builder_writes_injection_log(tmp_path, monkeypatch):
    """注入日志：复盘时第一件事就是看它。"""
    import core.audit as audit_mod
    from core.agentkit.sources import ContextBuilder

    lines = []

    def fake_append(filename, payload):
        lines.append((filename, payload))
        return True

    monkeypatch.setattr(audit_mod, "append_jsonl", fake_append)
    # sources 里是延迟 import 的，所以要打到模块属性上
    builder = ContextBuilder(memory_facade=MemoryFacade(db=None))
    builder.build(goal="测试目标", allowed_tools=[])
    assert lines, "装配必须写注入日志"
    assert lines[0][0] == "context.jsonl"
    assert lines[0][1]["event"] == "context_injected"


# ============================================================
# 受限规划（工具表裁剪 + 上下文块替代全量历史）
# ============================================================

def _manager():
    from agents.manager import ManagerAgent
    from infra.logger import get_logger

    mgr = ManagerAgent.__new__(ManagerAgent)
    mgr.logger = get_logger()
    mgr.skill_store = type("S", (), {"list_all": staticmethod(list), "match": staticmethod(lambda q, k: [])})()
    return mgr


def test_allowed_tools_narrows_the_tool_table(monkeypatch):
    """角色白名单 → planner 只看得到这些工具。"""
    import tools.hub as hub_mod
    from agents.manager import _plan_allowed_tools

    fake = type("H", (), {"names": staticmethod(lambda: ["web_search", "web_fetch", "python_exec"])})()
    monkeypatch.setattr(hub_mod, "get_tool_hub", lambda: fake)

    mgr = _manager()
    token = _plan_allowed_tools.set(["web_search"])
    try:
        assert mgr._available_tool_names() == ["web_search"]
    finally:
        _plan_allowed_tools.reset(token)

    token = _plan_allowed_tools.set(None)
    try:
        assert set(mgr._available_tool_names()) == {
            "web_search", "web_fetch", "python_exec"
        }
    finally:
        _plan_allowed_tools.reset(token)


def test_context_blocks_replace_full_history(monkeypatch):
    """给了上下文块 → 提示词里用块，不再是主对话全量历史。"""
    from agents.manager import _plan_context_blocks

    class _Ctx:
        def to_llm_messages(self, **kwargs):
            return [{"role": "user", "content": "主对话里的历史内容"}]

        def summary_block(self):
            return "主对话摘要"

        def system_block(self):
            return "系统块"

        def __len__(self):
            return 1

    mgr = _manager()
    blocks = [{
        "source": "brief",
        "why": "这是你这一轮要完成的事",
        "content": "目标: 查监管要求",
        "chars": 20,
        "priority": 1,
    }]
    token = _plan_context_blocks.set(blocks)
    try:
        prompt = mgr._build_user_prompt("查监管要求", _Ctx())
    finally:
        _plan_context_blocks.reset(token)

    assert "查监管要求" in prompt
    assert "主对话里的历史内容" not in prompt, "不该再把全量历史塞进去"
    assert "brief" in prompt and "这是你这一轮要完成的事" in prompt


# ============================================================
# 子智能体岗位
# ============================================================

def test_unknown_role_falls_back_to_generalist():
    from core.subagents import SubAgentPool

    pool = SubAgentPool()
    role = pool._resolve_role("不存在的岗位")
    assert role.role_id == "generalist"


def test_named_role_is_resolved():
    from core.subagents import SubAgentPool

    pool = SubAgentPool()
    role = pool._resolve_role("researcher")
    assert role.role_id == "researcher"
    assert "web_search" in role.tools.allow


def test_allowed_tools_excludes_blocked_and_unregistered(monkeypatch):
    from core.subagents import SubAgentPool

    pool = SubAgentPool()
    monkeypatch.setattr(
        pool, "_all_tool_names", lambda: ["web_search", "web_fetch", "run_subagents"]
    )
    role = next(r for r in default_roles() if r.role_id == "researcher")
    allowed = pool._allowed_tools(role, {"run_subagents"})
    assert "web_search" in allowed
    assert "run_subagents" not in allowed
    assert "python_exec" not in allowed, "角色白名单外的工具不该给"
