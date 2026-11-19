"""产物交付 + 流式增量合并的测试。

对应两个实机问题:

1. **生成的视频/报表用户看不到** —— `python_exec` 把产物写进
   `memory/workspace/`,但没有任何口子给前端取。
2. **一条长回答推 1594 次 `message_delta`** —— 每个都 publish + 等 ACK,
   日志里刷出 1594 条"推送事件超时"。
"""
import asyncio
import time

import pytest



# ============================================================
# 1. 产物下载
# ============================================================

@pytest.fixture
def artifact_client(tmp_path, monkeypatch):
    """带独立工作区的后端。"""
    import backend.main as bm
    from fastapi.testclient import TestClient

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bm, "_ARTIFACT_ROOT", ws)

    with TestClient(bm.app) as c:
        yield c, ws


def test_download_needs_no_token(artifact_client):
    """本地单用户：产物下载无需令牌。"""
    c, ws = artifact_client
    (ws / "a.txt").write_text("hi", encoding="utf-8")
    assert c.get("/api/artifacts/a.txt").status_code == 200


def test_download_plain_path(artifact_client):
    c, ws = artifact_client
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    r = c.get("/api/artifacts/a.txt")
    assert r.status_code == 200
    assert r.text == "hello"


def test_download_from_subdirectory(artifact_client):
    c, ws = artifact_client
    (ws / "b.txt").write_text("via query", encoding="utf-8")
    r = c.get("/api/artifacts/b.txt")
    assert r.status_code == 200
    assert r.text == "via query"


def test_download_inline_by_default(artifact_client):
    c, ws = artifact_client
    (ws / "v.mp4").write_bytes(b"\x00" * 16)
    r = c.get("/api/artifacts/v.mp4")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["content-type"].startswith("video/mp4")


def test_download_forced_attachment(artifact_client):
    c, ws = artifact_client
    (ws / "r.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    r = c.get("/api/artifacts/r.csv", params={"download": "true"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")


def test_download_nested_path(artifact_client):
    """产物可能在子目录里(python_exec 的 run 目录)。"""
    c, ws = artifact_client
    sub = ws / "run1"
    sub.mkdir()
    (sub / "out.txt").write_text("nested", encoding="utf-8")
    r = c.get("/api/artifacts/run1/out.txt")
    assert r.status_code == 200
    assert r.text == "nested"


@pytest.mark.parametrize("bad", [
    "../../.env",
    "..\\..\\.env",
    "a/../../.env",
    "/etc/passwd",
    "",
])
def test_traversal_is_blocked(artifact_client, bad):
    """**路径穿越必须拦死** —— 否则鉴权形同虚设。

    反斜杠变体也要挡:Windows 上 `..\\..\\` 和 `../../` 等价。
    """
    from backend.main import _resolve_artifact
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _resolve_artifact(bad)
    assert exc.value.status_code in (400, 403)


def test_missing_artifact_404(artifact_client):
    c, ws = artifact_client
    r = c.get("/api/artifacts/nope.txt")
    assert r.status_code == 404


def test_list_artifacts(artifact_client):
    c, ws = artifact_client
    (ws / "one.txt").write_text("1", encoding="utf-8")
    sub = ws / "d"
    sub.mkdir()
    (sub / "two.txt").write_text("22", encoding="utf-8")

    r = c.get("/api/artifacts")
    assert r.status_code == 200
    data = r.json()
    names = {a["name"] for a in data["artifacts"]}
    assert {"one.txt", "two.txt"} <= names
    paths = {a["path"] for a in data["artifacts"]}
    assert "d/two.txt" in paths, "子目录的文件路径要用正斜杠"


# ============================================================
# 2. 产物收集(挂到 message_final)
# ============================================================

def test_collect_artifacts_from_tool_results():
    from core.agent import Agent
    from tools.base import ToolResult

    results = {
        "t1": ToolResult(success=True, data={"artifacts": [
            {"name": "report.csv", "path": "run1/report.csv", "size": 2048},
        ]}),
        "t2": ToolResult(success=True, data={"text": "没有产物"}),
    }
    got = Agent._collect_artifacts(results)
    assert len(got) == 1
    assert got[0]["name"] == "report.csv"
    assert got[0]["url"] == "/api/artifacts/run1/report.csv"


def test_collect_artifacts_dedupes():
    """同一个文件被两个工具都报了,只留一条。"""
    from core.agent import Agent
    from tools.base import ToolResult

    item = {"name": "x.txt", "path": "x.txt", "size": 1}
    results = {
        "t1": ToolResult(success=True, data={"artifacts": [item]}),
        "t2": ToolResult(success=True, data={"artifacts": [item]}),
    }
    assert len(Agent._collect_artifacts(results)) == 1


def test_collect_artifacts_tolerates_bad_shapes():
    from core.agent import Agent
    from tools.base import ToolResult

    results = {
        "t1": ToolResult(success=True, data={"artifacts": "not-a-list"}),
        "t2": ToolResult(success=True, data=None),
        "t3": ToolResult(success=False, error="x"),
    }
    assert Agent._collect_artifacts(results) == []


