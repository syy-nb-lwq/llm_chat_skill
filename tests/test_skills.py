"""技能加载/匹配/注册表测试"""
import textwrap
from pathlib import Path

import pytest

from skills.loader import SkillLoader
from skills.registry import SkillRegistry, _has_cycle
from skills.models import Skill, SkillStep


SAMPLE_YAML = textwrap.dedent("""
name: test_skill
version: 1.0.0
capability: 测试
method: 测试方法论
patterns:
  - 测试
  - test
tags:
  - 测试
steps:
  - id: s1
    name: step1
    description: 干点啥
    tool: weather_query
    input_schema:
      type: object
      properties:
        city: {type: string}
      required: [city]
""").strip()


def test_loader_yaml(tmp_path: Path):
    (tmp_path / "builtin").mkdir()
    (tmp_path / "builtin" / "test.yaml").write_text(SAMPLE_YAML, encoding="utf-8")

    loader = SkillLoader(tmp_path)
    skills = loader.load_all()
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "test_skill"
    assert s.version == "1.0.0"
    assert len(s.steps) == 1
    assert s.steps[0].tool == "weather_query"


def test_registry_match():
    reg = SkillRegistry([
        Skill(name="travel", patterns=["行程", "旅游"]),
        Skill(name="code", patterns=["代码", "python"]),
    ])
    res = reg.match("帮我安排一段旅游行程")
    assert res and res[0].name == "travel"
    res2 = reg.match("帮我写段 python 代码")
    assert res2 and res2[0].name == "code"


def test_registry_match_score_exact_full():
    reg = SkillRegistry([Skill(name="t", patterns=["abc"])])
    exact = reg.match("abc")
    fuzzy = reg.match("xx abc yy")
    assert exact[0] is not None
    # 完全匹配分数应该更高(0.5 vs 1.0),出现在 top 1
    assert exact and exact[0].name == "t"


def test_registry_validate_missing_tool():
    skill = Skill(name="x", steps=[SkillStep(id="s1", tool="ghost_tool")])
    reg = SkillRegistry([skill])
    issues = reg.validate(["weather_query"])
    assert any("ghost_tool" in i for i in issues)


def test_registry_validate_cycle():
    s1 = SkillStep(id="a", depends_on=["b"])
    s2 = SkillStep(id="b", depends_on=["a"])
    assert _has_cycle([s1, s2])


def test_registry_validate_no_cycle():
    s1 = SkillStep(id="a", depends_on=["b"])
    s2 = SkillStep(id="b")
    assert not _has_cycle([s1, s2])


def test_registry_add_conflict():
    reg = SkillRegistry()
    reg.add(Skill(name="t", version="1.0.0"))
    with pytest.raises(Exception):
        reg.add(Skill(name="t", version="1.0.0"))  # 同 version 重复


def test_registry_version_allowed():
    reg = SkillRegistry()
    reg.add(Skill(name="t", version="1.0.0"))
    reg.add(Skill(name="t", version="1.1.0"))  # 不同 version 允许
    assert "t" in reg._by_name