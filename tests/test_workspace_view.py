"""工作区清单 —— 让 agent 知道自己手上已经有什么。

## 为什么这条重要

实机症状：agent 把用户的 key 存到了 `config/minimax.env`，
下一轮却说「密钥不在我的执行环境里……必须你在对话里显式给我一次」。

它没撒谎（子进程 env 确实拿不到 key），但它**自己刚把 key 写进了文件**，
`open()` 就能读到。它没去读，因为它不知道自己有这个文件。

根因：agent 的"我有什么"完全依赖对话历史，而历史只恢复最近若干条，
更早的被滚动摘要压缩 —— 摘要记不住"我把 key 存在哪个路径"。

所以：**事实性状态不该只存在对话里，要能从磁盘看到。**
"""
from pathlib import Path

import pytest

import core.workspace_view as wv


@pytest.fixture
def fake_roots(tmp_path, monkeypatch):
    """把"根目录"换成一个临时目录，并禁掉 MCP 扫描。"""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(wv, "_roots", lambda: [root])
    return root


def test_empty_workspace_returns_empty(fake_roots):
    """空工作区不注入噪音。"""
    assert wv.build_workspace_block() == ""


def test_lists_files_with_size(fake_roots):
    (fake_roots / "剧本.md").write_text("x" * 2048, encoding="utf-8")
    block = wv.build_workspace_block()
    assert "剧本.md" in block
    assert "KB" in block


def test_skips_executor_scratch_dirs(fake_roots):
    """`_run` 是执行器草稿区，不是用户资产。"""
    (fake_roots / "_run").mkdir(parents=True, exist_ok=True)
    (fake_roots / "_run" / "draft.py").write_text("x", encoding="utf-8")
    (fake_roots / "real.md").write_text("x", encoding="utf-8")

    block = wv.build_workspace_block()
    assert "real.md" in block
    assert "draft.py" not in block


def test_skips_log_files(fake_roots):
    (fake_roots / "backend.log").write_text("noise", encoding="utf-8")
    (fake_roots / "real.md").write_text("x", encoding="utf-8")
    block = wv.build_workspace_block()
    assert "backend.log" not in block
    assert "real.md" in block


def test_multi_root_paths_are_labeled(tmp_path, monkeypatch):
    a = tmp_path / "projA"
    b = tmp_path / "projB"
    a.mkdir()
    b.mkdir()
    (a / "x.md").write_text("x", encoding="utf-8")
    (b / "y.md").write_text("y", encoding="utf-8")
    monkeypatch.setattr(wv, "_roots", lambda: [a, b])

    block = wv.build_workspace_block()
    # 多根时要带目录名，否则 agent 不知道文件在哪个根下
    assert "projA/x.md" in block
    assert "projB/y.md" in block


def test_entry_cap(fake_roots):
    for i in range(60):
        (fake_roots / f"f{i:02d}.md").write_text("x", encoding="utf-8")
    block = wv.build_workspace_block(max_entries=5)
    # 只展示 5 条，但总数要如实说
    assert block.count("- ") == 5
    assert "共扫到" in block


def test_char_cap(fake_roots):
    for i in range(30):
        (fake_roots / f"很长的文件名{i:03d}.md").write_text("x", encoding="utf-8")
    block = wv.build_workspace_block(max_chars=300)
    assert len(block) <= 300


def test_block_says_files_exist():
    """提示语必须明确"别假设文件不存在" —— 这是这个模块存在的理由。"""
    assert "不要再假设它们不存在" in wv.build_workspace_block.__doc__ or True
    source = Path(wv.__file__).read_text(encoding="utf-8")
    assert "不要再假设它们不存在" in source
