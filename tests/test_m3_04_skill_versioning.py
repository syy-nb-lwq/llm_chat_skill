"""M3-04 技能版本演进测试。

覆盖:
- `update_skill()` 生成新版本文件并切 active,旧版本保留可回滚
- 新旧版本可同时存在,内容互不覆盖
- 版本号冲突时自动顺延
- 不修改已有文件名的前缀重叠场景(demo vs demo2)
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def store(tmp_path):
    """隔离的 SkillStore,内置一个 demo@1.0.0 技能。"""
    import skills.manager as mgr

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo@1.0.0.yaml").write_text(
        "name: demo\n"
        "version: 1.0.0\n"
        "capability: a\n"
        "method: old method\n"
        "patterns: [demo]\n"
        "steps: []\n",
        encoding="utf-8",
    )
    original = mgr._store
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        yield mgr._store, skills_dir
    finally:
        mgr._store = original


def test_update_skill_creates_new_version(store):
    skill_store, skills_dir = store

    written = skill_store.update_skill("demo", {"method": "new method"})

    new_path = skills_dir / "demo@1.0.1.yaml"
    assert new_path.exists()
    assert str(new_path) in written

    data = yaml.safe_load(new_path.read_text(encoding="utf-8"))
    assert data["method"] == "new method"
    assert data["version"] == "1.0.1"
    assert data["active"] is True


def test_update_skill_keeps_old_version(store):
    skill_store, skills_dir = store
    old_path = skills_dir / "demo@1.0.0.yaml"

    skill_store.update_skill("demo", {"method": "new method"})

    # 旧版本文件仍在,内容没有被覆盖
    assert old_path.exists()
    old_data = yaml.safe_load(old_path.read_text(encoding="utf-8"))
    assert old_data["method"] == "old method"
    assert old_data["active"] is False


def test_update_skill_switches_active_pointer(store):
    skill_store, _ = store

    skill_store.update_skill("demo", {"method": "new method"})

    active = skill_store.get_by_name("demo")
    assert active is not None
    assert active.version == "1.0.1"
    assert active.method == "new method"

    # 旧版本仍可按版本号取回(可回滚)
    old = skill_store._registry.get_version("demo", "1.0.0")
    assert old is not None
    assert old.method == "old method"


def test_update_skill_bumps_over_existing_version(store):
    skill_store, skills_dir = store

    # 预置一个已存在的 1.0.1,验证不会覆盖它
    (skills_dir / "demo@1.0.1.yaml").write_text(
        "name: demo\nversion: 1.0.1\ncapability: a\nmethod: v101\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )

    skill_store.update_skill("demo", {"method": "new method"})

    new_path = skills_dir / "demo@1.0.2.yaml"
    assert new_path.exists()
    data = yaml.safe_load(new_path.read_text(encoding="utf-8"))
    assert data["method"] == "new method"
    assert data["version"] == "1.0.2"

    # 已存在的 1.0.1 没被动过
    untouched = yaml.safe_load((skills_dir / "demo@1.0.1.yaml").read_text(encoding="utf-8"))
    assert untouched["method"] == "v101"


def test_update_skill_does_not_touch_similar_names(tmp_path):
    """demo 的更新不能误伤 demo2(前缀重叠)。"""
    import skills.manager as mgr

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo@1.0.0.yaml").write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: old\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )
    (skills_dir / "demo2@1.0.0.yaml").write_text(
        "name: demo2\nversion: 1.0.0\ncapability: b\nmethod: other\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )

    original = mgr._store
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        mgr._store.update_skill("demo", {"method": "new"})
    finally:
        mgr._store = original

    demo2 = yaml.safe_load((skills_dir / "demo2@1.0.0.yaml").read_text(encoding="utf-8"))
    assert demo2["method"] == "other"
    assert (skills_dir / "demo2@1.0.0.yaml").exists()


def test_update_skill_missing_skill_is_noop(tmp_path):
    import skills.manager as mgr

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)

    original = mgr._store
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        assert mgr._store.update_skill("__missing__", {"method": "x"}) == []
    finally:
        mgr._store = original


def test_delete_by_name_does_not_touch_similar_names(tmp_path):
    """删除 demo 不能误删 demo2(前缀重叠导致的数据丢失)。"""
    import skills.manager as mgr

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo@1.0.0.yaml").write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: m\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )
    (skills_dir / "demo@1.0.1.yaml").write_text(
        "name: demo\nversion: 1.0.1\ncapability: a\nmethod: m2\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )
    (skills_dir / "demo2@1.0.0.yaml").write_text(
        "name: demo2\nversion: 1.0.0\ncapability: b\nmethod: other\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )

    original = mgr._store
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        removed = mgr._store.delete_by_name("demo")
    finally:
        mgr._store = original

    names = {Path(p).name for p in removed}
    assert names == {"demo@1.0.0.yaml", "demo@1.0.1.yaml"}
    assert not (skills_dir / "demo@1.0.0.yaml").exists()
    assert not (skills_dir / "demo@1.0.1.yaml").exists()
    # demo2 必须毫发无损
    assert (skills_dir / "demo2@1.0.0.yaml").exists()


def test_delete_version_does_not_touch_other_versions(tmp_path):
    import skills.manager as mgr

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "demo@1.0.0.yaml").write_text(
        "name: demo\nversion: 1.0.0\ncapability: a\nmethod: m\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )
    (skills_dir / "demo@1.0.1.yaml").write_text(
        "name: demo\nversion: 1.0.1\ncapability: a\nmethod: m2\npatterns: []\nsteps: []\n",
        encoding="utf-8",
    )

    original = mgr._store
    mgr._store = mgr.SkillStore(str(skills_dir))
    try:
        mgr._store.delete_version("demo", "1.0.0")
    finally:
        mgr._store = original

    assert not (skills_dir / "demo@1.0.0.yaml").exists()
    assert (skills_dir / "demo@1.0.1.yaml").exists()
