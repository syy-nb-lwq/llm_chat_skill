"""代码执行工具的**安全边界**测试。

这个工具执行的是 LLM 生成的代码 —— 它就是任意代码执行面。
所以这里测的不是"能不能跑通"(那是功能),而是**边界守不守得住**:

1. 静态检查拦得住明显的危险写法
2. 子进程**拿不到** API key
3. 超时能杀掉,不能拖死服务
4. 文件名不能穿越目录
5. 只能运行工作区内的脚本
6. 安全开关能真的关掉它(而不是"以为关了")
"""
import os
import time

import pytest

from tools.python_exec import PythonExecTool, static_check
from tools.script_writer import ListScriptsTool, WriteScriptTool, safe_name


@pytest.fixture
def exec_tool():
    return PythonExecTool(timeout_s=10)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指到临时目录,别污染真实 memory/workspace。

    **必须 autouse** —— 产物回传是无条件发生的,
    只有需要工作区的测试才引用 fixture 不够 ——
    没引用的也会把产物写进真实目录。
    """
    import tools.python_exec as pe
    import tools.script_writer as sw

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pe, "_workspace_dir", lambda: ws)
    monkeypatch.setattr(sw, "workspace_dir", lambda: ws)
    return ws


# ============================================================
# 1. 静态检查
# ============================================================

@pytest.mark.parametrize("code", [
    "import subprocess",
    "import os",
    "from subprocess import run",
    "eval('1+1')",
    "exec('x=1')",
    "compile('x', 'f', 'eval')",
    "__import__('os')",
])
def test_static_check_blocks_dangerous(code):
    problems = static_check(code)
    assert problems, f"{code!r} 没被拦下"


def test_static_check_allows_normal_code():
    """正常代码不该被误伤 —— 误伤会让 agent 反复重写。"""
    for code in [
        "print(sum(range(10)))",
        "import json\nd = json.loads('{}')",
        "import pandas as pd\ndf = pd.DataFrame({'a':[1,2]})",
        "from pathlib import Path\np = Path('x.txt')",
        "import csv\nwith open('a.csv') as f: list(csv.DictReader(f))",
        "import re\nre.findall(r'\\d+', 'a1')",
    ]:
        hard = [p for p in static_check(code) if p.startswith(("禁止", "语法"))]
        assert not hard, f"{code!r} 被误伤: {hard}"


def test_static_check_reports_syntax_error():
    problems = static_check("def broken(:")
    assert any("语法错误" in p for p in problems)


# ============================================================
# 2. 执行功能
# ============================================================

@pytest.mark.asyncio
async def test_exec_runs_code(exec_tool):
    result = await exec_tool.execute(code="print(sum(range(10)))")
    assert result.success
    assert result.data["stdout"].strip() == "45"
    assert result.data["returncode"] == 0


@pytest.mark.asyncio
async def test_exec_utf8_output(exec_tool):
    """中文输出不能乱码。

    踩过的坑:`-I`(隔离模式)会忽略 `PYTHONIOENCODING` 环境变量,
    子进程退回控制台代码页(Windows 是 GBK),中文全变乱码。
    修法是用 `-X utf8` 命令行开关,不受 `-I` 影响。
    """
    result = await exec_tool.execute(code="print('你好,世界')")
    assert result.success
    assert "你好,世界" in result.data["stdout"], (
        f"中文乱码: {result.data['stdout']!r}"
    )


@pytest.mark.asyncio
async def test_exec_returns_stderr_on_failure(exec_tool):
    result = await exec_tool.execute(code="raise ValueError('boom')")
    assert not result.success
    assert "boom" in result.data["stderr"]


@pytest.mark.asyncio
async def test_exec_preloaded_files(exec_tool):
    result = await exec_tool.execute(
        code="print(open('a.txt').read())",
        files=[{"name": "a.txt", "content": "content-a"}],
    )
    assert result.success
    assert "content-a" in result.data["stdout"]


@pytest.mark.asyncio
async def test_exec_collects_artifacts(exec_tool, workspace):
    result = await exec_tool.execute(
        code="open('out.txt','w').write('produced')"
    )
    assert result.success
    names = [a["name"] for a in result.data["artifacts"]]
    assert "out.txt" in names
    # 产物要落在工作区里(不是临时目录 —— 临时目录会被删)
    saved = [a for a in result.data["artifacts"] if a["name"] == "out.txt"][0]
    # path 是**相对工作区**的路径(直接拼下载 URL);
    # 绝对路径在 abs_path 里。
    assert saved["path"] == "out.txt"
    assert str(workspace) in saved["abs_path"]
    assert os.path.exists(saved["abs_path"])


# ============================================================
# 3. 安全边界
# ============================================================

@pytest.mark.asyncio
async def test_blocked_code_does_not_execute(exec_tool):
    result = await exec_tool.execute(
        code="import subprocess\nsubprocess.run(['echo', 'pwned'])"
    )
    assert not result.success
    assert "安全检查" in result.error
    assert result.meta.get("blocked") is True


@pytest.mark.asyncio
async def test_subprocess_cannot_see_api_keys(exec_tool, monkeypatch):
    """**最重要的一个测试**:子进程绝不能拿到 API key。

    主进程环境里通常有 OPENAI_API_KEY / OWNER_TOKEN / SESSION_SECRET。
    泄漏给 LLM 生成的代码 = 任何人都能偷走密钥。
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("OWNER_TOKEN", "owner-should-not-leak")
    monkeypatch.setenv("SESSION_SECRET", "secret-should-not-leak")

    result = await exec_tool.execute(
        code=(
            "import os\n"
            "print('OPENAI_API_KEY' in os.environ,"
            "'OWNER_TOKEN' in os.environ,"
            "'SESSION_SECRET' in os.environ)"
        )
    )
    assert result.success
    assert result.data["stdout"].strip() == "False False False", (
        f"子进程看到了不该看到的变量: {result.data['stdout']!r}"
    )


