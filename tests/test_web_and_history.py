"""联网能力 + 会话恢复的测试。

覆盖三件实机发现的事:

1. **搜索后端会挂** —— 公共 SearXNG 实例/Wikipedia 在这台机器上全废,
   必须有多来源兜底,而且 DeepSeek 自己的 Search API 优先。
2. **工具链式调用** —— planner 曾把占位符写成文字("从结果里选一个链接"),
   执行期必失败。需要支持 `${t1.data.results[0].url}` 这种下标语法,
   而且报错要告诉它怎么改。
3. **刷新页面不该丢对话** —— 会话内容是全量保留的,前端要能取回来。
"""
import json

import pytest

from infra.auth import issue_session_token


# ============================================================
# 1. 占位符替换(链式调用的基础)
# ============================================================

def test_resolve_array_index():
    """`${t1.data.results[0].url}` 必须能解析 —— 搜索结果就是数组。

    没有下标语法时,planner 只能写文字占位,执行期直接失败。
    """
    from agents.learning import resolve_params
    from tools.base import ToolResult

    results = {
        "t1": ToolResult(
            success=True,
            data={"results": [
                {"title": "A", "url": "https://a.example"},
                {"title": "B", "url": "https://b.example"},
            ]},
        ),
    }
    resolved = resolve_params({"url": "${t1.data.results[0].url}"}, results)
    assert resolved["url"] == "https://a.example"


def test_resolve_array_index_second():
    from agents.learning import resolve_params
    from tools.base import ToolResult

    results = {"t1": ToolResult(success=True, data={"results": [{"url": "u0"}, {"url": "u1"}]})}
    assert resolve_params({"u": "${t1.data.results[1].url}"}, results)["u"] == "u1"


def test_resolve_out_of_range_is_empty():
    """越界要解析成空串,不能抛 —— 上游少一条不该让整轮崩掉。"""
    from agents.learning import resolve_params
    from tools.base import ToolResult

    results = {"t1": ToolResult(success=True, data={"results": [{"url": "u0"}]})}
    assert resolve_params({"u": "${t1.data.results[9].url}"}, results)["u"] == ""


def test_resolve_plain_field_still_works():
    """回归:不带下标的引用不能坏。"""
    from agents.learning import resolve_params
    from tools.base import ToolResult

    results = {"t1": ToolResult(success=True, data={"text": "hello"})}
    assert resolve_params({"q": "${t1.data.text}"}, results)["q"] == "hello"


def test_resolve_failed_upstream_gives_empty():
    from agents.learning import resolve_params

    results = {"t1": type("R", (), {"success": False, "data": None, "error": "boom"})()}
    assert resolve_params({"q": "${t1.data.text}"}, results)["q"] == ""


def test_planner_prompt_teaches_placeholder_syntax():
    """planner 提示词必须教占位符语法 —— 否则它会写文字占位符。"""
    from agents.manager import ManagerAgent

    prompt = ManagerAgent().system_prompt()
    assert "depends_on" in prompt
    assert "${t1.data.results[0].url}" in prompt, (
        "提示词里没有可用的占位符示例,planner 会写文字占位符"
    )
    assert "绝对不要写" in prompt


# ============================================================
# 2. web_fetch
# ============================================================

@pytest.mark.asyncio
async def test_web_fetch_rejects_placeholder_with_guidance():
    """占位符写错时要**告诉它怎么改**,不能只说"不合法"。"""
    from tools.web_fetch import WebFetchTool

    result = await WebFetchTool().execute(url="从搜索结果中选择最相关的一个网页链接")
    assert not result.success
    assert "http://" in result.error
    # 关键:给出可执行的修正方向
    assert "${t1.data.results[0].url}" in result.error
    assert "depends_on" in result.error


@pytest.mark.asyncio
async def test_web_fetch_rejects_empty_with_guidance():
    from tools.web_fetch import WebFetchTool

    result = await WebFetchTool().execute(url="")
    assert not result.success
    assert "占位符" in result.error


