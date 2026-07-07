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
        "LLM_API_KEY=sk-test-dummy\n"
        "LLM_BASE_URL=https://api.openai.com/v1\n"
        "LLM_MODEL=gpt-4o-mini\n"
        "LOG_LEVEL=WARNING\n",
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
    from tools import base as tools_mod
    llm_mod.reset_llm_client()
    logger_mod.get_logger().__init__()
    skill_mod.reset_skill_store()
    tools_mod.reset_tool_registry()
    yield