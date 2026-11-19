"""会话恢复链路测试:init 携带 session_id → 恢复同一会话。

覆盖:
  - 带合法 session_id → 服务端复用该会话
  - 带不存在的 session_id → 新建会话
  - 会话内容跨"重启"仍在
"""
import pytest

from core.session_memory import SessionMemoryStore


@pytest.fixture
def patched_store(monkeypatch, tmp_path):
    import core.session_memory as sm_mod

    store = SessionMemoryStore(db_path=tmp_path / "sessions.db")
    monkeypatch.setattr(sm_mod, "get_session_memory_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_init_resumes_existing_session(patched_store):
    from backend.session import SessionManager

    mgr = SessionManager()
    first = await mgr.issue_identity("conn-1")
    # 模拟这一轮产生了会话内容
    patched_store.ensure_session(first.session_id)
    patched_store.append_message(first.session_id, "user", "上次说的方案")

    # 重连:带上同一个 session_id
    second = await mgr.issue_identity("conn-2", resume_session_id=first.session_id)
    assert second.session_id == first.session_id
    # 会话身份仍然是新的
    assert second.client_id != first.client_id


@pytest.mark.asyncio
async def test_init_without_session_id_creates_new(patched_store):
    """不带 session_id → 新建会话。"""
    from backend.session import SessionManager

    mgr = SessionManager()
    ident = await mgr.issue_identity("conn-1")
    assert ident.session_id.startswith("s-")


@pytest.mark.asyncio
async def test_session_content_survives_new_manager(patched_store):
    """换一个 SessionManager(相当于进程重启)后,内容仍在。"""
    from backend.session import SessionManager
    from core.agent import Agent

    mgr1 = SessionManager()
    ident = await mgr1.issue_identity("conn-1")
    agent = Agent(
        session_id=ident.session_id,
        persist_session=True,
    )
    agent.context.add_user_message("重启前的消息")

    # 模拟重启:新的 SessionManager + 新的 Agent
    mgr2 = SessionManager()
    ident2 = await mgr2.issue_identity("conn-2", resume_session_id=ident.session_id)
    agent2 = Agent(
        session_id=ident2.session_id,
        persist_session=True,
    )
    contents = [m.content for m in agent2.context.messages]
    assert "重启前的消息" in contents


@pytest.mark.asyncio
async def test_agent_restores_only_recent_window(patched_store):
    """恢复只装最近 N 条,更早的靠摘要/检索。"""
    from core.agent import Agent
    from core.session_config import session_restore_limit

    agent = Agent(session_id="s-window", persist_session=True)
    for i in range(30):
        agent.context.add_user_message(f"消息{i}")

    limit = session_restore_limit()
    again = Agent(session_id="s-window", persist_session=True)
    assert len(again.context.messages) <= limit
    # 装回来的是最近的
    assert again.context.messages[-1].content == "消息29"


@pytest.mark.asyncio
async def test_session_store_search_feeds_manager(patched_store):
    """指代型提问能从会话历史里检索到相关内容。"""
    from agents.manager import ManagerAgent

    patched_store.append_message("s-q", "user", "我们定的发布方案是灰度上线")
    patched_store.append_message("s-q", "assistant", "好的,记录下来")

    mgr = ManagerAgent()
    hints = mgr._search_session_history("上次那个发布方案怎么定的", "s-q")
    assert hints
    assert any("灰度" in h for h in hints)


def test_non_referential_question_does_not_search(patched_store):
    """普通问题不该触发会话检索(避免噪声)。"""
    from agents.manager import ManagerAgent

    patched_store.append_message("s-q", "user", "我们定的发布方案是灰度上线")
    mgr = ManagerAgent()
    hints = mgr._search_session_history("北京天气怎么样", "s-q")
    assert hints == []
