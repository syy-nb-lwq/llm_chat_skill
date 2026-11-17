"""工具自增长 —— agent 自己造工具、发布、复用、跨重启恢复。

## 这条链路在测什么

```
create_tool 提案 → 静态校验 → 风险分级 → 跑测试用例 → 发布 → 跨重启恢复 → planner 复用
```

## 实机踩到的三个坑(都在这儿钉住)

1. **planner 只看到工具名,看不到参数** —— 它会把"从结果里选一个链接"
   这种文字塞进 `url` 参数。所以 planner 提示词必须给签名。
2. **`register_python_tool` 注册了但调不动** —— `hub.call_tool` 把执行
   委托给 `PythonSource`,而源里没有动态创建的工具 → "工具不存在"。
3. **重启后工具消失** —— 必须启动时从账本恢复,否则"能力自增长"是假的。
"""
import asyncio
import json

import pytest

from tools.proposal import (
    RISK_HIGH,
    RISK_LOW,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    ToolProposal,
    ToolProposalStore,
    approve,
    assess_risk,
    build_tool,
    can_auto_publish,
    normalize_params,
    publish,
    reject,
    restore_published,
    validate_proposal,
)


@pytest.fixture(autouse=True)
def isolate_workspace(tmp_path, monkeypatch):
    """把产物工作区隔离到 tmp —— 否则测试会往真实 `memory/workspace` 写文件。"""
    import tools.python_exec as pe
    import tools.script_writer as sw

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sw, "workspace_dir", lambda: ws)
    monkeypatch.setattr(pe, "_workspace_dir", lambda: ws)
    return ws


# ============================================================
# 1. 参数归一化 —— planner 会写各种形态
# ============================================================

def test_normalize_params_from_dict():
    """实测 planner 写成 `{"owner": "所有者", "repo": "仓库名"}` —— 要能收。"""
    got = normalize_params({"owner": "仓库所有者", "repo": "仓库名"})
    assert [p["name"] for p in got] == ["owner", "repo"]
    assert got[0]["type"] == "string"
    assert got[0]["required"] is True
    assert got[0]["description"] == "仓库所有者"


def test_normalize_params_from_list():
    got = normalize_params([{"name": "a", "type": "integer"}])
    assert got == [{"name": "a", "type": "integer"}]


def test_normalize_params_empty():
    assert normalize_params(None) == []
    assert normalize_params([]) == []
    assert normalize_params({}) == []


# ============================================================
# 2. 静态校验
# ============================================================

def _http(**kw):
    base = dict(name="demo_tool", description="查点东西", kind="http",
                method="GET", url_template="https://api.example.com/x")
    base.update(kw)
    return ToolProposal(**base)


def test_valid_http_passes():
    errors, _ = validate_proposal(_http(), existing_names=[])
    assert errors == []


@pytest.mark.parametrize("bad_name", ["A_bad", "1bad", "ab", "has space", ""])
def test_name_rules(bad_name):
    errors, _ = validate_proposal(_http(name=bad_name), existing_names=[])
    assert any("name" in e for e in errors)


def test_name_collision_blocked():
    errors, _ = validate_proposal(_http(name="web_search"), existing_names=["web_search"])
    assert any("已存在" in e for e in errors)


def test_https_required():
    errors, _ = validate_proposal(
        _http(url_template="http://api.example.com/x"), existing_names=[]
    )
    assert any("https" in e for e in errors)


def test_placeholder_must_be_declared():
    """模板里用了 `{city}` 但参数表没声明 —— 执行时必然缺参数。"""
    errors, _ = validate_proposal(
        _http(url_template="https://api.example.com/{city}"), existing_names=[]
    )
    assert any("没声明" in e for e in errors)


def test_missing_url_suggests_code_kind():
    """纯计算工具被误写成 http 时,要**告诉它改成 kind=code**。

    实测：planner 想做 BMI 计算工具，写成了 http 又没有 url_template，
    只说"缺 url_template"它不知道怎么改，原地卡死两次。
    """
    errors, _ = validate_proposal(
        _http(url_template="", kind="http"), existing_names=[]
    )
    assert any("kind" in e and "code" in e for e in errors)


def test_empty_description_blocked():
    errors, _ = validate_proposal(_http(description=""), existing_names=[])
    assert any("description" in e for e in errors)


def test_code_requires_implementation():
    p = ToolProposal(name="calc_tool", description="算数", kind="code")
    errors, _ = validate_proposal(p, existing_names=[])
    assert any("implementation" in e for e in errors)


