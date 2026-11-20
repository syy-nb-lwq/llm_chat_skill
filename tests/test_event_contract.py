"""事件契约测试 —— 防止"改了名字，另一端静默失效"。

四道闸：

1. **后端**：`emit()` / `push_event()` / `on_event()` / `_emit()` 里出现的事件名字面量，
   必须在 `core/events.py` 里；
2. **前端**：`wsService.on()` / `wsService.off()` / `this._emit()` 里的事件名字面量，
   必须在事件契约里；
3. **前端常量存在性**：`EV.XXX` 引用的键必须真实存在 —— JS 里 `on(undefined)`
   不会报错，只会让监听永远不触发，这是比写错字符串更隐蔽的失效；
4. **同源**：`events.js` 必须等于用 `core/events.py` 重新生成的结果（禁止手改）。

配套自测（`test_patterns_detect_literals`）保证上面几条真的能抓到东西 ——
否则全部改成常量后扫描结果为空，"通过"就变成了假象。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import events as ev_mod

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIRS = ["core", "agents", "tools", "backend"]
FRONTEND_SRC = ROOT / "frontend" / "src"
EVENTS_JS = FRONTEND_SRC / "events.js"

_Q = r"(?P<q>['\"])"
_NAME = r"(?P<name>[a-z_]+)"

# 后端五种发射形态，统一用具名组 q / name
BACKEND_PATTERNS = [
    re.compile(r"(?<![\w.])emit\(\s*" + _Q + _NAME + r"(?P=q)"),
    re.compile(r"(?<![\w])_emit\(\s*" + _Q + _NAME + r"(?P=q)"),
    re.compile(r"_emit\(on_event,\s*" + _Q + _NAME + r"(?P=q)"),
    re.compile(r"(?<![\w.])on_event\(\s*" + _Q + _NAME + r"(?P=q)"),
    re.compile(r"push_event\([^,]+,\s*" + _Q + _NAME + r"(?P=q)"),
]

FRONTEND_PATTERNS = [
    re.compile(r"wsService\.(?:on|off)\(\s*" + _Q + _NAME + r"(?P=q)"),
    re.compile(r"this\._emit\(\s*" + _Q + _NAME + r"(?P=q)"),
]

#: 前端 EV 常量引用（用于检查常量是否真的存在）
FRONTEND_CONST_RE = re.compile(r"\bEV\.(?P<name>[A-Z][A-Z0-9_]*)\b")


def _iter_backend_files():
    for d in BACKEND_DIRS:
        for path in (ROOT / d).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _iter_frontend_files():
    for path in FRONTEND_SRC.rglob("*"):
        if path.suffix in (".js", ".vue"):
            yield path


def _literal_backend_events():
    found = []
    for path in _iter_backend_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for pattern in BACKEND_PATTERNS:
                for m in pattern.finditer(line):
                    found.append((path.relative_to(ROOT).as_posix(), lineno, m.group("name")))
    return found


def _literal_frontend_events():
    found = []
    for path in _iter_frontend_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            for pattern in FRONTEND_PATTERNS:
                for m in pattern.finditer(line):
                    found.append((path.relative_to(ROOT).as_posix(), lineno, m.group("name")))
    return found


def _frontend_const_refs():
    found = []
    for path in _iter_frontend_files():
        if path.name == "events.js":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            for m in FRONTEND_CONST_RE.finditer(line):
                found.append((path.relative_to(ROOT).as_posix(), lineno, m.group("name")))
    return found


# ===== 自测：保证扫描器不是空转 =====

def test_patterns_detect_literals():
    samples = [
        'emit("thinking", {})',
        "emit('plan', {})",
        'self._emit("goal_round", {})',
        'await self._emit(on_event, "subagent_done", task)',
        'on_event("tool_result", {})',
        'push_event(conn_id, "connected", {})',
    ]
    hits = []
    for line in samples:
        for pattern in BACKEND_PATTERNS:
            for m in pattern.finditer(line):
                hits.append(m.group("name"))
    assert hits == ["thinking", "plan", "goal_round", "subagent_done",
                    "tool_result", "connected"], hits

    fe_samples = ["wsService.on('thinking', cb)", 'this._emit("log", p)']
    fe_hits = []
    for line in fe_samples:
        for pattern in FRONTEND_PATTERNS:
            for m in pattern.finditer(line):
                fe_hits.append(m.group("name"))
    assert fe_hits == ["thinking", "log"], fe_hits


# ===== 四道闸 =====

def test_backend_event_literals_are_declared():
    unknown = [item for item in _literal_backend_events() if item[2] not in ev_mod.ALL_EVENTS]
    assert not unknown, (
        "以下后端事件名不在 core/events.py 中，请改用常量：\n"
        + "\n".join(f"  {f}:{n}  {name}" for f, n, name in unknown)
    )


def test_frontend_event_literals_are_declared():
    unknown = [item for item in _literal_frontend_events() if item[2] not in ev_mod.ALL_EVENTS]
    assert not unknown, (
        "以下前端事件名不在事件契约中：\n"
        + "\n".join(f"  {f}:{n}  {name}" for f, n, name in unknown)
    )


def test_frontend_ev_constants_exist():
    """`EV.XXX` 写错不会报错，只会让 on() 变成 on(undefined) —— 必须静态兜住。"""
    known = {name[len("EV_"):] for name in ev_mod.EVENT_CONSTANTS}
    unknown = [item for item in _frontend_const_refs() if item[2] not in known]
    assert not unknown, (
        "前端引用了不存在的事件常量（会造成监听静默失效）：\n"
        + "\n".join(f"  {f}:{n}  EV.{name}" for f, n, name in unknown)
    )


def test_events_js_is_in_sync_with_python():
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import gen_events_js
    finally:
        sys.path.pop(0)

    assert EVENTS_JS.exists(), "缺少 frontend/src/events.js，请运行 scripts/gen_events_js.py"
    expected = gen_events_js.render()
    assert EVENTS_JS.read_text(encoding="utf-8") == expected, (
        "frontend/src/events.js 与 core/events.py 不一致，请重新运行 "
        "`python scripts/gen_events_js.py`"
    )


def test_stream_and_audit_sets_are_disjoint():
    overlap = ev_mod.STREAM_EVENTS & ev_mod.AUDIT_EVENTS
    assert not overlap, f"事件同时属于流事件与审计事件：{sorted(overlap)}"


@pytest.mark.parametrize("name", ["thinking", "message_delta", "message_final", "error"])
def test_core_conversation_events_exist(name):
    """主干事件不能被误删 —— 这些是前端渲染对话的最小集合。"""
    assert name in ev_mod.STREAM_EVENTS
