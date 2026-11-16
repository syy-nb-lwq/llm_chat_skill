"""M3-03 / M3-05 / M3-06 结果校验与自动发布收紧测试。

覆盖:
- 回归样例从 YAML 加载
- ResultValidator:通过 / 工具没被调用 / 输出缺内容 / 字段缺失
- 风险等级:无回归样例 → high,全通过 → low,部分失败 → medium
- M3-06:高置信度补丁在缺少回归样例时被拦下,不落盘
- M3-06:回归样例通过才自动发布,并写审计记录
- 静态校验只拦"新引入"的问题,基线老问题不阻断补丁
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===== 测试替身 =====


class EchoTool:
    """一个可控的假工具,避免依赖真实网络。"""

    name = "echo_tool"
    description = "回显输入"

    def __init__(self, fail: bool = False, payload=None):
        self.fail = fail
        self.payload = payload or {"echo": "ok", "code": 200}
        self.calls = []

    def schema(self):
        from tools.base import ToolParam, ToolSchema

        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[ToolParam(name="text", type="string", description="文本", required=False)],
        )

    def execute(self, **kwargs):
        from tools.base import ToolResult

        self.calls.append(kwargs)
        if self.fail:
            return ToolResult(success=False, error="boom")
        return ToolResult(success=True, data=dict(self.payload))


@pytest.fixture
def echo_tool():
    from tools.hub import get_tool_hub

    tool = EchoTool()
    hub = get_tool_hub()
    try:
        hub.register_python_tool(tool)
    except ValueError:
        pass
    yield tool
    hub.unregister_tool(tool.name)


def _skill_with_regression(regression, tool="echo_tool"):
    from skills.models import Skill, SkillStep

    return Skill(
        name="demo_skill",
        version="1.0.1",
        capability="演示技能的能力描述",
        method="演示方法论",
        patterns=["demo"],
        examples=["示例"],
        steps=[
            SkillStep(
                id="s1",
                name="调用工具",
                tool=tool,
                params={"text": "${user_input.text}"},
            )
        ],
        regression=regression,
    )


# ===== 加载 =====


def test_regression_loaded_from_yaml(tmp_path):
    from skills.loader import SkillLoader

    path = tmp_path / "demo@1.0.0.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "demo",
                "version": "1.0.0",
                "capability": "cap",
                "method": "method",
                "patterns": ["demo"],
                "examples": ["ex"],
                "regression": [
                    {
                        "name": "case-1",
                        "input": {"user_input": "北京"},
                        "expect": {"tool": "echo_tool", "success": True},
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    skill = SkillLoader(tmp_path).load_all()[0]
    assert len(skill.regression) == 1
    assert skill.regression[0]["name"] == "case-1"
    assert skill.regression[0]["expect"]["tool"] == "echo_tool"


def test_regression_accepts_plain_string(tmp_path):
    from skills.loader import SkillLoader

    path = tmp_path / "demo@1.0.0.yaml"
    path.write_text(
        "name: demo\nversion: 1.0.0\ncapability: cap\nmethod: method\n"
        "patterns: [demo]\nexamples: [ex]\nregression:\n  - 只有名字\n",
        encoding="utf-8",
    )
    skill = SkillLoader(tmp_path).load_all()[0]
    assert skill.regression == [{"name": "只有名字", "input": {}, "expect": {}}]


# ===== ResultValidator =====


@pytest.mark.asyncio
async def test_validator_passes_when_expectations_met(echo_tool):
    from core.result_validator import ResultValidator

    skill = _skill_with_regression([
        {
            "name": "正常",
            "input": {"user_input": "北京"},
            "expect": {"tool": "echo_tool", "success": True, "contains": ["ok"], "output_keys": ["code"]},
        }
    ])
    report = await ResultValidator().validate(skill)
    assert report.total == 1
    assert report.passed == 1
    assert report.ok is True
    assert report.risk_level == "low"


@pytest.mark.asyncio
async def test_validator_fails_when_tool_not_called(echo_tool):
    from core.result_validator import ResultValidator

    skill = _skill_with_regression([
        {"name": "错工具", "input": {"user_input": "x"}, "expect": {"tool": "other_tool"}}
    ])
    report = await ResultValidator().validate(skill)
    assert report.ok is False
    assert report.risk_level == "medium"
    assert "期望调用工具" in report.cases[0].reason


@pytest.mark.asyncio
async def test_validator_fails_when_output_missing(echo_tool):
    from core.result_validator import ResultValidator

    skill = _skill_with_regression([
        {"name": "缺内容", "input": {"user_input": "x"}, "expect": {"contains": ["不存在的内容"]}}
    ])
    report = await ResultValidator().validate(skill)
    assert report.ok is False
    assert "缺少预期内容" in report.cases[0].reason


@pytest.mark.asyncio
async def test_validator_fails_when_key_missing(echo_tool):
    from core.result_validator import ResultValidator

    skill = _skill_with_regression([
        {"name": "缺字段", "input": {"user_input": "x"}, "expect": {"output_keys": ["no_such_key"]}}
    ])
    report = await ResultValidator().validate(skill)
    assert report.ok is False
    assert "缺少预期字段" in report.cases[0].reason


@pytest.mark.asyncio
async def test_validator_without_regression_is_high_risk(echo_tool):
    from core.result_validator import ResultValidator

    skill = _skill_with_regression([])
    report = await ResultValidator().validate(skill)
    assert report.has_regression is False
    assert report.risk_level == "high"
    assert report.ok is False


@pytest.mark.asyncio
async def test_validator_reports_tool_failure(echo_tool):
    from core.result_validator import ResultValidator
    from tools.hub import get_tool_hub

    hub = get_tool_hub()
    # 换成会失败的同名工具
    hub.unregister_tool(echo_tool.name)
    failing = EchoTool(fail=True)
    hub.register_python_tool(failing)

    try:
        skill = _skill_with_regression(
            [{"name": "失败", "input": {"user_input": "x"}, "expect": {"success": True}}]
        )
        report = await ResultValidator().validate(skill)
        assert report.ok is False
        assert report.cases[0].failed_tools
    finally:
        hub.unregister_tool(failing.name)
        hub.register_python_tool(echo_tool)


# ===== 静态校验分级 =====


def test_split_issues_separates_soft_issues():
    from skills.validator import split_issues

    errors, warnings = split_issues([
        "capability 为空或太短(<5 字符)",
        "缺少 examples(教学证据为空,无回归样例)",
    ])
    assert errors == ["capability 为空或太短(<5 字符)"]
    assert warnings == ["缺少 examples(教学证据为空,无回归样例)"]


def test_new_issues_only_reports_introduced_problems():
    from skills.validator import new_issues

    baseline = ["capability 为空或太短(<5 字符)"]
    candidate = ["capability 为空或太短(<5 字符)", "step s1 引用未知工具 foo"]
    assert new_issues(baseline, candidate) == ["step s1 引用未知工具 foo"]


def test_validate_skill_checks_regression_structure():
    from skills.models import Skill
    from skills.validator import validate_skill

    skill = Skill(
        name="demo_skill",
        capability="cap",
        method="method",
        examples=["ex"],
        regression=[{"name": "", "expect": {"tool": "not_exist"}}],
    )
    issues = validate_skill(skill, ["echo_tool"])
    assert any("缺少 name" in i for i in issues)
    assert any("期望未知工具" in i for i in issues)


# ===== M3-06 自动发布收紧 =====


@pytest.fixture
def skill_store(tmp_path):
    """隔离的 SkillStore,内置一个带回归样例的 demo 技能。"""
    import skills.manager as mgr

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo@1.0.0.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "demo",
                "version": "1.0.0",
                "capability": "演示用能力描述",
                "method": "old method",
                "patterns": ["demo"],
                "examples": ["旧样例"],
                "steps": [
                    {"id": "s1", "name": "调用", "tool": "echo_tool", "params": {"text": "${user_input.text}"}}
                ],
                "regression": [
                    {
                        "name": "case-1",
                        "input": {"user_input": "北京"},
                        "expect": {"tool": "echo_tool", "success": True},
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    original = mgr._store
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        yield mgr._store, skills_dir
    finally:
        mgr._store = original


@pytest.mark.asyncio
async def test_auto_publish_blocked_without_regression(skill_store, echo_tool, tmp_path):
    """没有回归样例 → 不允许自动发布,补丁降级为人工审批。"""
    import core.critic as critic_mod
    import skills.manager as mgr
    from core.memory import SkillPatch

    store, skills_dir = skill_store
    # 去掉回归样例
    path = skills_dir / "demo@1.0.0.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("regression", None)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    store.reload()

    critic = critic_mod.ExecutionCritic.__new__(critic_mod.ExecutionCritic)
    critic.memory = _FakeMemory(tmp_path)
    critic.logger = mgr.get_logger() if hasattr(mgr, "get_logger") else critic_mod.get_logger()

    patch = SkillPatch(
        id="p1",
        trace_id="t1",
        timestamp="2026-01-01T00:00:00",
        target_skill="demo",
        patch_type="improve_skill",
        diagnosis="d",
        suggestion={"type": "improve_skill", "target_skill": "demo", "method": "new method"},
        confidence=0.95,
    )
    applied, report, reason = await critic._try_auto_publish(patch)
    assert applied is False
    assert "回归样例" in reason
    # 没有落盘
    assert not (skills_dir / "demo@1.0.1.yaml").exists()


@pytest.mark.asyncio
async def test_auto_publish_succeeds_with_passing_regression(skill_store, echo_tool, tmp_path):
    import core.critic as critic_mod
    from core.memory import SkillPatch

    store, skills_dir = skill_store

    critic = critic_mod.ExecutionCritic.__new__(critic_mod.ExecutionCritic)
    critic.memory = _FakeMemory(tmp_path)
    critic.logger = critic_mod.get_logger()

    patch = SkillPatch(
        id="p2",
        trace_id="t2",
        timestamp="2026-01-01T00:00:00",
        target_skill="demo",
        patch_type="improve_skill",
        diagnosis="d",
        suggestion={"type": "improve_skill", "target_skill": "demo", "method": "new method"},
        confidence=0.95,
    )
    applied, report, reason = await critic._try_auto_publish(patch)
    assert applied is True, reason
    assert report is not None and report.ok is True
    # 新版本已落盘
    assert (skills_dir / "demo@1.0.1.yaml").exists()
    # 审计留痕
    audits = list((tmp_path / "memory" / "audit").glob("auto_publish_*.json"))
    assert audits, "自动发布必须写审计记录"


@pytest.mark.asyncio
async def test_auto_publish_blocked_when_regression_fails(skill_store, echo_tool, tmp_path):
    import core.critic as critic_mod
    from core.memory import SkillPatch

    store, skills_dir = skill_store
    # 把回归样例改成不可能通过
    path = skills_dir / "demo@1.0.0.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["regression"][0]["expect"]["contains"] = ["绝对不会出现的内容"]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    store.reload()

    critic = critic_mod.ExecutionCritic.__new__(critic_mod.ExecutionCritic)
    critic.memory = _FakeMemory(tmp_path)
    critic.logger = critic_mod.get_logger()

    patch = SkillPatch(
        id="p3",
        trace_id="t3",
        timestamp="2026-01-01T00:00:00",
        target_skill="demo",
        patch_type="improve_skill",
        diagnosis="d",
        suggestion={"type": "improve_skill", "target_skill": "demo", "method": "new method"},
        confidence=0.95,
    )
    applied, report, reason = await critic._try_auto_publish(patch)
    assert applied is False
    assert "回归样例未通过" in reason
    assert not (skills_dir / "demo@1.0.1.yaml").exists()


class _FakeMemory:
    """只提供 write_audit 的最小 MemoryStore 替身。"""

    def __init__(self, base_path):
        from core.memory import MemoryStore

        self._real = MemoryStore(base_path=base_path / "memory")

    def write_audit(self, kind, payload):
        return self._real.write_audit(kind, payload)