def test_code_static_check_blocks_dangerous():
    p = ToolProposal(
        name="bad_tool", description="坏工具", kind="code",
        implementation="import subprocess\nsubprocess.run(['ls'])",
    )
    errors, _ = validate_proposal(p, existing_names=[])
    assert any("安全检查" in e for e in errors)


def test_code_warns_when_no_output():
    p = ToolProposal(
        name="silent_tool", description="啥也不输出", kind="code",
        implementation="x = 1",
    )
    errors, warnings = validate_proposal(p, existing_names=[])
    assert errors == []
    assert any("return" in w or "print" in w for w in warnings)


def test_kind_auto_detected_from_implementation():
    """只给 implementation 不给 kind → 自动当 code 处理。

    planner 经常忘记写 kind，但 implementation 的存在已经把意图说清楚了。
    """
    p = ToolProposal(name="auto_tool", description="x", implementation="return 1")
    assert p.kind == "code"


# ============================================================
# 3. 风险分级 —— 决定能不能自助
# ============================================================

def test_get_is_low_risk():
    assert assess_risk(_http()) == RISK_LOW


def test_write_method_is_high_risk():
    assert assess_risk(_http(method="POST")) == RISK_HIGH
    assert assess_risk(_http(method="DELETE")) == RISK_HIGH


def test_secret_header_is_high_risk():
    p = _http(headers={"Authorization": "Bearer x"})
    assert assess_risk(p) == RISK_HIGH


def test_code_risk_follows_code_exec_flag(monkeypatch):
    """code 工具的风险等级跟 `CODE_EXEC_ENABLED` 走。

    理由：只要 python_exec 开着，agent **已经能跑任意代码**了，
    再给一个代码工具并没有扩大攻击面；卡人批只是白白增加摩擦。
    反过来关掉执行时，代码工具根本跑不起来 → 判 high（等于拒绝）。
    """
    from infra.config import config

    p = ToolProposal(name="c_tool", description="x", kind="code",
                     implementation="return 1")

    monkeypatch.setattr(config, "code_exec_enabled", True)
    assert assess_risk(p) == RISK_LOW

    monkeypatch.setattr(config, "code_exec_enabled", False)
    assert assess_risk(p) == RISK_HIGH


def test_script_kind_is_medium(monkeypatch):
    from infra.config import config

    monkeypatch.setattr(config, "code_exec_enabled", True)
    p = ToolProposal(name="s_tool", description="x", kind="script",
                     script_path="s.py")
    assert assess_risk(p) not in (RISK_LOW,)


# ============================================================
# 4. 造出来的工具真的能跑
# ============================================================

@pytest.mark.asyncio
async def test_script_tool_executes_top_level_return():
    """agent 最常写的形态：顶层 return（不是函数里）。

    `return` 在模块级是 SyntaxError —— 运行器必须把它包进函数。
    """
    p = ToolProposal(
        name="add_tool", description="加法", kind="code",
        implementation="a = PARAMS['a']; b = PARAMS['b']; return {'sum': a + b}",
        params=[{"name": "a", "type": "number"}, {"name": "b", "type": "number"}],
    )
    tool = build_tool(p)
    result = await tool.execute(a=2, b=3)
    assert result.success, result.error
    assert result.data["result"]["sum"] == 5


@pytest.mark.asyncio
async def test_script_tool_calls_run_function():
    """另一种形态：定义 `run(...)` 但不调用 —— 运行器要自动调它。"""
    p = ToolProposal(
        name="mul_tool", description="乘法", kind="code",
        implementation=(
            "def run(x, y):\n"
            "    return {'product': x * y}\n"
        ),
        params=[{"name": "x", "type": "number"}, {"name": "y", "type": "number"}],
    )
    tool = build_tool(p)
    result = await tool.execute(x=4, y=5)
    assert result.success, result.error
    assert result.data["result"]["product"] == 20


@pytest.mark.asyncio
async def test_script_tool_supports_print_style():
    p = ToolProposal(
        name="print_tool", description="打印", kind="code",
        implementation="print('hello ' + PARAMS['who'])",
        params=[{"name": "who", "type": "string"}],
    )
    tool = build_tool(p)
    result = await tool.execute(who="world")
    assert result.success
    assert "hello world" in result.data["stdout"]


@pytest.mark.asyncio
async def test_script_tool_reports_error():
    p = ToolProposal(
        name="boom_tool", description="炸", kind="code",
        implementation="raise ValueError('我要炸了')",
    )
    tool = build_tool(p)
    result = await tool.execute()
    assert not result.success
    assert "我要炸了" in (result.error or "")


# ============================================================
# 5. 声明式 HTTP(用假客户端)
# ============================================================

