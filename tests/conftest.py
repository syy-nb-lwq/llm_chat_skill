"""pytest 全局 fixture"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

# 把项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试用 .env(避免污染真实配置)
ENV_PATH = ROOT / ".env"
if not ENV_PATH.exists():
    ENV_PATH.write_text(
        "OPENAI_API_KEY=sk-test-dummy\n"
        "OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "OPENAI_MODEL=gpt-4o-mini\n"
        "LOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )
else:
    # 确保测试必需的 OPENAI_API_KEY 存在(conftest 历史模板曾误用 LLM_API_KEY)
    _text = ENV_PATH.read_text(encoding="utf-8")
    if "OPENAI_API_KEY" not in _text:
        ENV_PATH.write_text(
            "OPENAI_API_KEY=sk-test-dummy\n" + _text,
            encoding="utf-8",
        )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def reset_singletons():
    """每个测试前重置全局单例,避免污染"""
    from infra import llm as llm_mod
    from infra import logger as logger_mod
    from skills import manager as skill_mod
    from tools.hub import reset_tool_hub
    from tools.base import reset_tool_registry
    from agents import teaching_session as ts_mod
    from core import memory_repository as mem_repo_mod

    # 清理 backend/skills 残留文件
    import shutil
    backend_skills = Path(__file__).parent.parent / "backend" / "skills"
    if backend_skills.exists():
        for f in backend_skills.glob("*.md"):
            f.unlink()

    llm_mod.reset_llm_client()
    logger_mod.get_logger().__init__()
    skill_mod.reset_skill_store()
    ts_mod.reset_teaching_store()
    mem_repo_mod.reset_memory_repository()
    # 清理 teaching 持久化目录
    teach_dir = Path(__file__).parent.parent / "memory" / "teachings"
    if teach_dir.exists():
        for f in teach_dir.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    # 清理 episodes 目录
    ep_dir = Path(__file__).parent.parent / "memory" / "episodes"
    if ep_dir.exists():
        for f in ep_dir.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    # 清理 MemoryRepository 持久化的 sqlite 文件
    # (旧 schema 缺 scope 等列,会与新 SCHEMA 的 CREATE INDEX 冲突)
    for db_name in ("semantic_memory.db", "semantic_memory.db-wal", "semantic_memory.db-shm"):
        db_file = Path(__file__).parent.parent / "memory" / db_name
        if db_file.exists():
            try:
                db_file.unlink()
            except Exception:
                pass
    # 重置两个工具系统
    reset_tool_registry()
    reset_tool_hub()
    yield