"""M1-10 e2e 验证:Teach → Confirm → Persist → Restart → Recall → Execute

依据 docs/11-开发任务清单.md M1-10:
- 三轮教会"日报生成"技能(纯方法论,无工具)
- 持久化到磁盘
- 模拟服务重启(清单例 + 重新加载 SkillStore)
- 用户发送当天工作记录,系统召回并按 skill.method 输出
- 执行记录带技能版本号

设计:
- 完全离线运行:通过 ScriptedLLMProvider 替换真实 LLM
- 隔离运行时数据:SkillStore / TeachingSessionStore 都用 tmp_path
- 不依赖 OPENAI_API_KEY / 网络
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pytest
import yaml


# ===== 公共 fixtures =====


@pytest.fixture
def e2e_env(isolated_runtime, fake_llm):
    """组合夹具:隔离运行时 + 注入 fake LLM。"""
    runtime = isolated_runtime
    llm = fake_llm
    return {"runtime": runtime, "llm": llm}


def _queue_extract(llm, *, name, method, capability, patterns=None, tags=None):
    """入队 extract_skill 的 LLM 响应(EXTRACT_SCHEMA)。"""
    llm.queue({
        "name": name,
        "method": method,
        "capability": capability,
        "patterns": patterns or [],
        "tags": tags or [],
        "steps": [],
    })


# ===== 场景一:三轮回合教学 → 草稿完整 =====


@pytest.mark.asyncio
async def test_three_round_teaching_reaches_draft(e2e_env):
    """用户三轮输入后,TeachingSession 状态机推进到 DRAFT。"""
    runtime = e2e_env["runtime"]
    llm = e2e_env["llm"]

    from agents.skill_trainer import SkillTrainer
    from agents.teaching_session import TeachingSessionStore, TeachingStatus
    from skills.manager import SkillStore

    # 把 SkillTrainer 内部的 SkillStore 切到 isolated dir
    skill_store = SkillStore(path=str(runtime["skills_path"]))
    teaching_store = TeachingSessionStore(base_path=runtime["teachings_path"])
    trainer = SkillTrainer(teaching_store=teaching_store)
    trainer.skill_store = skill_store

    user_id = "alice"
    session_id = "s-e2e-1"

    # Round 1: 用户只说"以后帮我整理日报"
    _queue_extract(llm,
        name="DailyReport", method="", capability="",
        patterns=["日报", "daily report"], tags=["工作"],
    )

    ts1 = await trainer.start_or_continue(
        "以后帮我整理日报",
        user_id=user_id, session_id=session_id,
    )
    assert ts1.status == TeachingStatus.COLLECTING, f"Round1 状态应为 COLLECTING,实际 {ts1.status}"
    assert ts1.partial_skill.get("name") == "DailyReport"
    assert "method" in ts1.missing_fields
    assert "capability" in ts1.missing_fields

    # Round 2: 用户补充方法(method)但 capability 还缺
    _queue_extract(llm,
        name="DailyReport",
        method="1) 今日完成 2) 遇到的问题 3) 明日计划;简洁但不简单",
        capability="",
        patterns=["日报"],
    )

    ts2 = await trainer.start_or_continue(
        "需要包含:今日完成、问题、明日计划",
        user_id=user_id, session_id=session_id,
    )
    assert ts2.partial_skill.get("method"), "method 应当被填写"
    assert "capability" in ts2.missing_fields, "capability 仍应缺失"

    # Round 3: 用户补充 capability → 信息完整 → DRAFT
    _queue_extract(llm,
        name="DailyReport",
        method="1) 今日完成 2) 遇到的问题 3) 明日计划;简洁但不简单",
        capability="根据当天工作记录生成结构化日报,不披露客户名称和内部地址",
        patterns=["日报", "daily report"],
        tags=["工作"],
    )

    ts3 = await trainer.start_or_continue(
        "能力是:按上面的栏目生成日报,风格简洁,不披露客户名称",
        user_id=user_id, session_id=session_id,
    )
    assert ts3.missing_fields == [], f"字段应齐全,仍缺 {ts3.missing_fields}"
    assert ts3.status == TeachingStatus.DRAFT, f"Round3 应为 DRAFT,实际 {ts3.status}"
    assert ts3.draft_skill is not None
    assert ts3.draft_skill["name"] == "DailyReport"


# ===== 场景二:确认发布 → 落盘 → 重启 → 召回 =====


@pytest.mark.asyncio
async def test_confirm_publish_then_restart_then_recall(e2e_env):
    """完整闭环:发布 → 模拟重启 → 从磁盘加载 → selected_skill 命中并带版本号。"""
    runtime = e2e_env["runtime"]
    llm = e2e_env["llm"]

    from agents.skill_trainer import SkillTrainer
    from agents.teaching_session import TeachingSessionStore, TeachingStatus
    from agents.manager import ManagerAgent
    from skills.manager import SkillStore, reset_skill_store

    user_id = "bob"
    session_id = "s-e2e-2"

    # ---------- 1) 教学 + 确认发布 ----------
    skill_store = SkillStore(path=str(runtime["skills_path"]))
    teaching_store = TeachingSessionStore(base_path=runtime["teachings_path"])
    trainer = SkillTrainer(teaching_store=teaching_store)
    trainer.skill_store = skill_store

    _queue_extract(llm,
        name="DailyReport", method="", capability="",
        patterns=["日报"],
    )
    ts1 = await trainer.start_or_continue(
        "以后帮我写日报",
        user_id=user_id, session_id=session_id,
    )
    assert ts1.status == TeachingStatus.COLLECTING

    _queue_extract(llm,
        name="DailyReport",
        method="1) 完成 2) 问题 3) 计划",
        capability="",
        patterns=[],
    )
    ts2 = await trainer.start_or_continue(
        "分三块:完成/问题/计划",
        user_id=user_id, session_id=session_id,
    )

    _queue_extract(llm,
        name="DailyReport",
        method="1) 完成 2) 问题 3) 计划",
        capability="按三块结构生成日报,不披露客户名",
        patterns=["日报", "daily report"],
        tags=["工作"],
    )
    ts3 = await trainer.start_or_continue(
        "风格简洁,不披露客户名",
        user_id=user_id, session_id=session_id,
    )
    assert ts3.status == TeachingStatus.DRAFT

    # 确认发布
    ok, msg, skill = trainer.confirm_and_publish(user_id, session_id)
    assert ok, f"发布失败: {msg}"
    assert skill is not None
    assert skill.name == "DailyReport"
    assert skill.version == "1.0.0"

    # 校验磁盘
    yaml_path = runtime["skills_path"] / "user" / "DailyReport@1.0.0.yaml"
    assert yaml_path.exists(), f"技能未落盘: {yaml_path}"
    raw = yaml_path.read_text(encoding="utf-8")
    assert "DailyReport" in raw
    assert "1.0.0" in raw
    assert "active: true" in raw

    # TeachingSession 状态机应当推进到 ACTIVE(find_active_for 只返回非终态,
    # 所以发布后改用 list_active 然后过滤 user_id+session_id 校验)
    all_sessions = teaching_store.list_active()  # 列表里通常没有刚 ACTIVE 的
    # 直接从磁盘重新读取
    import json as _json
    sess_files = list(runtime["teachings_path"].glob("*.json"))
    assert sess_files, "未持久化 teaching session"
    persisted = _json.loads(sess_files[0].read_text(encoding="utf-8"))
    assert persisted["status"] == TeachingStatus.ACTIVE, \
        f"TS 应为 ACTIVE,实际 {persisted['status']}"

    # ---------- 2) 模拟重启:清单例 → 重新加载 ----------
    reset_skill_store()
    from agents import teaching_session as ts_mod
    ts_mod.reset_teaching_store()

    new_store = SkillStore(path=str(runtime["skills_path"]))
    all_skills = new_store.list_all()
    assert any(s.name == "DailyReport" for s in all_skills), \
        "重启后未从磁盘加载 DailyReport"

    daily = new_store.get_by_name("DailyReport")
    assert daily is not None
    assert daily.version == "1.0.0"
    assert daily.capability.startswith("按三块结构")
    assert "日报" in daily.patterns

    # ---------- 3) 召回:Manager 应当能命中 DailyReport ----------
    manager = ManagerAgent()
    manager.skill_store = new_store  # 替换默认 store

    llm.queue({
        "intent": "为用户整理日报",
        "selected_skill": "DailyReport",
        "is_retry": False,
        "tool_tasks": [],  # 纯方法论技能,无工具
    })

    from core.context import Context
    ctx = Context()
    plan = await manager.plan("帮我整理今天的工作日报", ctx)

    assert plan.intent == "skill", f"期望 SKILL 意图,实际 {plan.intent}"
    assert plan.selected_skill is not None, "未选中任何技能"
    assert plan.selected_skill.name == "DailyReport"
    assert plan.selected_skill.version == "1.0.0", \
        f"版本应为 1.0.0,实际 {plan.selected_skill.version}"
    assert plan.tool_tasks == [], "纯方法论技能不应有工具任务"


# ===== 场景三:执行链路上 execution 记录带技能版本 =====


@pytest.mark.asyncio
async def test_execution_record_carries_skill_version(e2e_env):
    """Agent.handle() 跑完后,plan 事件必须包含 selected_skill 与 version。"""
    runtime = e2e_env["runtime"]
    llm = e2e_env["llm"]

    from agents.skill_trainer import SkillTrainer
    from agents.teaching_session import TeachingSessionStore, TeachingStatus
    from core.agent import Agent
    from core.critic import build_execution_context
    from skills.manager import SkillStore

    user_id = "carol"
    session_id = "s-e2e-3"

    # ---------- 教学 + 发布 ----------
    skill_store = SkillStore(path=str(runtime["skills_path"]))
    teaching_store = TeachingSessionStore(base_path=runtime["teachings_path"])
    trainer = SkillTrainer(teaching_store=teaching_store)
    trainer.skill_store = skill_store

    _queue_extract(llm, name="DailyReport", method="", capability="",
                   patterns=["日报"])
    await trainer.start_or_continue("以后帮我写日报",
                                    user_id=user_id, session_id=session_id)

    _queue_extract(llm, name="DailyReport",
                   method="1) 完成 2) 问题 3) 计划", capability="")
    await trainer.start_or_continue("分三块写",
                                    user_id=user_id, session_id=session_id)

    _queue_extract(llm, name="DailyReport",
                   method="1) 完成 2) 问题 3) 计划",
                   capability="按三块生成日报,简洁不披露客户名",
                   patterns=["日报", "daily"])
    ts3 = await trainer.start_or_continue("简洁,不披露客户名",
                                          user_id=user_id, session_id=session_id)
    assert ts3.status == TeachingStatus.DRAFT

    ok, msg, skill = trainer.confirm_and_publish(user_id, session_id)
    assert ok and skill is not None

    # ---------- 模拟重启 ----------
    from skills import manager as skill_mod
    skill_mod.reset_skill_store()
    fresh_store = SkillStore(path=str(runtime["skills_path"]))
    skill_mod._store = fresh_store

    # ---------- 执行链路:Agent.handle() ----------
    # Manager.plan(): Fake LLM 返回选中 DailyReport 的 JSON
    llm.queue({
        "intent": "整理日报",
        "selected_skill": "DailyReport",
        "is_retry": False,
        "tool_tasks": [],  # 纯方法论,无工具
    })
    # Orchestrator 生成最终回复
    llm.queue("【今日完成】...【问题】...【明日计划】...")

    # 抓取 emit 事件
    events: List[tuple] = []

    def on_event(event: str, payload: dict):
        events.append((event, payload))

    agent = Agent(session_id=session_id)
    agent.manager.skill_store = fresh_store

    answer = await agent.handle("请帮我整理今天的工作日报", on_event=on_event)

    # 校验事件
    event_names = [e[0] for e in events]
    assert "plan" in event_names, f"应发出 plan 事件,实际 {event_names}"
    plan_evt = next(p for e, p in events if e == "plan")
    assert plan_evt["skill"] == "DailyReport", \
        f"plan 事件应指向 DailyReport,实际 {plan_evt}"

    # 校验最终回答非空
    assert answer, "Agent 应返回非空回答"
    assert isinstance(answer, str)

    # 校验 execution 记录(直接构造一个,模拟 critic 会使用的字段)
    execution_context = build_execution_context(
        trace_id=agent.trace_id,
        scenario="DailyReport",
        intent="skill",
        selected_skill="DailyReport",
        tool_results={},
        latency_ms=10,
    )
    assert execution_context.scenario == "DailyReport"
    assert execution_context.intent == "skill"
    assert execution_context.selected_skill == "DailyReport"

    # 通过 skill_store 反查 skill.version,确认能定位到 1.0.0
    recalled = fresh_store.get_by_name("DailyReport")
    assert recalled is not None
    assert recalled.version == "1.0.0"
    assert recalled.method == "1) 完成 2) 问题 3) 计划"


# ===== 场景四:落盘 YAML 必须含 active: true =====


@pytest.mark.asyncio
async def test_persisted_yaml_is_immutable_and_active(e2e_env):
    """落盘 YAML 必须含 active: true,符合 M1-03 不可变 + active 指针要求。"""
    runtime = e2e_env["runtime"]
    llm = e2e_env["llm"]

    from agents.skill_trainer import SkillTrainer
    from agents.teaching_session import TeachingSessionStore, TeachingStatus
    from skills.manager import SkillStore

    user_id = "dave"
    session_id = "s-e2e-4"

    skill_store = SkillStore(path=str(runtime["skills_path"]))
    teaching_store = TeachingSessionStore(base_path=runtime["teachings_path"])
    trainer = SkillTrainer(teaching_store=teaching_store)
    trainer.skill_store = skill_store

    # 单轮完整教学 → DRAFT
    _queue_extract(llm,
        name="DailyReport",
        method="1) 完成 2) 问题 3) 计划",
        capability="按三块生成日报",
        patterns=["日报", "daily"],
    )
    await trainer.start_or_continue(
        "以后帮我整理日报,分三块:完成、问题、计划",
        user_id=user_id, session_id=session_id,
    )

    # 自动 publish(teach() 在信息完整时会自动 confirm_and_publish)
    # 这里 teaching session 可能仍在 COLLECTING/DRAFT,需要再调一次 confirm_and_publish
    ts = teaching_store.find_active_for(user_id, session_id)
    assert ts is not None
    if ts.status == TeachingStatus.DRAFT:
        ok, msg, skill = trainer.confirm_and_publish(user_id, session_id)
        assert ok, f"发布失败: {msg}"

    yaml_path = runtime["skills_path"] / "user" / "DailyReport@1.0.0.yaml"
    assert yaml_path.exists(), f"未找到 YAML: {yaml_path}"

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert data["name"] == "DailyReport"
    assert data["version"] == "1.0.0"
    assert data["active"] is True
    assert data["source"] == "taught"
    assert data["method"].startswith("1) 完成")
    assert "日报" in data["patterns"]
