"""产物登记 —— 把授权目录里已有的文件变成可下载产物。

## 为什么要有这个工具

`/api/artifacts` 只服务 `memory/workspace`，而 agent 干活的地方常常是
MCP 授权目录（`D:/llm_chat_workspace/ai_drama_project`）。
实机症状：agent 在那儿生成了 2 MB 的 mp4，用户在界面上**下载不到**。

## 安全边界（这里比功能更值得测）

这个工具把文件复制到「认证用户可下载」的位置，所以它不能是万能读盘：

- 来源必须在授权根目录内
- **应用自身目录一律拒绝** —— 那里有 `.env`（真实 key）
- 敏感文件名一律拒绝（`.env` / `*.pem` / `*.key` / `secret` …）

> 判据：**能被 agent 读 ≠ 能被用户下载。**
"""
import asyncio

import pytest

import tools.artifact_publish as ap


@pytest.fixture
def env(tmp_path, monkeypatch):
    """两个授权根：产物目录 + 一个"项目"目录。"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr("tools.script_writer.workspace_dir", lambda: ws)
    monkeypatch.setattr(ap, "_authorized_roots", lambda: [ws, proj])
    return ws, proj


def _run(coro):
    return asyncio.run(coro)


def test_publishes_file_from_authorized_root(env):
    ws, proj = env
    src = proj / "样片.mp4"
    src.write_bytes(b"x" * 1024)

    r = _run(ap.PublishArtifactTool().execute(path=str(src)))
    assert r.success, r.error
    assert r.data["url"] == "/api/artifacts/output/样片.mp4"
    assert (ws / "output" / "样片.mp4").is_file()
    assert r.data["size"] == 1024
    # 前端「生成的文件」区靠这个字段列出来
    assert r.data["artifacts"][0]["path"] == "output/样片.mp4"


def test_resolves_relative_path_against_roots(env):
    ws, proj = env
    (proj / "a.md").write_text("hi", encoding="utf-8")
    r = _run(ap.PublishArtifactTool().execute(path="a.md"))
    assert r.success, r.error


def test_rejects_env_file(env):
    """`.env` 里是真实 key，绝不能变成下载链接。"""
    ws, proj = env
    (proj / ".env").write_text("OPENAI_API_KEY=sk-real", encoding="utf-8")
    r = _run(ap.PublishArtifactTool().execute(path=str(proj / ".env")))
    assert not r.success
    assert "密钥" in r.error or "凭据" in r.error


@pytest.mark.parametrize("name", [
    "id_rsa", "server.pem", "key.key", "my_secret.txt", "credentials.json",
    "minimax.env",
])
def test_rejects_sensitive_names(env, name):
    ws, proj = env
    f = proj / name
    f.write_text("x", encoding="utf-8")
    r = _run(ap.PublishArtifactTool().execute(path=str(f)))
    assert not r.success, f"{name} 不该被放行"


def test_rejects_file_outside_roots(tmp_path, env):
    ws, proj = env
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "data.bin"
    f.write_bytes(b"x")
    r = _run(ap.PublishArtifactTool().execute(path=str(f)))
    assert not r.success
    assert "不在授权目录内" in r.error


def test_rejects_missing_file(env):
    r = _run(ap.PublishArtifactTool().execute(path="nope.mp4"))
    assert not r.success
    assert "不存在" in r.error


def test_rejects_empty_path(env):
    r = _run(ap.PublishArtifactTool().execute(path=""))
    assert not r.success


@pytest.mark.parametrize("bad", ["../evil.mp4", "/abs/evil.mp4", "C:/x.mp4", ".."])
def test_rejects_path_traversal_in_name(env, bad):
    ws, proj = env
    (proj / "ok.mp4").write_bytes(b"x")
    r = _run(ap.PublishArtifactTool().execute(path=str(proj / "ok.mp4"), name=bad))
    assert not r.success, f"{bad} 不该被接受"


def test_app_root_is_not_an_authorized_root():
    """应用自身目录不能出现在可登记根里（那里有 .env / .git / 源码）。"""
    from pathlib import Path

    app_root = Path(ap.__file__).resolve().parent.parent
    roots = ap._authorized_roots()
    assert all(r.resolve() != app_root for r in roots)


def test_tool_is_registered():
    from tools.hub import get_tool_hub

    assert "publish_artifact" in get_tool_hub().names()
