"""FastAPI integration tests."""
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
        assert skill_path.exists()
        text = skill_path.read_text(encoding="utf-8")
        assert "new method" in text
    finally:
        mgr._store = original_store
        memory_mod._memory_store = original_memory_store



def test_approve_user_correction_blocks_without_regression_examples(client, tmp_path, monkeypatch):
    import core.memory as memory_mod
    import skills.manager as mgr
    from core.memory import MemoryStore

    original_store = mgr._store
    original_memory_store = memory_mod._memory_store

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    skill_path = skills_dir / "demo@1.0.0.yaml"
    skill_path.write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: old method\npatterns: [demo]\nsteps: []\nexamples: []\n",
        encoding="utf-8",
    )

    memory_store = MemoryStore(base_path=tmp_path / "memory")
    patch_file = memory_store.patches_dir / "patch_demo_correction.json"
    patch_file.write_text(
        json.dumps(
            {
                "id": "patch_demo_correction",
                "trace_id": "trace_2",
                "timestamp": "2026-07-16T12:00:00",
                "target_skill": "demo",
                "patch_type": "user_correction",
                "diagnosis": "用户纠正",
                "suggestion": {
                    "type": "improve_method",
                    "target_skill": "demo",
                    "version_target": "next",
                    "user_feedback": "问题部分如果为空就不显示",
                },
                "confidence": 0.95,
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
        response = client.post("/api/patches/patch_demo_correction/approve")
        assert response.status_code == 200
        payload = response.json()
        assert payload["applied"] is False
        assert "缺少旧样例，无法执行回归对比" in payload["rejection_reasons"]
        assert payload["regression_results"]["passed"] is False
        assert payload["risk_summary"]["risk_level"] == "high"
        assert not (skills_dir / "user" / "demo@1.0.1.yaml").exists()
    finally:
        mgr._store = original_store
        memory_mod._memory_store = original_memory_store



def test_auto_approved_patch_still_requires_review_gate(client, tmp_path, monkeypatch):
    import core.memory as memory_mod
    import skills.manager as mgr
    from core.memory import MemoryStore

    original_store = mgr._store
    original_memory_store = memory_mod._memory_store

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    skill_path = skills_dir / "demo@1.0.0.yaml"
    skill_path.write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: old method\npatterns: [demo]\nsteps: []\nexamples: [示例1]\n",
        encoding="utf-8",
    )

    memory_store = MemoryStore(base_path=tmp_path / "memory")
    patch_file = memory_store.patches_dir / "patch_demo_auto.json"
    patch_file.write_text(
        json.dumps(
            {
                "id": "patch_demo_auto",
                "trace_id": "trace_3",
                "timestamp": "2026-07-16T12:00:00",
                "target_skill": "demo",
                "patch_type": "improve_skill",
                "diagnosis": "high confidence",
                "suggestion": {
                    "type": "improve_skill",
                    "target_skill": "demo"
                },
                "confidence": 0.95,
                "status": "auto_approved",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mgr.reset_skill_store()
    mgr._store = mgr.SkillStore(str(skills_dir))

    def fail_update_skill(*args, **kwargs):
        raise AssertionError("auto_approved patch should not directly call update_skill")

    monkeypatch.setattr(mgr._store, "update_skill", fail_update_skill)
    monkeypatch.setattr(memory_mod, "_memory_store", memory_store)
    monkeypatch.setattr("backend.main.config.self_evolution_enabled", True)
    try:
        response = client.post("/api/patches/patch_demo_auto/approve")
        assert response.status_code == 200
        payload = response.json()
        assert payload["applied"] is False
        assert "patch 缺少可发布的结构化变更" in payload["rejection_reasons"]
        assert payload["risk_summary"]["patch_status"] == "auto_approved"
        assert payload["risk_summary"]["risk_level"] == "high"
        assert skill_path.read_text(encoding="utf-8").count("old method") == 1
        assert not (skills_dir / "user" / "demo@1.0.1.yaml").exists()
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
    """
    import asyncio
    import time
    from unittest.mock import MagicMock
    from backend.websocket_handler import dispatcher, push_event
    from backend.session import sessions

    client_id = f"test-cid-{int(time.time()*1000)}"

    async def _drive():
        # 1) 替换 push_event 为本地 mock
        from backend import websocket_handler as wsh

        captured: list = []
        original_push = wsh.push_event

        async def fake_push(cid, event, payload):
            captured.append({"client_id": cid, "event": event, "payload": payload})

        wsh.push_event = fake_push
        try:
            # 2) 模拟订阅: 构造 Subscription
            sub = MagicMock()
            sub.topic = f"init/{client_id}"

            await dispatcher(sub, {"client_id": client_id})
            # init 应推送 connected
            assert any(
                e["event"] == "connected" and e["client_id"] == client_id
                for e in captured
            ), captured

            # 3) ping
            sub.topic = f"ping/{client_id}"
            await dispatcher(sub, {})
            assert any(
                e["event"] == "pong" and e["client_id"] == client_id
                for e in captured
            ), captured

            # 4) reset
            sub.topic = f"reset/{client_id}"
            await dispatcher(sub, {})
            assert any(
                e["event"] == "reset_ack" and e["client_id"] == client_id
                for e in captured
            ), captured

            # 5) chat: 把 agent 的 handle 替换为 stub
            from core.agent import Agent
            from agents import manager as mgr_mod
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
                sub.topic = f"chat/{client_id}"
                await dispatcher(sub, {"content": "hello"})
                # 任何 message_final 事件被推送给该 client
                assert any(
                    e["client_id"] == client_id
                    for e in captured
                ), captured
            finally:
                Agent.handle = original_handle
                cab.get_llm_client = gab_g_orig
        finally:
            wsh.push_event = original_push
            # 清理 session
            await sessions.destroy(client_id)

    asyncio.run(_drive())