# ============================================================
# 3. 搜索后端
# ============================================================

def test_search_backends_include_fallbacks():
    """后端必须是多来源 —— 公共实例会挂,单点就是死。"""
    from tools.search import SearchTool

    tool = SearchTool()
    names = [b.get("name") for b in tool.backends]
    assert len(names) >= 4, f"后端太少,单点故障: {names}"
    assert "duckduckgo" in names


def test_deepseek_backend_only_when_base_url_is_deepseek(monkeypatch):
    """只在 base_url 指向 deepseek 时才用它的搜索。

    否则拿 OpenAI 的 key 去调 api.deepseek.com 纯属浪费往返。
    """
    from infra.config import config
    from tools.search import deepseek_search_config

    monkeypatch.setattr(config, "openai_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(config, "openai_api_key", "sk-test")
    cfg = deepseek_search_config()
    assert cfg is not None
    assert cfg["url"] == "https://api.deepseek.com/v1/tools/search"

    monkeypatch.setattr(config, "openai_base_url", "https://api.openai.com/v1")
    assert deepseek_search_config() is None


def test_deepseek_backend_needs_key(monkeypatch):
    from infra.config import config
    from tools.search import deepseek_search_config

    monkeypatch.setattr(config, "openai_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(config, "openai_api_key", "")
    assert deepseek_search_config() is None


def test_search_backend_list_puts_deepseek_first(monkeypatch):
    """DeepSeek 搜索质量更好,要排在最前面。"""
    from infra.config import config
    from tools.search import SearchTool

    monkeypatch.setattr(config, "openai_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(config, "openai_api_key", "sk-test")
    tool = SearchTool()
    assert tool.backends[0]["name"] == "deepseek_search"


@pytest.mark.asyncio
async def test_search_returns_clear_error_when_all_fail(monkeypatch):
    """全挂时要给出每个后端的原因,而不是一句"失败了"。"""
    from tools.search import SearchTool

    tool = SearchTool(backends=[{"name": "dead", "kind": "html", "url": "http://127.0.0.1:9/x", "params": {}}])
    result = await tool.execute(query="test")
    assert not result.success
    assert "dead" in result.error
    await tool.aclose()


# ============================================================
# 4. 会话历史(刷新不丢对话)
# ============================================================

@pytest.fixture
def client(tmp_path):
    from backend.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def test_session_messages_requires_token(client):
    resp = client.get("/api/session/messages")
    assert resp.status_code == 401


def test_session_messages_empty_for_new_session(client):
    """全新会话返回空历史,不是 404 —— 前端不用特殊处理。"""
    token = issue_session_token("u1", "s-notexist", "c1")
    resp = client.get(
        "/api/session/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_session_messages_returns_saved_history(client):
    """存进去的消息要能取回来。"""
    from core.session_memory import get_session_memory_store

    store = get_session_memory_store()
    sid = "s-history-1"
    store.append_message(sid, "u1", "user", "我叫小明", turn_id="t1")
    store.append_message(sid, "u1", "assistant", "好的", turn_id="t1")

    token = issue_session_token("u1", sid, "c1")
    resp = client.get(
        "/api/session/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant"]


def test_session_messages_is_scoped_to_token_user(client):
    """**只返回令牌里那个用户的消息** —— 不能读到别人的会话。"""
    from core.session_memory import get_session_memory_store

    store = get_session_memory_store()
    sid = "s-history-2"
    store.append_message(sid, "u2", "user", "别人的秘密", turn_id="t1")

    # u1 的令牌指向同一个 session_id —— 也不该读到 u2 的内容
    token = issue_session_token("u1", sid, "c1")
    resp = client.get(
        "/api/session/messages", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert "别人的秘密" not in resp.text


def test_session_messages_rejects_bad_token(client):
    resp = client.get(
        "/api/session/messages", headers={"Authorization": "Bearer garbage"}
    )
    assert resp.status_code == 401