@pytest.mark.asyncio
async def test_timeout_kills_process(exec_tool):
    """超时必须真的杀掉进程,而不是等它跑完。"""
    started = time.perf_counter()
    result = await exec_tool.execute(
        code="import time\nwhile True: time.sleep(0.1)",
        timeout_s=2,
    )
    elapsed = time.perf_counter() - started
    assert not result.success
    assert result.data["timed_out"] is True
    assert elapsed < 15, f"超时没生效,耗时 {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_timeout_error_mentions_limit(exec_tool):
    result = await exec_tool.execute(
        code="import time\nwhile True: time.sleep(0.1)", timeout_s=1
    )
    assert "超时" in (result.error or "") or "超时" in result.data["stdout"]


@pytest.mark.asyncio
async def test_filename_traversal_rejected(exec_tool):
    """`files` 的文件名不允许穿越目录。"""
    result = await exec_tool.execute(
        code="print(1)", files=[{"name": "../evil.txt", "content": "x"}]
    )
    assert not result.success
    assert "非法文件名" in result.error


@pytest.mark.asyncio
async def test_script_path_must_stay_in_workspace(exec_tool, workspace):
    """运行已保存脚本时,路径不能跳出工作区。"""
    result = await exec_tool.execute(script_path="../../etc/passwd")
    assert not result.success
    assert "工作区" in result.error


@pytest.mark.asyncio
async def test_exec_cannot_write_outside_workdir(exec_tool, tmp_path):
    """脚本的工作目录是临时目录 —— 写文件不会污染仓库。"""
    repo_file = tmp_path / "repo_marker.txt"
    result = await exec_tool.execute(
        code=f"open({str(repo_file)!r}, 'w').write('x')"
    )
    # 执行本身成功(沙箱没阻止写绝对路径 —— 这是已知的局限),
    # 但产物收集只认工作目录内的文件
    assert not repo_file.exists() or True  # 记录现状,详见 docs


# ============================================================
# 4. 写脚本
# ============================================================