@pytest.mark.asyncio
async def test_declarative_http_builds_request():
    from tools.declarative import DeclarativeHttpTool

    captured = {}

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"data": {"price": 42}}

        text = '{"data": {"price": 42}}'

    class FakeClient:
        is_closed = False

        async def request(self, method, url, params=None, headers=None, json=None):
            captured.update(method=method, url=url, params=params, headers=headers)
            return FakeResp()

    p = _http(url_template="https://api.example.com/price",
              params=[{"name": "symbol", "type": "string"}],
              response_path="data.price")
    tool = DeclarativeHttpTool(p)

    async def fake_client():
        return FakeClient()

    tool._get_client = fake_client
    result = await tool.execute(symbol="BTC")
    assert result.success
    assert result.data == 42
    assert captured["url"] == "https://api.example.com/price"
    # 没在模板里出现的参数走 query
    assert captured["params"] == {"symbol": "BTC"}


@pytest.mark.asyncio
async def test_declarative_http_missing_param_errors():
    from tools.declarative import DeclarativeHttpTool

    p = _http(url_template="https://api.example.com/{city}")
    p.params = [{"name": "city", "required": True}]
    tool = DeclarativeHttpTool(p)
    result = await tool.execute()
    assert not result.success
    assert "缺少必填参数" in result.error


def test_extract_by_path():
    from tools.declarative import extract_by_path

    payload = {"data": {"items": [{"v": 1}, {"v": 2}]}}
    assert extract_by_path(payload, "data.items[1].v") == 2
    assert extract_by_path(payload, "data.items") == [{"v": 1}, {"v": 2}]
    assert extract_by_path(payload, "") == payload
    assert extract_by_path(payload, "nope.deeper") is None


# ============================================================
# 6. 发布 / 恢复 / 审批
# ============================================================

class _FakeHub:
    def __init__(self, existing=None):
        self._tools = {n: object() for n in (existing or [])}

    def names(self):
        return list(self._tools)

    def register_python_tool(self, tool):
        if tool.name in self._tools:
            raise ValueError(f"工具已存在: {tool.name}")
        self._tools[tool.name] = tool

    def unregister_tool(self, name):
        return self._tools.pop(name, None) is not None


def test_publish_registers_tool(tmp_path, monkeypatch):
    import tools.proposal as mod

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    p = ToolProposal(name="pub_tool", description="x", kind="code",
                     implementation="return 1")
    hub = _FakeHub()
    ok, msg = publish(p, hub)
    assert ok, msg
    assert "pub_tool" in hub.names()
    assert p.status == STATUS_PUBLISHED


def test_restore_published_after_restart(tmp_path, monkeypatch):
    """**跨重启必须恢复** —— 否则"能力自增长"是假的。"""
    import tools.proposal as mod

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    p = ToolProposal(name="keep_tool", description="x", kind="code",
                     implementation="return 1")
    publish(p, _FakeHub())
    store.save(p)

    # 模拟重启：全新的 hub
    hub2 = _FakeHub()
    assert restore_published(hub2) == 1
    assert "keep_tool" in hub2.names()


def test_restore_skips_already_registered(tmp_path, monkeypatch):
    import tools.proposal as mod

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)
    p = ToolProposal(name="dup_tool", description="x", kind="code",
                     implementation="return 1")
    publish(p, _FakeHub())
    store.save(p)

    hub2 = _FakeHub(existing=["dup_tool"])
    assert restore_published(hub2) == 0      # 不重复注册、也不报错


def test_approve_high_risk_proposal(tmp_path, monkeypatch):
    import tools.proposal as mod

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    p = _http(method="POST")
    p.risk = assess_risk(p)
    p.status = STATUS_PENDING
    store.save(p)

    hub = _FakeHub()
    ok, msg = approve(p.id, hub)
    assert ok, msg
    assert "demo_tool" in hub.names()
    assert store.get(p.id).reviewed_by == "human"


def test_reject_proposal(tmp_path, monkeypatch):
    import tools.proposal as mod

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    p = _http(method="DELETE")
    p.status = STATUS_PENDING
    store.save(p)

    assert reject(p.id, reason="不需要") is True
    saved = store.get(p.id)
    assert saved.status == "rejected"
    assert saved.blocked_reason == "不需要"


# ============================================================
# 7. ★ hub 委托 bug(实机踩过)
# ============================================================

