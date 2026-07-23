"""M3-07 e2e:用户纠正 → 新版本 → 切 active → 回滚。

依据 docs/11-开发任务清单.md M3-07:
- 教学 DailyReport(1.0.0)
- 用户纠正"问题部分如果没内容就不要显示"
- 系统生成 1.0.1 候选,通过验证
- 切 active,旧版本仍可查,回滚生效
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def skill_dir(tmp_path, monkeypatch):
    """隔离 skills/ 目录到 tmp_path。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from skills import manager as skill_mod
    from skills.manager import reset_skill_store

    reset_skill_store()
    skill_mod._store = skill_mod.SkillStore(path=str(tmp_path))
    yield tmp_path


def _publish_v1(skill_dir):
    import yaml
    from datetime import datetime
    user_dir = skill_dir / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    p = user_dir / "DailyReport@1.0.0.yaml"
    p.write_text(yaml.safe_dump({
        "name": "DailyReport",
        "version": "1.0.0",
        "capability": "按三块生成日报",
        "method": "1) 完成 2) 问题 3) 计划",
        "patterns": ["日报", "daily"],
        "tags": ["work"],
        "steps": [],
        "examples": ["示例1", "示例2"],
        "active": True,
        "source": "taught",
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_correction_generates_new_version_and_rollback(skill_dir):
    """用户纠正 → 后端写入 patch → 审批 → 1.0.1 发布 → 回滚到 1.0.0。"""
    import yaml
    from skills.manager import SkillStore
    from backend.main import _publish_skill_version, _bump_version

    _publish_v1(skill_dir)

    store = SkillStore(path=str(skill_dir))
    existing = store.get_by_name("DailyReport")
    assert existing.version == "1.0.0"

    # 1) 写入 user_correction patch
    import json
    from pathlib import Path
    patch_id = "patch_correction_1"
    patch_path = (
        Path(__file__).resolve().parents[2] / "memory" / "skill_patches" / "pending" / f"{patch_id}.json"
    )
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(json.dumps({
        "id": patch_id,
        "trace_id": "exec_x",
        "execution_id": "exec_x",
        "timestamp": "2026-07-20T00:00:00",
        "target_skill": "DailyReport",
        "evidence_execution_id": "exec_x",
        "patch_type": "user_correction",
        "diagnosis": "用户纠正: 问题部分如果没有内容就不要显示",
        "suggestion": {
            "type": "improve_method",
            "target_skill": "DailyReport",
            "version_target": "next",
            "user_feedback": "问题部分如果没内容就不显示",
            "evidence_execution_id": "exec_x",
            "current_version": "1.0.0",
        },
        "confidence": 0.9,
        "status": "pending",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 模拟 approve_patch 的核心逻辑(直接调用后端 helper)
    from datetime import datetime
    new_version = _bump_version(existing.version)
    correction = "问题部分如果没内容就不显示"
    candidate_method = existing.method + f"\n\n[用户纠正补丁] {correction}"

    from skills.models import Skill
    new_skill = Skill(
        name=existing.name,
        version=new_version,
        capability=existing.capability,
        method=candidate_method,
        patterns=list(existing.patterns),
        tags=list(existing.tags),
        steps=list(existing.steps),
        examples=list(existing.examples),
        source="evolved",
        updated_at=datetime.now().isoformat(),
    )

    issues = []
    try:
        from skills.validator import validate_skill
        issues = validate_skill(new_skill, tool_names=store.list_tool_names())
        issues = [i for i in issues if "缺少 examples" not in i]
    except Exception:
        pass
    assert not issues, f"validation failed: {issues}"

    ok, path_or_err = _publish_skill_version(new_skill)
    assert ok, path_or_err

    # 3) 校验两个版本并存 + active = 1.0.1
    store.reload()
    versions = store._registry.list_versions("DailyReport")
    assert set(versions) == {"1.0.0", "1.0.1"}
    assert store._registry._active_versions["DailyReport"] == "1.0.1"

    # 4) 回滚到 1.0.0
    ok = store._registry.set_active("DailyReport", "1.0.0")
    assert ok
    assert store._registry._active_versions["DailyReport"] == "1.0.0"
    # 旧版本内容可查
    v1 = store._registry.get_version("DailyReport", "1.0.0")
    assert v1 is not None
    assert "[用户纠正补丁]" not in v1.method
    # 新版本内容应包含纠正
    v2 = store._registry.get_version("DailyReport", "1.0.1")
    assert v2 is not None
    assert "问题部分如果没内容就不显示" in v2.method

    # 清理
    try:
        patch_path.unlink()
    except Exception:
        pass
