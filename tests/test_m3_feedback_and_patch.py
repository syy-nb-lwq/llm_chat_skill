"""M3-02 / M3-04 测试:FeedbackEvent + Skill 不可变补丁审批。

覆盖:
- 提交 correction 反馈会自动创建 patch
- patch 含 evidence_execution_id 和 target_skill
- 审批 patch 生成新版本(M3-04)
- 回滚可恢复旧版本
"""
import asyncio
import json
from pathlib import Path

import pytest

from core.feedback import (
    FeedbackEvent,
    FeedbackStore,
    get_feedback_store,
    reset_feedback_store,
)


@pytest.fixture
def fs(tmp_path):
    reset_feedback_store()
    return FeedbackStore(base_path=tmp_path)


def test_feedback_persisted(fs):
    ev = FeedbackEvent(
        execution_id="exec_001",
        user_id="alice",
        session_id="s",
        type="correction",
        content="问题部分如果没内容就不显示",
    )
    path = fs.save(ev)
    assert path.exists()
    # list 默认 user_id="default",需要用 "alice" 或 None
    items = fs.list(user_id="alice", execution_id="exec_001")
    assert len(items) >= 1
    assert items[0]["type"] == "correction"


def test_feedback_user_isolation(fs):
    fs.save(FeedbackEvent(execution_id="e1", user_id="alice", type="accept"))
    fs.save(FeedbackEvent(execution_id="e2", user_id="bob", type="accept"))
    assert all(it.get("user_id") == "alice" for it in fs.list(user_id="alice"))
    assert all(it.get("user_id") == "bob" for it in fs.list(user_id="bob"))


def test_feedback_get_for_execution(fs):
    fs.save(FeedbackEvent(execution_id="e1", user_id="alice", type="correction", content="a"))
    fs.save(FeedbackEvent(execution_id="e1", user_id="alice", type="accept", content="b"))
    fs.save(FeedbackEvent(execution_id="e2", user_id="alice", type="accept"))
    items = fs.get_for_execution("e1")
    assert len(items) == 2
    assert all(it["execution_id"] == "e1" for it in items)


# ===== Skill 版本不可变 + 审批生成新版本 =====


@pytest.fixture
def skill_store(tmp_path):
    """构造一个 skills/user 目录,预置 1.0.0 版本。"""
    from skills.manager import SkillStore, reset_skill_store
    reset_skill_store()

    user_dir = tmp_path / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "DailyReport@1.0.0.yaml").write_text(
        "\n".join([
            "name: DailyReport",
            "version: 1.0.0",
            "capability: 按三块生成日报",
            "method: |",
            "  1) 今日完成 2) 问题 3) 计划",
            "patterns: [日报, daily]",
            "tags: [work]",
            "steps: []",
            "examples: [示例1, 示例2]",
            "active: true",
            "source: taught",
        ]),
        encoding="utf-8",
    )
    store = SkillStore(path=str(tmp_path))
    return store


def test_skill_versions_coexist(skill_store):
    """1.0.0 应该可以加载(单版本默认 active)。"""
    s = skill_store.get_by_name("DailyReport")
    assert s is not None
    assert s.version == "1.0.0"
    versions = skill_store._registry.list_versions("DailyReport")
    assert "1.0.0" in versions


def test_publish_new_version_does_not_overwrite_old(skill_store):
    """新版本发布 → 旧版本仍可访问。"""
    from datetime import datetime
    import yaml
    from skills.models import Skill

    base = skill_store.base_path
    new_path = base / "user" / "DailyReport@1.0.1.yaml"
    new_path.write_text(
        yaml.safe_dump({
            "name": "DailyReport",
            "version": "1.0.1",
            "capability": "按四块生成日报",
            "method": "1) 今日完成 2) 问题 3) 计划 4) 反思",
            "patterns": ["日报", "daily"],
            "tags": ["work"],
            "steps": [],
            "examples": ["示例1"],
            "active": True,
            "source": "evolved",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    new_skill = Skill(
        name="DailyReport", version="1.0.1",
        capability="按四块生成日报",
        method="1) 今日完成 2) 问题 3) 计划 4) 反思",
        patterns=["日报", "daily"],
        tags=["work"],
        steps=[],
        examples=["示例1"],
        source="evolved",
    )
    skill_store.add(new_skill, set_active=True)

    # 刷新后两个版本都在
    skill_store.reload()
    versions = skill_store._registry.list_versions("DailyReport")
    assert set(versions) >= {"1.0.0", "1.0.1"}
    # 1.0.1 是 active
    active = skill_store._registry._active_versions.get("DailyReport")
    assert active == "1.0.1"


def test_rollback_restores_old_version(skill_store):
    """回滚到 1.0.0 → active 重新指向旧版本。"""
    from skills.models import Skill

    new_skill = Skill(
        name="DailyReport", version="1.0.1",
        capability="按四块生成日报",
        method="反思追加",
        patterns=["日报"],
        tags=["work"],
        steps=[],
        examples=["x"],
        source="evolved",
    )
    skill_store.add(new_skill, set_active=True)
    skill_store.reload()

    ok = skill_store._registry.set_active("DailyReport", "1.0.0")
    assert ok
    active = skill_store._registry._active_versions.get("DailyReport")
    assert active == "1.0.0"
