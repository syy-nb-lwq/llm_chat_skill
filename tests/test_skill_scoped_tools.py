"""技能优先裁剪工具表 —— 子智能体按需加载工具，而非全量塞进上下文。

用户要求：子智能体执行任务应先找 skill，skill 里就带着该用的 tool，
用它裁剪工具集。这样上下文更短，也避免模型在全量工具里选错（工具漂移）。
"""
from core.subagents import SubAgentPool
from skills.models import Skill


class FakeStore:
    def __init__(self, matched):
        self.matched = matched

    def match(self, query, k):
        return self.matched[:k]


def _patch_store(monkeypatch, matched):
    import skills.manager as m

    monkeypatch.setattr(m, "get_skill_store", lambda: FakeStore(matched))
    # subagents 里是 `from skills.manager import get_skill_store`（函数内导入），
    # 上面 patch 模块属性即可覆盖。


def _pool():
    return SubAgentPool()


ROLE_TOOLS = ["read_document", "python_exec", "web_search", "publish_artifact", "video_probe"]


def test_scopes_to_skills_tools(monkeypatch):
    """命中技能 → 只保留技能声明的工具 ∩ 角色白名单。"""
    sk = Skill(name="报销格式化", required_tools=["read_document", "python_exec", "publish_artifact"])
    _patch_store(monkeypatch, [sk])

    tools, names = _pool()._skill_scoped_tools("按格式输出报销文件", ROLE_TOOLS, None)
    assert tools == ["read_document", "python_exec", "publish_artifact"]
    assert names == ["报销格式化"]


def test_intersection_never_escalates(monkeypatch):
    """技能声明了角色白名单之外的工具 → 禁止越权，只留交集。"""
    sk = Skill(name="视频", required_tools=["video_probe", "dangerous_tool"])
    _patch_store(monkeypatch, [sk])

    tools, names = _pool()._skill_scoped_tools("生成视频", ROLE_TOOLS, None)
    assert tools == ["video_probe"]
    assert "dangerous_tool" not in tools


def test_empty_required_tools_falls_back(monkeypatch):
    """技能没声明工具（纯判断/旧技能）→ 退回角色白名单，不破坏现有行为。"""
    sk = Skill(name="偏好问答", required_tools=[])
    _patch_store(monkeypatch, [sk])

    tools, names = _pool()._skill_scoped_tools("你还记得我什么", ROLE_TOOLS, None)
    assert tools == ROLE_TOOLS
    assert names == []


def test_no_match_falls_back(monkeypatch):
    """没匹配到技能 → 原样返回角色白名单。"""
    _patch_store(monkeypatch, [])

    tools, names = _pool()._skill_scoped_tools("随便聊两句", ROLE_TOOLS, None)
    assert tools == ROLE_TOOLS
    assert names == []


def test_union_of_multiple_skills(monkeypatch):
    """命中多个技能 → 取各自工具的并集，仍受角色白名单约束。"""
    s1 = Skill(name="读表", required_tools=["read_document"])
    s2 = Skill(name="出文件", required_tools=["python_exec", "publish_artifact"])
    _patch_store(monkeypatch, [s1, s2])

    tools, names = _pool()._skill_scoped_tools("读表并出报销文件", ROLE_TOOLS, None)
    assert set(tools) == {"read_document", "python_exec", "publish_artifact"}
    assert set(names) == {"读表", "出文件"}


def test_allowed_skills_filters_matches(monkeypatch):
    """角色限定技能时，只用限定内的技能来裁剪。"""
    s1 = Skill(name="允许的技能", version="1.0.0", required_tools=["read_document"])
    s2 = Skill(name="禁止的技能", version="1.0.0", required_tools=["video_probe"])
    _patch_store(monkeypatch, [s1, s2])

    tools, names = _pool()._skill_scoped_tools(
        "任意", ROLE_TOOLS, ["允许的技能"]
    )
    assert tools == ["read_document"]
    assert names == ["允许的技能"]


def test_no_intersection_falls_back(monkeypatch):
    """技能工具与角色白名单完全无交集 → 退回白名单，避免子智能体无工具可用。"""
    sk = Skill(name="越界技能", required_tools=["dangerous_tool"])
    _patch_store(monkeypatch, [sk])

    tools, names = _pool()._skill_scoped_tools("任意", ROLE_TOOLS, None)
    assert tools == ROLE_TOOLS
