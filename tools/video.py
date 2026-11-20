"""视频工具 —— 拼接 / 探测时长分辨率。

## 为什么需要单独的模块

`python_exec` **禁止 `subprocess`**（动态执行入口，静态检查无法保证安全）。
这个限制是对的，不能为了拼视频就放开。

所以拼视频这件事必须做成**一等公民的工具**：由我们控制 ffmpeg 的调用方式
（固定参数、受控路径、无 shell），而不是把 `subprocess` 交给 LLM 自由发挥。

实机踩过：agent 生成了 3 个 5.6 秒的镜头，然后生成一个叫
`ai_drama_final.mp4` 的"成片" —— 但那个成片只有 5.6 秒，
**根本没拼接**（三个镜头合计 16.9 秒）。它做不了，因为没有 ffmpeg，
而且 `subprocess` 被禁。

## ffmpeg 从哪来

优先 `imageio-ffmpeg`（pip 包自带静态二进制，不动系统 PATH），
其次系统的 `ffmpeg`。两者都没有就明确报错，并给出安装命令 —— 
不要让 agent 猜。

## 为什么 probe 不用 ffprobe

`imageio-ffmpeg` 只带 ffmpeg、不带 ffprobe。而 MP4 的时长/分辨率
就在容器头部几个字节里，用纯 Python 解析就够（本文件 `parse_mp4`）。
少一个外部依赖，也少一个失败点。
"""
from __future__ import annotations

import asyncio
import shutil
import struct
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from infra.logger import get_logger
from tools.base import Tool, ToolParam, ToolResult, ToolSchema
from core.exception_policy import downgrade


#: 拼接后允许的最大字节（避免把磁盘写爆）
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024

#: 单次 ffmpeg 调用的超时
FFMPEG_TIMEOUT_S = 300


def find_ffmpeg() -> Optional[str]:
    """找一个可用的 ffmpeg 可执行文件。"""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return str(exe)
    except Exception as exc:
        downgrade(exc, where='tools/video.py')
    return shutil.which("ffmpeg")


def ffmpeg_hint() -> str:
    return (
        "没找到 ffmpeg。任选一种装上：\n"
        "  - pip install imageio-ffmpeg   （推荐，自带二进制，不动系统 PATH）\n"
        "  - winget install Gyan.FFmpeg\n"
        "  - 或把 ffmpeg.exe 放进 PATH"
    )


# ============================================================
# MP4 容器解析（不需要 ffprobe）
# ============================================================

def _iter_boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8].decode("latin-1", "replace")
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            body = pos + 16
        elif size == 0:
            body, size = pos + 8, end - pos
        else:
            body = pos + 8
        if size < 8:
            break
        yield typ, body, min(pos + size, end)
        pos += size


def parse_mp4(path: Path) -> Dict[str, Any]:
    """读出时长(秒)与分辨率。解析不了就返回 None 字段，不抛异常。"""
    info: Dict[str, Any] = {"duration_s": None, "width": None, "height": None}
    try:
        data = path.read_bytes()
    except Exception:
        return info

    containers = {"moov", "trak", "mdia", "minf", "stbl"}

    def walk(start: int, end: int) -> None:
        for typ, body, box_end in _iter_boxes(data, start, end):
            if typ in containers:
                walk(body, box_end)
            elif typ == "mvhd":
                version = data[body]
                try:
                    if version == 0:
                        ts, dur = struct.unpack(">II", data[body + 12:body + 20])
                    else:
                        ts = struct.unpack(">I", data[body + 20:body + 24])[0]
                        dur = struct.unpack(">Q", data[body + 24:body + 32])[0]
                    if ts:
                        info["duration_s"] = round(dur / ts, 2)
                except Exception as exc:
                    downgrade(exc, where='tools/video.py')
            elif typ == "tkhd":
                version = data[body]
                off = body + (76 if version == 0 else 88)
                try:
                    w, h = struct.unpack(">II", data[off:off + 8])
                    if w and h and not info["width"]:
                        info["width"], info["height"] = w >> 16, h >> 16
                except Exception as exc:
                    downgrade(exc, where='tools/video.py')
    try:
        walk(0, len(data))
    except Exception as exc:
        downgrade(exc, where='tools/video.py')
    return info


# ============================================================
# 公共：路径安全 + 输出目录
# ============================================================

def _authorized_roots() -> List[Path]:
    try:
        from tools.artifact_publish import _authorized_roots as roots

        return roots()
    except Exception:
        return []


def _resolve_input(raw: str) -> Optional[Path]:
    """把用户给的输入路径解析成授权目录内的真实文件。"""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return None
    candidate = Path(text)
    roots = _authorized_roots()

    if candidate.is_file():
        resolved = candidate.resolve()
    else:
        resolved = None
        for root in roots:
            probe = (root / text).resolve()
            if probe.is_file():
                resolved = probe
                break
    if resolved is None:
        return None
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return resolved
        except Exception:
            continue
    return None


def _output_path(name: str) -> Optional[Path]:
    """输出一律落在产物区的 output/ 下（这样界面能直接下载）。"""
    from tools.script_writer import workspace_dir

    base = (name or "").strip().replace("\\", "/").split("/")[-1]
    if not base or base in (".", ".."):
        return None
    if not base.lower().endswith((".mp4", ".mov", ".mkv", ".webm")):
        base += ".mp4"
    dest = Path(workspace_dir()) / "output" / base
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


# ============================================================
# 工具：探测
# ============================================================

