"""Session manager cleanup tests."""
import pytest

from backend.session import Session, SessionManager


@pytest.mark.asyncio
async def test_session_gc_awaits_async_dispose_callbacks():
    manager = SessionManager(ttl_s=0)
    session = Session(client_id="cid", agent=None)  # type: ignore[arg-type]
    called = {"value": False}

    async def dispose():
        called["value"] = True

    session.dispose_callbacks.append(dispose)
    manager._sessions["cid"] = session
    session.last_active = 0

    await manager.gc()
    assert called["value"] is True
    assert "cid" not in manager._sessions
