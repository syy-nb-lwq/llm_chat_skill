"""视频工具 —— 探测 / 拼接。

## 为什么必须做成工具而不是交给 python_exec

`python_exec` **禁止 `subprocess`**，这是对的（动态执行入口，静态检查
保证不了安全）。但拼视频必须调 ffmpeg，所以这件事要做成**受控的一等公民工具**：
参数固定、路径受控、无 shell，而不是把 subprocess 开放给 LLM。

实机症状：agent 生成了 3 个 5.6 秒镜头，却交出一个只有 5.6 秒的"成片"
—— 根本没拼接，而且它做不了，因为既没有 ffmpeg、也不许 subprocess。
"""
import subprocess
from pathlib import Path

import pytest

import tools.video as video


def _ffmpeg_or_skip():
    exe = video.find_ffmpeg()
    if not exe:
        pytest.skip("没有 ffmpeg，跳过（装 pip install imageio-ffmpeg 即可）")
    return exe


def _make_clip(ffmpeg: str, path: Path, seconds: int = 1, size: str = "320x240"):
    """用 lavfi 造一段纯色测试视频（不需要任何素材）。"""
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=red:s={size}:d={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    return path


@pytest.fixture
def clips(tmp_path, monkeypatch):
    ffmpeg = _ffmpeg_or_skip()
    ws = tmp_path / "workspace"
    (ws / "output").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()

    monkeypatch.setattr("tools.script_writer.workspace_dir", lambda: ws)
    monkeypatch.setattr(video, "_authorized_roots", lambda: [ws, proj])

    a = _make_clip(ffmpeg, proj / "a.mp4", 1)
    b = _make_clip(ffmpeg, proj / "b.mp4", 2)
    return ws, proj, a, b


def test_find_ffmpeg_returns_existing_file():
    exe = video.find_ffmpeg()
    if exe:
        assert Path(exe).is_file()


def test_parse_mp4_reads_duration_and_size(clips):
    _ws, _proj, a, b = clips
    info = video.parse_mp4(a)
    assert info["duration_s"] is not None
    assert abs(info["duration_s"] - 1.0) < 0.4
    assert (info["width"], info["height"]) == (320, 240)


def test_probe_tool(clips):
    import asyncio

    _ws, _proj, a, _b = clips
    r = asyncio.run(video.VideoProbeTool().execute(path=str(a)))
    assert r.success, r.error
    assert r.data["width"] == 320
    assert r.data["size"] > 0


def test_concat_two_clips(clips):
    """两段 1s + 2s 拼起来，时长应该约 3s —— 这是"真的拼上了"的判据。"""
    import asyncio

    ws, _proj, a, b = clips
    r = asyncio.run(
        video.VideoConcatTool().execute(
            inputs=[str(a), str(b)], output="final.mp4"
        )
    )
    assert r.success, r.error
    assert r.data["segments"] == 2

    out = ws / "output" / "final.mp4"
    assert out.is_file()
    info = video.parse_mp4(out)
    # 关键断言：拼接后的时长接近两段之和，而不是只等于一段
    assert 2.6 < info["duration_s"] < 3.6, f"拼接后时长异常: {info['duration_s']}"
    assert abs(r.data["duration_s"] - info["duration_s"]) < 0.1
    assert r.data["url"] == "/api/artifacts/output/final.mp4"


def test_concat_requires_two_inputs(clips):
    import asyncio

    _ws, _proj, a, _b = clips
    r = asyncio.run(video.VideoConcatTool().execute(inputs=[str(a)]))
    assert not r.success
    assert "至少" in r.error


def test_concat_rejects_missing_input(clips):
    import asyncio

    _ws, _proj, a, _b = clips
    r = asyncio.run(
        video.VideoConcatTool().execute(inputs=[str(a), "nope.mp4"])
    )
    assert not r.success
    assert "找不到输入" in r.error


def test_concat_rejects_path_outside_roots(clips, tmp_path):
    import asyncio

    _ws, _proj, a, _b = clips
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    r = asyncio.run(
        video.VideoConcatTool().execute(inputs=[str(a), str(outside)])
    )
    assert not r.success


def test_concat_handles_mismatched_resolution(tmp_path, monkeypatch):
    """分辨率不一致时应自动重编码，而不是直接失败。"""
    import asyncio

    ffmpeg = _ffmpeg_or_skip()
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr("tools.script_writer.workspace_dir", lambda: ws)
    monkeypatch.setattr(video, "_authorized_roots", lambda: [ws, proj])

    a = _make_clip(ffmpeg, proj / "a.mp4", 1, "320x240")
    b = _make_clip(ffmpeg, proj / "b.mp4", 1, "640x480")

    r = asyncio.run(
        video.VideoConcatTool().execute(inputs=[str(a), str(b)], output="mix.mp4")
    )
    assert r.success, r.error
    info = video.parse_mp4(ws / "output" / "mix.mp4")
    # 统一到第一段的尺寸
    assert (info["width"], info["height"]) == (320, 240)


def test_tools_registered():
    from tools.hub import get_tool_hub

    names = get_tool_hub().names()
    assert "video_concat" in names
    assert "video_probe" in names