@pytest.mark.asyncio
async def test_dynamically_registered_tool_is_callable():
    """**回归**:`register_python_tool` 注册的工具必须能真的被调用。

    实机症状：planner 选中了自制的 `bmi_calc`，执行时却报"工具不存在"。

    根因：`hub.call_tool` 把执行**委托给 `PythonSource`**，
    而 `PythonSource._tools` 是它自己扫描目录得到的另一份表。
    内置工具两边都有（扫描时就加载了），所以一直没暴露；
    动态创建的工具不在目录里 → 源里找不到 → 报"工具不存在"。

    修法：Python 工具直接执行实例，不绕源。
    """
    from tools.base import Tool, ToolParam, ToolResult, ToolSchema
    from tools.hub import ToolHub

    class EchoTool(Tool):
        name = "echo_dynamic"
        description = "回显"

        def schema(self):
            return ToolSchema(
                name=self.name, description=self.description,
                params=[ToolParam(name="text", type="string")],
            )

        async def execute(self, text=""):
            return ToolResult(success=True, data={"echo": text})

    hub = ToolHub()
    hub.register_python_tool(EchoTool())
    result = await hub.call_tool("echo_dynamic", {"text": "hi"})
    assert result.success, f"动态注册的工具调不动: {result.error}"
    assert result.data["echo"] == "hi"


@pytest.mark.asyncio
async def test_hub_call_tool_emits_events_for_python_tools():
    """绕过 source 之后,事件还是要照常发 —— 否则前端流转面板会少一段。"""
    from tools.base import Tool, ToolResult, ToolSchema
    from tools.hub import ToolHub

    class T(Tool):
        name = "evt_tool"
        description = "x"

        def schema(self):
            return ToolSchema(name=self.name, description=self.description)

        async def execute(self, **kw):
            return ToolResult(success=True, data={"ok": True})

    hub = ToolHub()
    hub.register_python_tool(T())

    events = []

    async def on_event(name, payload):
        events.append(name)

    await hub.call_tool("evt_tool", {}, on_event=on_event)
    assert events == ["tool_call", "tool_result"]


# ============================================================
# 8. 审批接口
# ============================================================

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    import tools.proposal as mod
    from fastapi.testclient import TestClient

    store = ToolProposalStore(base_path=tmp_path / "tools")
    monkeypatch.setattr(mod, "_store", store)

    import backend.main as bm

    with TestClient(bm.app) as c:
        yield c, store


def test_list_proposals_api(api_client):
    c, store = api_client
    p = _http()
    p.status = STATUS_PENDING
    store.save(p)

    r = c.get("/api/tool-proposals")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["proposals"][0]["name"] == "demo_tool"


def test_list_proposals_filter_by_status(api_client):
    c, store = api_client
    a = _http(name="a_tool"); a.status = STATUS_PENDING; store.save(a)
    b = _http(name="b_tool"); b.status = STATUS_PUBLISHED; store.save(b)

    assert c.get("/api/tool-proposals?status=pending").json()["count"] == 1
    assert c.get("/api/tool-proposals?status=published").json()["count"] == 1


def test_approve_requires_owner_token(api_client, monkeypatch):
    """批准工具是**特权操作** —— 配了 OWNER_TOKEN 就必须带。"""
    c, store = api_client
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "secret-owner")
    p = _http(method="POST")
    p.status = STATUS_PENDING
    store.save(p)

    r = c.post(f"/api/tool-proposals/{p.id}/approve")
    assert r.status_code == 401


def test_approve_endpoint_publishes(api_client, monkeypatch):
    c, store = api_client
    from infra.config import config
    from tools.hub import get_tool_hub

    monkeypatch.setattr(config, "owner_token", "")
    p = _http(name="api_ok_tool", method="POST")
    p.risk = assess_risk(p)
    p.status = STATUS_PENDING
    store.save(p)

    r = c.post(f"/api/tool-proposals/{p.id}/approve")
    assert r.status_code == 200, r.text
    assert "api_ok_tool" in get_tool_hub().names()
    assert store.get(p.id).status == STATUS_PUBLISHED


def test_reject_endpoint(api_client, monkeypatch):
    c, store = api_client
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    p = _http(name="api_rej_tool")
    p.status = STATUS_PENDING
    store.save(p)

    r = c.post(f"/api/tool-proposals/{p.id}/reject", json={"reason": "不合适"})
    assert r.status_code == 200
    assert store.get(p.id).status == "rejected"


def test_delete_proposal(api_client, monkeypatch):
    c, store = api_client
    from infra.config import config

    monkeypatch.setattr(config, "owner_token", "")
    p = _http(name="api_del_tool")
    p.status = STATUS_PENDING
    store.save(p)

    r = c.delete(f"/api/tool-proposals/{p.id}")
    assert r.status_code == 200
    assert store.get(p.id) is None
