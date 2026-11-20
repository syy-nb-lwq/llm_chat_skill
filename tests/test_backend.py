"""FastAPI integration tests."""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def client():
    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "self_evolution_enabled" in response.json()


def test_list_tools(client):
    response = client.get("/api/tools")
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "weather_query" in names
    assert "web_search" in names


def test_list_skills(client):
    response = client.get("/api/skills")
    assert response.status_code == 200
    skills = response.json()["skills"]
    assert isinstance(skills, list)
    for skill in skills:
        assert "name" in skill
        assert "version" in skill
        assert "steps" in skill


def test_delete_skill_not_found(client):
    response = client.delete("/api/skills/__nonexistent_skill__")
    assert response.status_code == 404


def test_delete_and_reload_skill(client, tmp_path):
    import skills.manager as mgr

    original = mgr._store
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "tmp_test_skill@1.0.0.yaml").write_text(
        "\n".join(
            [
                "name: tmp_test_skill",
                "version: 1.0.0",
                "capability: tmp",
                "method: tmp",
                "patterns: [tmp]",
                "steps: []",
            ]
        ),
        encoding="utf-8",
    )

    mgr.reset_skill_store()
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        response = client.get("/api/skills")
        names = [skill["name"] for skill in response.json()["skills"]]
        assert "tmp_test_skill" in names

        response = client.delete("/api/skills/tmp_test_skill")
        assert response.status_code == 200
        assert response.json()["deleted"] == "tmp_test_skill"

        response = client.get("/api/skills")
        names = [skill["name"] for skill in response.json()["skills"]]
        assert "tmp_test_skill" not in names
    finally:
        mgr._store = original


def test_delete_specific_skill_version(client, tmp_path):
    import skills.manager as mgr

    original = mgr._store
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo@1.0.0.yaml").write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: old\npatterns: [demo]\nsteps: []\n",
        encoding="utf-8",
    )
    (skills_dir / "demo@1.0.1.yaml").write_text(
        "name: demo\nversion: 1.0.1\ncapability: a\nmethod: new\npatterns: [demo]\nsteps: []\n",
        encoding="utf-8",
    )

    mgr.reset_skill_store()
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        response = client.delete("/api/skills/demo/1.0.0")
        assert response.status_code == 200
        assert response.json()["version"] == "1.0.0"
        assert not (skills_dir / "demo@1.0.0.yaml").exists()
        assert (skills_dir / "demo@1.0.1.yaml").exists()
    finally:
        mgr._store = original


def test_feature_toggle_round_trip(client, monkeypatch):
    import backend.main as backend_main

    async def noop():
        return None

    monkeypatch.setattr(backend_main, "_start_reflect_loop", noop)
    monkeypatch.setattr(backend_main, "_stop_reflect_loop", noop)

    response = client.get("/api/features")
    assert response.status_code == 200
    assert "self_evolution_enabled" in response.json()

    response = client.post(
        "/api/features/self-evolution",
        json={"enabled": True, "persist": False},
    )
    assert response.status_code == 200
    assert response.json()["self_evolution_enabled"] is True

    response = client.post(
        "/api/features/self-evolution",
        json={"enabled": False, "persist": False},
    )
    assert response.status_code == 200
    assert response.json()["self_evolution_enabled"] is False