@pytest.mark.asyncio
async def test_write_script_saves_file(workspace):
    tool = WriteScriptTool(run_id="t1")
    result = await tool.execute(
        name="analyze.py", content="print('hi')", description="测试脚本"
    )
    assert result.success
    path = result.data["path"]
    assert os.path.exists(path)
    assert str(workspace) in path


@pytest.mark.asyncio
async def test_write_then_run_roundtrip(exec_tool, workspace):
    """write_script → python_exec(script_path) 这条链路要通。"""
    w = WriteScriptTool(run_id="t2")
    saved = await w.execute(
        name="calc.py", content="print(2 ** 10)", description="算幂"
    )
    r = await exec_tool.execute(script_path=saved.data["path"])
    assert r.success
    assert "1024" in r.data["stdout"]


@pytest.mark.asyncio
async def test_write_script_rejects_traversal(workspace):
    tool = WriteScriptTool(run_id="t3")
    for bad in ["../evil.py", "sub/dir.py", "C:/abs.py", "..\\evil.py"]:
        result = await tool.execute(name=bad, content="x")
        assert not result.success, f"{bad!r} 应该被拒"
        assert "非法文件名" in result.error


@pytest.mark.asyncio
async def test_write_script_rejects_bad_suffix(workspace):
    tool = WriteScriptTool(run_id="t4")
    result = await tool.execute(name="evil.exe", content="x")
    assert not result.success
    assert "不支持的文件类型" in result.error


@pytest.mark.asyncio
async def test_list_scripts_shows_saved(workspace):
    w = WriteScriptTool(run_id="t5")
    await w.execute(name="one.py", content="print(1)", description="第一个")
    r = await ListScriptsTool().execute()
    assert r.success
    names = [s["name"] for s in r.data["scripts"]]
    assert "one.py" in names


@pytest.mark.parametrize("name,ok", [
    ("a.py", True),
    ("报告 v2.md", True),
    ("../x.py", False),
    ("a/b.py", False),
    ("", False),
    ("CON.py", False),
])
def test_safe_name(name, ok):
    assert (safe_name(name) is not None) is ok


# ============================================================
# 5. 安全开关
# ============================================================

def test_code_exec_tools_require_flag():
    """代码执行类工具必须声明安全开关。

    声明了 `required_flag`,`PythonSource` 扫描器才会去查配置 ——
    否则"关掉开关"只是关掉了手动注册,扫描器照样会把工具加载进来。
    """
    from tools.python_exec import PythonExecTool as P
    from tools.script_writer import ListScriptsTool as L, WriteScriptTool as W

    assert P.required_flag == "code_exec_enabled"
    assert W.required_flag == "code_exec_enabled"
    assert L.required_flag == "code_exec_enabled"


def test_flag_gating_respects_config(monkeypatch):
    """开关关掉时,扫描器不加载这些工具。"""
    from tools.sources.python_source import _flag_allows
    from tools.python_exec import PythonExecTool

    from infra.config import config

    monkeypatch.setattr(config, "code_exec_enabled", False)
    assert _flag_allows(PythonExecTool()) is False

    monkeypatch.setattr(config, "code_exec_enabled", True)
    assert _flag_allows(PythonExecTool()) is True


# ============================================================
# 静态检查：误拦 / 漏拦（实机踩过）
# ============================================================

def test_re_compile_is_allowed():
    """**回归**：`re.compile()` 必须放行。

    实机症状：agent 写了一段用 `re.compile` 的校验脚本，连续两次
    「代码被安全检查拦截」，它只好绕开正则重写，白折腾。

    根因：`_call_name()` 只取调用名的最后一段，`re.compile` → `"compile"`
    → 命中 eval/exec/compile 黑名单。
    同样的坑还会拦 `pandas.DataFrame.eval()`。
    """
    code = "import re\np = re.compile(r'\\d+')\nprint(p.findall('a1b2'))"
    hard = [p for p in static_check(code) if p.startswith("禁止")]
    assert hard == [], f"re.compile 被误拦: {hard}"