class VideoProbeTool(Tool):
    name = "video_probe"
    description = (
        "读取视频文件的时长与分辨率（纯 Python 解析 MP4 容器，不依赖 ffprobe）。\n"
        "拼接之前先用它确认：各段时长加起来对不对、分辨率是否一致。"
    )

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[ToolParam(
                name="path", type="string", required=True,
                description="视频文件路径（授权目录内）",
            )],
            returns={"duration_s": "时长(秒)", "width": "宽", "height": "高", "size": "字节"},
        )

    async def execute(self, path: str = "") -> ToolResult:
        target = _resolve_input(path)
        if target is None:
            return ToolResult(success=False, error=f"文件不存在或不在授权目录内：{path!r}")
        info = parse_mp4(target)
        info["size"] = target.stat().st_size
        info["path"] = str(target)
        return ToolResult(success=True, data=info)


# ============================================================
# 工具：拼接
# ============================================================

class VideoConcatTool(Tool):
    name = "video_concat"
    description = (
        "把多个视频**按顺序拼接**成一个（调用 ffmpeg，无 shell，参数受控）。\n"
        "先用 video_probe 确认各段分辨率/编码一致，再做拼接。\n"
        "分辨率不一致时会自动重编码到第一段的分辨率（慢一些，但不会失败）。\n"
        "输出文件落在产物区 output/ 下，可以直接下载。"
    )

    def __init__(self):
        self.logger = get_logger()

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(
                    name="inputs", type="array", required=True,
                    description="按顺序排列的视频路径列表（至少 2 个）",
                ),
                ToolParam(
                    name="output", type="string", required=False,
                    description="输出文件名，如 final.mp4（默认 merged.mp4）",
                ),
                ToolParam(
                    name="reencode", type="boolean", required=False, default=False,
                    description="强制重编码（默认先试无损快速拼接，失败才重编码）",
                ),
            ],
            returns={"url": "下载地址", "path": "产物区相对路径", "duration_s": "成片时长"},
        )

    async def execute(
        self,
        inputs: Optional[List[str]] = None,
        output: str = "merged.mp4",
        reencode: bool = False,
    ) -> ToolResult:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return ToolResult(success=False, error=ffmpeg_hint())

        if not isinstance(inputs, list) or len(inputs) < 2:
            return ToolResult(
                success=False,
                error="inputs 至少要有 2 个视频路径，按想要的顺序排列",
            )

        sources: List[Path] = []
        for raw in inputs:
            resolved = _resolve_input(str(raw))
            if resolved is None:
                return ToolResult(
                    success=False,
                    error=f"找不到输入：{raw!r}（或不在授权目录内）",
                )
            sources.append(resolved)

        dest = _output_path(output)
        if dest is None:
            return ToolResult(success=False, error=f"输出文件名不合法：{output!r}")

        # 先探一下，方便判断要不要重编码
        probes = [parse_mp4(p) for p in sources]
        sizes = {(p["width"], p["height"]) for p in probes if p["width"]}
        mismatch = len(sizes) > 1

        ok, err = await self._run_concat(ffmpeg, sources, dest, reencode or mismatch)
        if not ok:
            return ToolResult(success=False, error=err)

        if not dest.is_file() or dest.stat().st_size == 0:
            return ToolResult(success=False, error="ffmpeg 没有产出文件（检查输入是否有效）")
        if dest.stat().st_size > MAX_OUTPUT_BYTES:
            return ToolResult(success=False, error="输出文件超过 2GB 上限")

        info = parse_mp4(dest)
        rel = f"output/{dest.name}"
        self.logger.info(
            "VideoConcat",
            f"拼接完成 {len(sources)} 段 -> {rel} ({dest.stat().st_size} B, "
            f"{info['duration_s']}s)",
        )

        expected = sum(p["duration_s"] or 0 for p in probes)
        return ToolResult(
            success=True,
            data={
                "name": dest.name,
                "path": rel,
                "abs_path": str(dest),
                "size": dest.stat().st_size,
                "duration_s": info["duration_s"],
                "input_duration_s": round(expected, 2),
                "width": info["width"],
                "height": info["height"],
                "segments": len(sources),
                "url": f"/api/artifacts/{rel}",
                "artifacts": [{"name": dest.name, "path": rel, "size": dest.stat().st_size}],
            },
        )

    async def _run_concat(
        self, ffmpeg: str, sources: List[Path], dest: Path, reencode: bool
    ) -> tuple:
        """跑 ffmpeg。先试 `-c copy`（快），失败或要求重编码时再走重编码。"""
        list_file = Path(tempfile.mkdtemp(prefix="concat_")) / "list.txt"
        # concat demuxer 的清单格式；单引号要转义
        lines = []
        for p in sources:
            safe = str(p).replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        base = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
        ]

        attempts = []
        if not reencode:
            attempts.append(base + ["-c", "copy", str(dest)])
        # 重编码：统一到第一段的尺寸，像素格式/音频也统一，兼容性最稳
        first = parse_mp4(sources[0])
        vf = []
        if first.get("width") and first.get("height"):
            vf = ["-vf", f"scale={first['width']}:{first['height']}:force_original_aspect_ratio=decrease,pad={first['width']}:{first['height']}:(ow-iw)/2:(oh-ih)/2"]
        attempts.append(base + [
            *vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            str(dest),
        ])

        last_err = ""
        for cmd in attempts:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=FFMPEG_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                return False, f"ffmpeg 超时（{FFMPEG_TIMEOUT_S}s）"
            except Exception as exc:
                return False, f"调用 ffmpeg 失败：{exc}"

            if proc.returncode == 0:
                return True, ""
            last_err = (stderr or b"").decode("utf-8", "replace")[-800:]
            self.logger.warning("VideoConcat", f"拼接尝试失败，换重编码：{last_err[:200]}")

        return False, f"ffmpeg 拼接失败：{last_err}"