@pytest.mark.asyncio
async def test_python_exec_artifact_path_is_relative(tmp_path, monkeypatch):
    """**回归**:`python_exec` 产物路径必须是相对路径。

    早先给的是绝对路径(`D:\\...\\workspace\\a.txt`),拼成下载 URL 会得到
    `/api/artifacts/D:/.../a.txt` —— 前端点开就是 404。
    接口是 `/api/artifacts/{相对路径}`,所以这里必须对齐。
    """
    import tools.python_exec as pe

    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(pe, "_workspace_dir", lambda: ws)

    tool = pe.PythonExecTool()
    result = await tool.execute(code="open('a.txt', 'w').write('x')")
    assert result.success
    arts = result.data["artifacts"]
    assert arts, "应该收集到产物"
    for a in arts:
        assert not a["path"].startswith("/"), f"路径不该是绝对路径: {a['path']}"
        assert ":" not in a["path"], f"路径不该带盘符: {a['path']}"


@pytest.mark.asyncio
async def test_write_script_returns_artifact(tmp_path, monkeypatch):
    """脚本本身也是交付物 —— 前端要能下载它。"""
    import tools.script_writer as sw

    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(sw, "workspace_dir", lambda: ws)

    tool = sw.WriteScriptTool(run_id="r1")
    result = await tool.execute(name="gen.py", content="print(1)")
    arts = result.data["artifacts"]
    assert len(arts) == 1
    assert arts[0]["path"] == "r1/gen.py"


# ============================================================
# 3. delta 合并
# ============================================================

@pytest.fixture
def fake_push(monkeypatch):
    """拦掉真实 publish,记录推出去的事件。"""
    import backend.websocket_handler as wh

    sent = []

    async def _fake_push(client_id, event, payload):
        sent.append((event, payload))

    monkeypatch.setattr(wh, "push_event", _fake_push)
    return sent


@pytest.mark.asyncio
async def test_deltas_are_coalesced(fake_push):
    """1500 个单字增量应该被合并成少数几次推送。"""
    from backend.websocket_handler import DeltaCoalescer

    b = DeltaCoalescer("c1")
    for _ in range(1500):
        await b.send("message_delta", {"delta": "字"})
    await b.flush()

    deltas = [p for e, p in fake_push if e == "message_delta"]
    assert b.deltas_in == 1500
    assert len(deltas) < 60, f"合并效果不足:{len(deltas)} 次推送"
    # 内容不能丢
    assert "".join(d["delta"] for d in deltas) == "字" * 1500


@pytest.mark.asyncio
async def test_coalescer_preserves_content_exactly(fake_push):
    from backend.websocket_handler import DeltaCoalescer

    b = DeltaCoalescer("c1")
    chunks = [f"片段{i}," for i in range(300)]
    for c in chunks:
        await b.send("message_delta", {"delta": c})
    await b.flush()

    deltas = [p["delta"] for e, p in fake_push if e == "message_delta"]
    assert "".join(deltas) == "".join(chunks)


@pytest.mark.asyncio
async def test_non_delta_event_flushes_first(fake_push):
    """**顺序不能乱**:message_final 必须排在已缓存的增量之后。

    否则前端会先渲染最终回答,再收到零散增量 —— 画面倒流。
    """
    from backend.websocket_handler import DeltaCoalescer

    b = DeltaCoalescer("c1")
    await b.send("message_delta", {"delta": "前半段"})
    await b.send("message_final", {"content": "前半段后半段"})

    events = [e for e, _ in fake_push]
    assert events == ["message_delta", "message_final"], f"顺序错了: {events}"
    assert fake_push[0][1]["delta"] == "前半段"


@pytest.mark.asyncio
async def test_flush_is_idempotent(fake_push):
    from backend.websocket_handler import DeltaCoalescer

    b = DeltaCoalescer("c1")
    await b.send("message_delta", {"delta": "x"})
    await b.flush()
    n = len(fake_push)
    await b.flush()          # 再冲一次不该产生新事件
    assert len(fake_push) == n


@pytest.mark.asyncio
async def test_empty_delta_ignored(fake_push):
    from backend.websocket_handler import DeltaCoalescer

    b = DeltaCoalescer("c1")
    await b.send("message_delta", {"delta": ""})
    await b.flush()
    assert fake_push == []
    assert b.deltas_in == 0


@pytest.mark.asyncio
async def test_coalescer_has_time_based_flush(fake_push):
    """流很慢时,不能一直攒着不发 —— 超过间隔就要推出去。"""
    from backend.websocket_handler import DeltaCoalescer

    b = DeltaCoalescer("c1", max_chars=10_000, max_interval=0.05)
    await b.send("message_delta", {"delta": "a"})
    assert fake_push == [], "刚发一个不该立刻推"
    time.sleep(0.08)
    await b.send("message_delta", {"delta": "b"})
    assert len(fake_push) == 1, "超过时间间隔后应该推出去"