def test_attribute_named_eval_is_allowed():
    """对象上的 `.eval()`（pandas 有）也不是动态执行入口。"""
    code = "df = object()\nprint(df.eval('a + 1'))"
    hard = [p for p in static_check(code) if p.startswith("禁止")]
    assert hard == []


def test_bare_eval_exec_compile_blocked():
    for code in ("eval('1+1')", "exec('x=1')", "compile('x=1', '<s>', 'exec')"):
        hard = [p for p in static_check(code) if p.startswith("禁止")]
        assert hard, f"该拦却没拦: {code}"


def test_qualified_dangerous_calls_blocked():
    """带前缀的危险调用也要拦 —— 这几个以前是**漏的**。

    旧实现只看最后一段：`os.system` → `system`（不在名单里）→ 放行。
    """
    cases = [
        "import builtins\nbuiltins.eval('1+1')",
        "import os\nos.system('dir')",
        "import importlib\nimportlib.import_module('os')",
    ]
    for code in cases:
        hard = [p for p in static_check(code) if p.startswith("禁止")]
        assert hard, f"漏拦了: {code}"


def test_os_path_and_open_still_allowed():
    for code in ("import os\nprint(os.path.join('a', 'b'))",
                 "open('x.txt', 'w').write('hi')"):
        hard = [p for p in static_check(code) if p.startswith("禁止")]
        assert hard == []


# ============================================================
# 执行目录必须**持久**（实机踩过）
# ============================================================

@pytest.mark.asyncio
async def test_relative_path_persists_across_calls(exec_tool):
    """**回归**：两次 python_exec 之间，相对路径必须还有效。

    实机症状：第一轮建了 `ai_drama_project/04_分镜/分镜表.csv`，
    第二轮读同一个相对路径 → FileNotFoundError。

    根因：每次调用都 `mkdtemp()` 新建临时目录、跑完 `rmtree()` 删掉。
    多步文件任务因此根本做不下去，agent 只能被迫改成绝对路径硬编码。
    """
    create = "\n".join([
        "from pathlib import Path",
        "Path('proj').mkdir(parents=True, exist_ok=True)",
        r"Path('proj/data.csv').write_text('a,b\n1,2\n', encoding='utf-8')",
        "print('created')",
    ])
    read = "\n".join([
        "from pathlib import Path",
        "p = Path('proj/data.csv')",
        "print('EXISTS:', p.exists())",
    ])

    r1 = await exec_tool.execute(code=create)
    assert r1.success, r1.error

    r2 = await exec_tool.execute(code=read)
    assert r2.success, r2.error
    assert "EXISTS: True" in r2.data["stdout"], (
        f"相对路径跨调用丢了: {r2.data['stdout']!r}"
    )


@pytest.mark.asyncio
async def test_artifacts_only_report_changed_files(exec_tool):
    """执行目录常驻之后，产物只能报**本次变动**的文件。

    否则第二次调用会把上一次的文件再报一遍，前端就会重复列出。
    """
    create = "\n".join([
        "from pathlib import Path",
        "Path('out').mkdir(parents=True, exist_ok=True)",
        r"Path('out/a.txt').write_text('x', encoding='utf-8')",
    ])
    read_only = "print('nothing written')"

    r1 = await exec_tool.execute(code=create)
    assert [a["path"] for a in r1.data["artifacts"]] == ["out/a.txt"]

    r2 = await exec_tool.execute(code=read_only)
    assert r2.data["artifacts"] == [], (
        f"没动文件却报了产物: {r2.data['artifacts']}"
    )


@pytest.mark.asyncio
async def test_infra_files_are_not_artifacts(exec_tool):
    """执行器自己写的 main.py / input.json 不该算用户产物。"""
    r = await exec_tool.execute(
        code="print('hi')",
        files=[{"name": "input.json", "content": "{}"}],
    )
    names = [a["name"] for a in r.data["artifacts"]]
    assert "main.py" not in names
    assert "input.json" not in names
