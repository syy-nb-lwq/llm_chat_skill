"""会话恢复链路测试:init 携带 session_id → 恢复同一会话。

覆盖:
  - 带合法 session_id → 服务端复用该会话
  - 带他人 session_id → 不复用(越权防护),新建会话
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


@pytest.fixture
def patched_users(monkeypatch, tmp_path):
    import core.user_service as us_mod

    service = us_mod.UserService(base_path=tmp_path / "users")
    monkeypatch.setattr(us_mod, "get_user_service", lambda: service)
    return service


@pytest.mark.asyncio
async def test_init_resumes_existing_session(patched_store, patched_users):
    from backend.session import SessionManager

    user = patched_users.create(display_name="u")
    token = patched_users.issue_token(user.id, user.tenant_id)

    mgr = SessionManager()
    first = await mgr.issue_identity("conn-1", token)
    # 模拟这一轮产生了会话内容
    patched_store.ensure_session(first.session_id, first.user_id)
    patched_store.append_message(
        first.session_id, first.user_id, "user", "上次说的方案"
    )

    # 重连:带上同一个 session_id
    second = await mgr.issue_identity("conn-2", token, resume_session_id=first.session_id)
    assert second.session_id == first.session_id
    assert second.user_id == first.user_id
    # 会话身份仍然是新的
    assert second.client_id != first.client_id


@pytest.mark.asyncio
async def test_init_rejects_foreign_session(patched_store, patched_users):
    """别人的 session_id 不能被恢复。"""
    from backend.session import SessionManager

    alice = patched_users.create(display_name="alice")
    bob = patched_users.create(display_name="bob")
    alice_token = patched_users.issue_token(alice.id, alice.tenant_id)
    bob_token = patched_users.issue_token(bob.id, bob.tenant_id)

    mgr = SessionManager()
    alice_ident = await mgr.issue_identity("conn-a", alice_token)
    patched_store.ensure_session(alice_ident.session_id, alice_ident.user_id)
    patched_store.append_message(
        alice_ident.session_id, alice_ident.user_id, "user", "alice 的私密内容"
    )

    # bob 拿着 alice 的 session_id 来恢复
    bob_ident = await mgr.issue_identity("conn-b", bob_token, resume_session_id=alice_ident.session_id)
    assert bob_ident.session_id != alice_ident.session_id
    assert bob_ident.user_id == bob.id

    # bob 恢复后装不回 alice 的内容
    from core.agent import Agent

    agent = Agent(
        session_id=bob_ident.session_id,
        user_id=bob_ident.user_id,
        persist_session=True,
    )
    assert len(agent.context.messages) == 0


@pytest.mark.asyncio
async def test_init_with_unknown_session_creates_new(patched_store, patched_users):
    from backend.session import SessionManager

    user = patched_users.create()
    token = patched_users.issue_token(user.id, user.tenant_id)
    mgr = SessionManager()
    ident = await mgr.issue_identity("conn-1", token, resume_session_id="s-not-exist")
    assert ident.session_id != "s-not-exist"


@pytest.mark.asyncio
async def test_session_content_survives_new_manager(patched_store, patched_users):
    """换一个 SessionManager(相当于进程重启)后,内容仍在。"""
    from backend.session import SessionManager
    from core.agent import Agent

    user = patched_users.create()
    token = patched_users.issue_token(user.id, user.tenant_id)

    mgr1 = SessionManager()
    ident = await mgr1.issue_identity("conn-1", token)
    agent = Agent(
        session_id=ident.session_id,
        user_id=ident.user_id,
        persist_session=True,
    )
    agent.context.add_user_message("重启前的消息")

    # 模拟重启:新的 SessionManager + 新的 Agent
    mgr2 = SessionManager()
    ident2 = await mgr2.issue_identity("conn-2", token, resume_session_id=ident.session_id)
    agent2 = Agent(
        session_id=ident2.session_id,
        user_id=ident2.user_id,
        persist_session=True,
    )
    contents = [m.content for m in agent2.context.messages]
    assert "重启前的消息" in contents


@pytest.mark.asyncio
async def test_agent_restores_only_recent_window(patched_store, patched_users):
    """恢复只装最近 N 条,更早的靠摘要/检索。"""
    from core.agent import Agent
    from core.session_config import session_restore_limit

    agent = Agent(session_id="s-window", user_id="u1", persist_session=True)
    for i in range(30):
        agent.context.add_user_message(f"消息{i}")

    limit = session_restore_limit()
    again = Agent(session_id="s-window", user_id="u1", persist_session=True)
    assert len(again.context.messages) <= limit
    # 装回来的是最近的
    assert again.context.messages[-1].content == "消息29"


@pytest.mark.asyncio
async def test_session_store_search_feeds_manager(patched_store):
    """指代型提问能从会话历史里检索到相关内容。"""
    from agents.manager import ManagerAgent

    patched_store.append_message("s-q", "u1", "user", "我们定的发布方案是灰度上线")
    patched_store.append_message("s-q", "u1", "assistant", "好的,记录下来")

    mgr = ManagerAgent()
    hints = mgr._search_session_history("上次那个发布方案怎么定的", "s-q", "u1")
    assert hints
    assert any("灰度" in h for h in hints)


def test_non_referential_question_does_not_search(patched_store):
    """普通问题不该触发会话检索(避免噪声)。"""
    from agents.manager import ManagerAgent

    patched_store.append_message("s-q", "u1", "user", "我们定的发布方案是灰度上线")
    mgr = ManagerAgent()
    hints = mgr._search_session_history("北京天气怎么样", "s-q", "u1")
    assert hints == []
