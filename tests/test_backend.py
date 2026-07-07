"""FastAPI 后端集成测试"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    with TestClient(app) as c:
        yield c


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert "tools" in data
    assert "skills" in data


def test_list_tools(client):
    r = client.get("/api/tools")
    assert r.status_code == 200
    data = r.json()
    names = [t["name"] for t in data["tools"]]
    assert "weather_query" in names
    assert "web_search" in names


def test_list_skills(client):
    r = client.get("/api/skills")
    assert r.status_code == 200
    data = r.json()
    assert "skills" in data
    # 内置 travel_plan 应该被加载
    names = [s["name"] for s in data["skills"]]
    assert "travel_plan" in names


def test_delete_skill_not_found(client):
    r = client.delete("/api/skills/__nonexistent_skill__")
    assert r.status_code == 404


def test_delete_and_reload_skill(client, tmp_path, monkeypatch):
    """写一个临时技能 → 列出来 → 删除 → 确认消失"""
    # 重定向 skill_store 路径
    import skills.manager as mgr
    skills_dir = tmp_path / "skills"
    (skills_dir / "user").mkdir(parents=True)
    yaml_content = (
        "name: tmp_skill\n"
        "version: 1.0.0\n"
        "capability: tmp\n"
        "method: tmp\n"
        "patterns: [tmp]\n"
        "steps: []\n"
    )
    (skills_dir / "user" / "tmp_skill@1.0.0.yaml").write_text(yaml_content, encoding="utf-8")

    # 重置 store 指向 tmp
    mgr.reset_skill_store()
    monkeypatch.setattr("infra.config.config.skills_path", skills_dir)
    # 直接重置 + 重建
    mgr._store = mgr.SkillStore(str(skills_dir))

    # 列表
    r = client.get("/api/skills")
    names = [s["name"] for s in r.json()["skills"]]
    assert "tmp_skill" in names

    # 删除
    r = client.delete("/api/skills/tmp_skill")
    assert r.status_code == 200
    assert r.json()["deleted"] == "tmp_skill"

    # 再列
    r = client.get("/api/skills")
    names = [s["name"] for s in r.json()["skills"]]
    assert "tmp_skill" not in names


def test_websocket_init_and_ping(client):
    """WS 协议: init → connected → ping → pong"""
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text(json.dumps({"type": "init", "client_id": "test-cid-001"}))
        msg = ws.receive_json()
        assert msg["type"] == "event"
        assert msg["event"] == "connected"
        assert msg["payload"]["client_id"] == "test-cid-001"

        ws.send_text(json.dumps({"type": "ping"}))
        msg = ws.receive_json()
        assert msg["event"] == "pong"


def test_websocket_chat_emits_events(monkeypatch):
    """chat 触发完整 event 流"""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    # 替换 LLM(避免真实调用)
    from tests.test_agents import _patch_llm
    valid_plan = json.dumps({
        "intent": "查天气",
        "selected_skill": "",
        "tool_tasks": [{"type": "weather_query", "params": {"city": "厦门"}}],
    }, ensure_ascii=False)
    final_answer = "今天厦门天气晴。"
    _patch_llm(monkeypatch, [valid_plan, final_answer])

    # 替换 weather 工具为 stub(避免真实网络)
    from tools.weather import WeatherTool
    from tools.base import ToolResult
    class StubWeather(WeatherTool):
        def execute(self, city, date="today"):
            return ToolResult(success=True, data={"city": city, "summary": "晴"})
    monkeypatch.setattr("tools.weather.WeatherTool", StubWeather)
    monkeypatch.setattr("tools.base._register_builtins", lambda reg: None)
    import tools.base as tb
    tb._registry = None  # 强制重新注册

    from backend.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        with c.websocket_connect("/ws/chat") as ws:
            ws.send_text(json.dumps({"type": "init", "client_id": "test-cid-002"}))
            ws.receive_json()  # connected

            ws.send_text(json.dumps({"type": "chat", "content": "厦门天气"}))

            events = []
            while True:
                msg = ws.receive_json()
                events.append(msg["event"])
                if msg["event"] in ("message_final", "error"):
                    break

            # 期望看到 thinking / plan / tool_call / tool_result / message_delta / message_final
            assert "thinking" in events
            assert "plan" in events
            assert "tool_call" in events
            assert "tool_result" in events
            assert "message_final" in events