def test_approve_patch_persists_skill_method(client, tmp_path, monkeypatch):
    import core.memory as memory_mod
    import skills.manager as mgr
    from core.memory import MemoryStore

    original_store = mgr._store
    original_memory_store = memory_mod._memory_store

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    skill_path = skills_dir / "demo@1.0.0.yaml"
    skill_path.write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: old method\npatterns: [demo]\nsteps: []\n",
        encoding="utf-8",
    )

    memory_store = MemoryStore(base_path=tmp_path / "memory")
    patch_file = memory_store.patches_dir / "patch_demo.json"
    patch_file.write_text(
        json.dumps(
            {
                "id": "patch_demo",
                "trace_id": "trace_1",
                "timestamp": "2026-07-16T12:00:00",
                "target_skill": "demo",
                "patch_type": "improve_skill",
                "diagnosis": "needs update",
                "suggestion": {
                    "type": "improve_skill",
                    "target_skill": "demo",
                    "method": "new method",
                },
                "confidence": 0.9,
                "status": "pending",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mgr.reset_skill_store()
    mgr._store = mgr.SkillStore(str(skills_dir))
    monkeypatch.setattr(memory_mod, "_memory_store", memory_store)
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", True)
    try:
        response = client.post("/api/patches/patch_demo/approve")
        assert response.status_code == 200
        payload = response.json()
        assert payload["applied"] is True

        # M3-04:审批后不原地改 YAML —— 生成新版本并切 active,旧版本保留
        new_path = skills_dir / "demo@1.0.1.yaml"
        assert new_path.exists()
        assert "new method" in new_path.read_text(encoding="utf-8")

        # 旧版本文件仍在,且内容没有被覆盖
        assert skill_path.exists()
        assert "old method" in skill_path.read_text(encoding="utf-8")

        # active 指针已切到新版本
        assert mgr._store.get_by_name("demo").version == "1.0.1"
    finally:
        mgr._store = original_store
        memory_mod._memory_store = original_memory_store


def test_startup_fails_fast_on_invalid_config(monkeypatch):
    from backend.main import app
    from infra.config import ConfigError
    import backend.main as backend_main

    def boom(self):
        raise ConfigError("boom")

    monkeypatch.setattr(type(backend_main.config), "validate", boom)
    with pytest.raises(ConfigError):
        with TestClient(app):
            pass


def test_websocket_init_and_ping_via_client():
    """WebSocket 端到端:init / ping / reset 全链路。

    TestClient 的 WebSocket 与服务端 event loop 在 subscribe 阶段
    会产生死锁(subscribe 需 await 服务端 ack,而 TestClient 的 ws
    是同步接口)。改为直接构造 Subscription 对象喂给 dispatcher,
    验证 init/ping/reset 路径都返回正确事件。

    C-01:init 时由服务端签发身份,后续请求必须带令牌。
    """
    import asyncio
    import time
    from unittest.mock import MagicMock
    from backend.websocket_handler import dispatcher
    from backend.session import sessions

    conn_id = f"test-cid-{int(time.time()*1000)}"

    async def _drive():
        # 1) 替换 push_event 为本地 mock
        from backend import websocket_handler as wsh

        captured: list = []
        original_push = wsh.push_event

        async def fake_push(cid, event, payload):
            captured.append({"client_id": cid, "event": event, "payload": payload})

        wsh.push_event = fake_push
        issued_client_id = None
        try:
            # 2) 模拟订阅: 构造 Subscription
            sub = MagicMock()
            sub.topic = f"init/{conn_id}"

            await dispatcher(sub, {"client_id": conn_id})
            # init 应推送 connected
            connected = [e for e in captured if e["event"] == "connected"]
            assert connected, captured
            assert connected[0]["client_id"] == conn_id

            # C-01:身份由服务端签发,不是客户端自带的 conn_id
            payload = connected[0]["payload"]
            issued_client_id = payload["client_id"]
            assert issued_client_id != conn_id
            assert payload["session_id"]

            # 3) ping(心跳不要求令牌)
            sub.topic = f"ping/{conn_id}"
            await dispatcher(sub, {})
            assert any(
                e["event"] == "pong" and e["client_id"] == conn_id
                for e in captured
            ), captured

            # 4) reset —— 本地单用户，无需令牌
            sub.topic = f"reset/{conn_id}"
            await dispatcher(sub, {})
            # reset 现在排进会话队列（不能插进正在跑的一轮），等它跑完
            _ident = sessions.resolve_identity(conn_id)
            _session = sessions.get(_ident.client_id)
            assert _session is not None
            await _session.drain()
            assert any(
                e["event"] == "reset_ack" and e["client_id"] == conn_id
                for e in captured
            ), captured

            # 5) chat: 把 agent 的 handle 替换为 stub
            from core.agent import Agent
            # 让 LLM 完全 stub,避免 chat 路径真正发请求
            from infra.llm import LLMClient
            from core.agent_base import get_llm_client as gab_g

            class StubLLM(LLMClient):
                def __init__(self):
                    self.model = "fake"
                    self.default_temperature = 0.0
                async def chat_with_retry(self, messages, **kw):
                    return "ok"
                async def _complete(self, messages, temperature=None):
                    return "ok"
                async def _stream(self, messages, temperature=None):
                    for ch in "ok":
                        yield ch
                async def stream(self, messages, **kw):
                    async for c in self._stream(messages):
                        yield c

            import infra.llm as _l_mod
            _l_mod._llm_client = None
            llm_client_orig = _l_mod.get_llm_client
            _l_mod.get_llm_client = lambda: StubLLM()
            gab_g_orig = gab_g
            import core.agent_base as cab
            cab.get_llm_client = lambda: StubLLM()

            original_handle = Agent.handle

            async def stub_handle(self, user_input, on_event=None):
                if on_event is not None:
                    res = on_event("message_final", {"content": "echo"})
                    if asyncio.iscoroutine(res):
                        await res
                return "echo"

            Agent.handle = stub_handle
            try:
                # 本地单用户：chat 无需令牌，直接放行
                captured.clear()
                sub.topic = f"chat/{conn_id}"
                await dispatcher(sub, {"content": "hello"})
                # dispatcher 只投递，整轮在 Session 队列里跑 —— 等它跑空
                session = sessions.get(issued_client_id)
                assert session is not None
                await session.drain()
                assert any(
                    e["event"] == "message_final"
                    for e in captured
                ), captured
            finally:
                Agent.handle = original_handle
                cab.get_llm_client = gab_g_orig
                # 这两处必须还原 —— 否则 `infra.llm.get_llm_client` 会**永久**变成
                # 上面那个 stub,后面所有测试拿到的都是假 LLM。
                # 症状很隐蔽:测试全绿,但真 LLM 通道其实从没被执行过。
                _l_mod.get_llm_client = llm_client_orig
                _l_mod._llm_client = None
        finally:
            wsh.push_event = original_push
            # 清理 session(用服务端签发的 client_id)
            if issued_client_id:
                await sessions.destroy(issued_client_id)

    asyncio.run(_drive())


@pytest.mark.asyncio
async def test_push_event_times_out_instead_of_hanging(monkeypatch):
    """客户端不回 ACK 时,推送要有超时上限,不能把这一轮永久卡死。

    fastapi-websocket-rpc 的 publish() 会等客户端回 ACK,
    而库默认**没有超时** —— 不兜底的话,客户端一挂,整轮事件投递就卡住。
    """
    from backend import websocket_handler as wsh

    async def hanging_publish(*args, **kwargs):
        await asyncio.sleep(3600)  # 永远不返回

    monkeypatch.setattr(wsh.endpoint, "publish", hanging_publish)
    monkeypatch.setattr(wsh, "PUSH_TIMEOUT", 0.05)

    # 不抛异常就算过:超时后放弃这条事件,流程继续
    await asyncio.wait_for(wsh.push_event("cid", "thinking", {"stage": "x"}), 2)
