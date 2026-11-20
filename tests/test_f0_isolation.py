"""F0 地基验收测试 —— 四个止血点，一条都不能回退。

对应 `docs/18` §8 的 F0：

| 用例 | 对应工作项 | 断言的事实 |
|---|---|---|
| 隔离 | F0-1 | 并发执行不串味；脚本/产物按会话分区 |
| 归属 | F0-2 | 自造工具带 owner，能按 owner 一键禁用 |
| 召回 | F0-3 | 技能注入是两阶段，提示词不随技能数线性膨胀 |
| 作用域 | F0-4 | 记忆按 scope 过滤（scope 不再是死字段） |
"""
import asyncio

import pytest

from core.exec_context import (
    ExecContext,
    get_exec_context,
    reset_exec_context,
    set_exec_context,
)


# ============================================================
# F0-1 执行隔离
# ============================================================

@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录（别污染真实 memory/workspace）。"""
    import tools.python_exec as pe
    import tools.script_writer as sw

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pe, "_workspace_dir", lambda: ws)
    monkeypatch.setattr(sw, "workspace_dir", lambda: ws)
    yield ws


def test_scope_key_differs_per_session():
    """不同会话 → 不同作用域（本地单用户，无用户维度）。"""
    a = ExecContext(session_id="s1").scope_key
    b = ExecContext(session_id="s1").scope_key
    c = ExecContext(session_id="s2").scope_key
    assert a == b, "同一会话的作用域要稳定"
    assert a != c, "不同会话不能共用作用域"
    assert ExecContext().scope_key == "default", "无上下文时保持单机默认"


def test_exec_context_set_and_reset():
    tokens = set_exec_context(session_id="s9", agent_id="sub_x")
    try:
        ctx = get_exec_context()
        assert ctx.session_id == "s9"
        assert ctx.agent_id == "sub_x"
    finally:
        reset_exec_context(tokens)
    assert get_exec_context().session_id == ""


def test_run_dir_is_scoped_per_session():
    """执行目录按会话分区 —— 并发时不会抢同一个 main.py。"""
    from tools.python_exec import _run_dir

    set_exec_context(session_id="s1")
    d1 = _run_dir()
    set_exec_context(session_id="s2")
    d2 = _run_dir()
    assert d1 != d2
    assert d1.name != "default"
    assert d2.parent.name == "_run"


def test_two_sessions_python_exec_do_not_cross_contaminate():
    """**回归 docs/17 P0-2**：并发会话执行的必须是各自的代码。

    实机踩过的现象：A 期望输出 I_AM_A，实际拿到 I_AM_B。
    根因是执行目录全局单例 + 脚本固定叫 main.py。
    """
    from tools.python_exec import PythonExecTool

    tool = PythonExecTool(timeout_s=20)

    async def _run(tag: str, session_id: str) -> str:
        tokens = set_exec_context(
            session_id=session_id,
            execution_id=f"exec-{tag}",
        )
        try:
            res = await tool.execute(code=f"print('I_AM_{tag.upper()}')")
            return res.data.get("stdout", "") if res.data else ""
        finally:
            reset_exec_context(tokens)

    async def _both():
        return await asyncio.gather(_run("a", "sess_a"), _run("b", "sess_b"))

    out_a, out_b = asyncio.run(_both())
    assert "I_AM_A" in out_a
    assert "I_AM_B" not in out_a
    assert "I_AM_B" in out_b
    assert "I_AM_A" not in out_b


def test_write_script_lands_in_session_scoped_dir():
    """脚本目录按会话分区，且同一会话内稳定（下一轮还能引用）。"""
    from tools.script_writer import WriteScriptTool

    tool = WriteScriptTool()
    set_exec_context(session_id="s1")
    try:
        d1 = tool._scope_dir()
        d2 = tool._scope_dir()
        assert d1 == d2, "同一会话内目录要稳定（相对路径跨调用有效）"
        set_exec_context(session_id="s2")
        assert tool._scope_dir() != d1, "不同会话不能共用目录"
    finally:
        reset_exec_context(())


def test_list_scripts_only_shows_current_scope():
    """A 看不到 B 存的脚本。"""
    from tools.script_writer import ListScriptsTool, WriteScriptTool

    async def _write(tag: str, session_id: str):
        tokens = set_exec_context(session_id=session_id)
        try:
            await WriteScriptTool().execute(name=f"{tag}.py", content="print(1)")
        finally:
            reset_exec_context(tokens)

    async def _list(session_id: str):
        tokens = set_exec_context(session_id=session_id)
        try:
            res = await ListScriptsTool().execute()
            return [s["name"] for s in res.data["scripts"]]
        finally:
            reset_exec_context(tokens)

    async def _all():
        await _write("alice", "s1")
        await _write("bob", "s2")
        return await _list("s1"), await _list("s2")

    alice, bob = asyncio.run(_all())
    assert "alice.py" in alice
    assert "bob.py" not in alice
    assert "bob.py" in bob


# ============================================================
# F0-2 工具归属
# ============================================================

def test_tool_proposal_carries_owner(tmp_path, monkeypatch):
    """自造工具必须带归属 —— 否则"谁造的/能不能禁用"无从谈起。"""
    import tools.proposal as mod
    from tools.proposal import ToolProposal, ToolProposalStore, publish

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    class _Hub:
        def __init__(self):
            self.tools = {}
            self.owners = {}

        def names(self):
            return list(self.tools)

        def register_python_tool(
            self, tool, *, owner_agent_id="", visibility="global"
        ):
            self.tools[tool.name] = tool
            self.owners[tool.name] = (owner_agent_id, visibility)

    p = ToolProposal(
        name="owned_tool", description="x", kind="code", implementation="return 1",
        owner_agent_id="sub_abc", visibility="global",
    )
    hub = _Hub()
    ok, msg = publish(p, hub)
    assert ok, msg
    assert hub.owners["owned_tool"] == ("sub_abc", "global")
    # 落盘后仍能读回归属（重启不丢）
    store.save(p)
    assert store.get(p.id).owner_agent_id == "sub_abc"


def test_tool_proposal_owner_defaults_to_current_context(tmp_path, monkeypatch):
    """没显式给 owner 时，从当前执行上下文补 —— 谁在造就是谁的。"""
    import tools.proposal as mod
    from tools.proposal import ToolProposal, ToolProposalStore

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    tokens = set_exec_context(session_id="s7", agent_id="sub_z")
    try:
        p = ToolProposal(name="ctx_tool", description="x", kind="code",
                         implementation="return 1")
    finally:
        reset_exec_context(tokens)
    assert p.owner_agent_id == "sub_z"


def test_tool_visibility_check():
    """visibility 的判定逻辑。"""
    from tools.proposal import ToolProposal

    p = ToolProposal(name="v", description="x", visibility="global")
    assert p.is_visible_to(agent_id="sub_x") is True

    p2 = ToolProposal(name="v2", description="x", visibility="owner",
                      owner_agent_id="sub_x")
    assert p2.is_visible_to(agent_id="sub_x") is True
    assert p2.is_visible_to(agent_id="sub_y") is False


def test_hub_can_disable_all_tools_of_an_owner():
    """一键收回某个 owner 的全部工具 —— 自造工具全局可见的代价，必须有退路。"""
    from tools.hub import ToolHub
    from tools.base import Tool, ToolSchema

    class _T(Tool):
        name = "t1"
        description = "d"

        def schema(self):
            return ToolSchema(name=self.name, description=self.description, params=[])

        async def execute(self, **kwargs):
            from tools.base import ToolResult

            return ToolResult(success=True, data={})

    hub = ToolHub()
    hub.register_python_tool(_T(), owner_agent_id="sub_a")
    assert hub.tools_by_owner("sub_a") == ["t1"]
    assert hub.unregister_owner("sub_a") == ["t1"]
    assert "t1" not in hub.names()


# ============================================================
# F0-3 技能两阶段召回
# ============================================================

def _mk_skill(name, capability, method, patterns):
    from skills.models import Skill

    return Skill(
        name=name, version="1.0.0", capability=capability, method=method,
        patterns=patterns,
    )


def test_format_skills_is_two_stage(monkeypatch):
    """目录给全部（名字+一句话），详情只给召回命中的那几个。"""
    from agents.manager import ManagerAgent

    mgr = ManagerAgent.__new__(ManagerAgent)   # 不跑 __init__（避免读磁盘）
    from infra.logger import get_logger

    mgr.logger = get_logger()
    mgr.skill_store = type("S", (), {
        "match": staticmethod(lambda q, k: [skills[0]]),
    })()
    skills = [
        _mk_skill("daily_report", "整理日报", "按今日完成/问题/明日计划三段输出",
                  ["日报", "周报"]),
        _mk_skill("video_script", "写视频脚本", "分镜+台词+时长", ["视频", "脚本"]),
    ]

    out = mgr._format_skills(skills, query="帮我写日报")
    # 目录里两个都在
    assert "daily_report" in out and "video_script" in out
    # 详情只给命中的
    assert "按今日完成/问题/明日计划三段输出" in out
    assert "分镜+台词+时长" not in out


def test_skill_prompt_does_not_grow_linearly(monkeypatch):
    """技能库变大时，提示词不能线性膨胀（docs/17 P1-4）。"""
    from agents.manager import ManagerAgent
    from infra.logger import get_logger

    mgr = ManagerAgent.__new__(ManagerAgent)
    mgr.logger = get_logger()

    def make(n):
        return [
            _mk_skill(
                f"skill_{i}", f"能力{i}", f"很长的方法说明{'x' * 200} #{i}",
                [f"关键词{i}"],
            )
            for i in range(n)
        ]

    def match(q, k):
        # 召回只命中一个
        return [s for s in make(200) if "skill_0" == s.name][:k]

    mgr.skill_store = type("S", (), {"match": staticmethod(match)})()

    small = len(mgr._format_skills(make(10), query="关键词0"))
    big = len(mgr._format_skills(make(100), query="关键词0"))
    # 目录有上限，详情只有 top-k → 增长远小于 10 倍
    assert big < small * 4, f"提示词随技能数膨胀过快：{small} → {big}"


# ============================================================
# F0-4 记忆 scope
# ============================================================

def test_memory_search_respects_scope(tmp_path):
    """scope 不再是死字段：按作用域过滤能真的隔开。"""
    from core.memory_db import MemoryDB

    db = MemoryDB(db_path=tmp_path / "memories.db")
    db.add(type="context", content="用户偏好简洁的中文日报",
           scope="user")
    db.add(type="context", content="会话里的临时结论",
           scope="session")
    db.add(type="context", content="智能体私有经验",
           scope="agent")

    hits = db.search_cjk("日报", scopes=["user"])
    contents = [e.content for e, _ in hits]
    assert any("偏好简洁" in c for c in contents)
    assert not any("会话里的临时结论" in c for c in contents)
    assert not any("智能体私有经验" in c for c in contents)

    agent_hits = db.search_cjk("智能体私有经验", scopes=["agent"])
    assert agent_hits, "agent 作用域的记忆应该能被召回到"


def test_semantic_memory_accepts_scope_filter(tmp_path):
    """门面层能按 scope 传下去（不传 = 不过滤，保持旧行为）。"""
    import inspect

    from core.semantic_memory import SemanticMemoryStore

    sig = inspect.signature(SemanticMemoryStore.search)
    assert "scopes" in sig.parameters
