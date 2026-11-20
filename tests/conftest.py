"""pytest 全局 fixture"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

# 把项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 让 pytest 可以离线跑通:
# backend 启动时会做 fail-fast 配置校验(infra/config.py::validate),
# 没有 OPENAI_API_KEY 时整个后端测试会直接报错。
# 这里只在"环境变量和 .env 都没有提供 key"时注入一个占位 key,
# 不覆盖真实配置,也不往项目根写测试残留文件。
_ENV_PATH = ROOT / ".env"
_env_text = ""
if _ENV_PATH.exists():
    try:
        _env_text = _ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        _env_text = ""

if not os.environ.get("OPENAI_API_KEY") and "OPENAI_API_KEY" not in _env_text:
    os.environ["OPENAI_API_KEY"] = "sk-test-dummy"

if not _ENV_PATH.exists():
    os.environ.setdefault("OPENAI_BASE_URL", "https://api.openai.com/v1")
    os.environ.setdefault("OPENAI_MODEL", "gpt-4o-mini")
    os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def pytest_addoption(parser):
    """`--live` 开关:跑需要真实 API 的评估集(L1 通道)。"""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="运行需要真实 LLM API 的测试(如意图评估集的 L1 通道)",
    )


@pytest.fixture
def live_enabled(request):
    """需要真实 API 的测试用这个 fixture 跳过。"""
    if not request.config.getoption("--live", default=False):
        pytest.skip("需要 --live 与真实 API key")


@pytest.fixture(scope="session")
def session_db_store(tmp_path_factory):
    """整个测试会话共用一个临时会话库(避免每个测试都建一次 SQLite 文件)。"""
    from core.session_memory import SessionMemoryStore

    return SessionMemoryStore(
        db_path=tmp_path_factory.mktemp("sessions") / "sessions.db"
    )


@pytest.fixture(scope="session")
def long_term_db(tmp_path_factory):
    """长期记忆库(用户画像)也指向临时目录。

    不隔离的话,跑测试会往真实的 `memory/semantic_memory.db` 里写画像数据 ——
    这跟生产库混在一起,而且会让"用户记住了什么"变得不可解释。
    """
    from core.memory_db import MemoryDB

    return MemoryDB(db_path=tmp_path_factory.mktemp("long_term") / "semantic_memory.db")


@pytest.fixture(scope="session")
def mem_store_base(tmp_path_factory):
    """MemoryStore(自演化的成功/失败记录)的隔离根目录。"""
    return tmp_path_factory.mktemp("memory_store")


#: 这些全局函数是"真 LLM 通道"的入口。
#:
#: 踩过的坑:某个测试用 `_l_mod.get_llm_client = lambda: StubLLM()` 直接赋值
#: (不是 monkeypatch)却没有还原,于是**后面所有测试拿到的都是假 LLM**。
#: 症状极其隐蔽:测试全绿,但"真 LLM 通道"其实从来没被执行过 ——
#: 改坏了解析逻辑也发现不了,直到线上炸。
#:
#: 这里在会话开始时留一份原函数,每个测试之后强制还原,
#: 让"永久改写全局"这类污染不再可能藏得住。
def _capture_global_getters():
    import core.agent_base as cab
    import infra.llm as llm_mod

    return {
        (llm_mod, "get_llm_client"): llm_mod.get_llm_client,
        (cab, "get_llm_client"): cab.get_llm_client,
    }


_ORIGINAL_GLOBAL_GETTERS = _capture_global_getters()


def _restore_global_getters():
    for (module, name), original in _ORIGINAL_GLOBAL_GETTERS.items():
        if getattr(module, name, None) is not original:
            setattr(module, name, original)


#: 会被"功能开关接口"改写的配置字段。
#:
#: 这些字段是**全局 config 单例上的可变属性** —— 接口直接 `setattr`,
#: 不是 monkeypatch,所以不会自动还原。测试之间必须手动还原,
#: 否则一个测试关掉开关,后面所有测试都以为它关着。
_FEATURE_FLAGS = (
    "self_evolution_enabled",
    "semantic_memory_enabled",
    "multi_provider_enabled",
    "intent_l1_enabled",
    "trace_enabled",
    "code_exec_enabled",
    "mcp_enabled",
)

#: 会话开始时拍下的快照
_ORIGINAL_FEATURE_FLAGS: dict = {}


def _capture_feature_flags():
    """拍下功能开关的初始值(导入时调一次)。"""
    from infra.config import config

    for name in _FEATURE_FLAGS:
        if hasattr(config, name):
            _ORIGINAL_FEATURE_FLAGS[name] = getattr(config, name)


def _restore_feature_flags():
    """把功能开关还原到会话开始时的值(每个测试后调)。"""
    from infra.config import config

    for name, value in _ORIGINAL_FEATURE_FLAGS.items():
        try:
            if getattr(config, name, None) is not value:
                setattr(config, name, value)
        except Exception:
            pass


_capture_feature_flags()


@pytest.fixture
def mem_store(tmp_path):
    """一个干净的 MemoryStore(每个测试独立)。

    不隔离的话,critic 的成功/失败记录会写进真实的 `memory/failures`、
    `memory/successes` —— 那些是生产数据,被测试污染就没法看了。
    用 `tmp_path`(函数级)而不是 session 级目录,否则测试之间会互相累积。
    """
    from core.memory import MemoryStore

    return MemoryStore(base_path=tmp_path / "mem")


@pytest.fixture
def stub_intent(monkeypatch):
    """把 ManagerAgent 的 L1 判定替换成固定结果。

    **意图的唯一裁决者是 L1**,所以测试里"让这句话被判成 X"唯一正确的做法
    就是 stub L1 —— 而不是去喂关键词、也不是去改规则
    (真机基线证明规则判意图会把准确率从 97.7% 拖到 94.7%)。

    用法:
        stub_intent("chitchat")
        stub_intent("task", confidence=0.4)
    """
    from agents import manager as mgr_mod
    from core.intent_labels import IntentLabel
    from core.llm_judge import IntentDecision

    def _apply(label: str, confidence: float = 0.95, reason: str = "stub",
               slots=None, needs_clarify=False):
        decision = IntentDecision(
            label=IntentLabel(label),
            confidence=confidence,
            reason=reason,
            slots=slots or {},
            needs_clarify=needs_clarify,
        )

        async def _fake(self, user_input, context=None):
            return decision

        monkeypatch.setattr(mgr_mod.ManagerAgent, "_l1_classify", _fake)
        return decision

    return _apply


@pytest.fixture(autouse=True)
def reset_singletons(monkeypatch, session_db_store, long_term_db, mem_store_base):
    """每个测试前重置全局单例,避免污染"""
    from infra import llm as llm_mod
    from infra import logger as logger_mod
    from skills import manager as skill_mod
    from tools.hub import reset_tool_hub
    from tools.base import reset_tool_registry
    from agents import teaching_session as ts_mod
    from core import session_memory as sm_mod
    from core import memory_db as mdb_mod
    from core import semantic_memory as sem_mod
    from core import memory_writer as mw_mod
    from core import memory as mem_mod
    from core import skill_quality as sq_mod
    from core.memorykit import reset_memory
    from core.llm_judge import reset_l1_classifier
    from core.clarifier import reset_clarifier
    from infra.config import config as config_mod

    # ===== 测试不该受开发者本地 `.env` 影响 =====
    #
    # `infra/config.py` 硬编码读项目根的 `.env`。开发者为了本地体验
    # 打开 MCP/代码执行,整套单测就会跟着变 —— 实测:
    # `MCP_ENABLED=true` 时,每个 TestClient 的 lifespan 都会去 spawn
    # 一个 npx MCP 服务器,单测从 100 秒变成 250 秒,
    # 而且**换台没装 npx / 没网的机器就直接失败**。
    #
    # 这类"环境依赖"必须挡在测试外,而不是指望每个开发者都记得改 .env。
    monkeypatch.setattr(config_mod, "mcp_enabled", False, raising=False)
    monkeypatch.setattr(config_mod, "code_exec_enabled", True, raising=False)
    monkeypatch.setattr(config_mod, "owner_token", "", raising=False)

    # 记忆三层门面：不重置的话,上一轮遗留的"当前轮工作记忆"会串到下一个测试
    reset_memory()

    # 会话库指向临时目录 —— 后端测试会创建带持久化的 Agent,
    # 不能让它们写进真实的 memory/sessions.db
    session_db_store.clear_all()
    monkeypatch.setattr(sm_mod, "get_session_memory_store", lambda: session_db_store)

    # 长期记忆库同理(画像写入走主链,不隔离会污染生产库)
    monkeypatch.setattr(mdb_mod, "get_memory_db", lambda: long_term_db)
    monkeypatch.setattr(sem_mod, "get_memory_db", lambda: long_term_db)
    sem_mod.reset_semantic_memory()
    mw_mod.reset_memory_writer()

    # MemoryStore(自演化的成功/失败记录)也要隔离:
    # 不隔离的话,critic 会把测试产生的记录写进真实的 memory/failures、memory/successes。
    #
    # 注意:这里改的是**全局实例**而不是 `get_memory_store` 函数本身 ——
    # 有些测试会自己替换 `core.memory._memory_store` 来指向临时目录,
    # 替换函数的话那些测试就找不到自己的 store 了。
    _store = mem_mod.MemoryStore(base_path=mem_store_base / "shared")
    monkeypatch.setattr(mem_mod, "_memory_store", _store)
    # `from core.memory import get_memory_store` 是直接导入,引用点要单独指回来
    monkeypatch.setattr("core.critic.get_memory_store", lambda: mem_mod.get_memory_store())
    monkeypatch.setattr("core.agent.get_memory_store", lambda: mem_mod.get_memory_store())

    # 技能质量账本(docs/14 A-07)也要隔离:
    # 不隔离的话,测试产生的"技能通过率"会写进真实的 memory/skills/quality.jsonl,
    # 生产的质量数据就不可信了。
    sq_mod.reset_skill_quality_store()
    quality_store = sq_mod.SkillQualityStore(base_path=mem_store_base / "skill_quality")
    monkeypatch.setattr(sq_mod, "get_skill_quality_store", lambda: quality_store)

    # TraceRecorder 也指向临时目录 ——
    # 否则测试会往真实的 memory/audit/traces.jsonl 里灌垃圾,
    # 生产的观测数据就不可信了。
    from core import trace as trace_mod

    trace_mod.reset_trace_recorder()
    monkeypatch.setattr(
        trace_mod,
        "get_trace_recorder",
        lambda: trace_mod.TraceRecorder(
            path=mem_store_base / "traces.jsonl",
            enabled=bool(getattr(config_mod, "trace_enabled", True)),
        ),
    )

    # 清理 backend/skills 残留文件
    backend_skills = Path(__file__).parent.parent / "backend" / "skills"
    if backend_skills.exists():
        for f in backend_skills.glob("*.md"):
            f.unlink()

    llm_mod.reset_llm_client()
    logger_mod.get_logger().__init__()
    skill_mod.reset_skill_store()
    ts_mod.reset_teaching_store()
    sm_mod.reset_session_memory_store()
    reset_l1_classifier()
    reset_clarifier()
    # 清理 teaching 持久化目录
    teach_dir = Path(__file__).parent.parent / "memory" / "teachings"
    if teach_dir.exists():
        for f in teach_dir.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    # 清理用户档案目录(假用户服务)
    users_dir = Path(__file__).parent.parent / "memory" / "users"
    if users_dir.exists():
        for f in users_dir.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    # 重置两个工具系统
    reset_tool_registry()
    reset_tool_hub()
    yield
    # 强制还原被永久改写的全局入口(见 _capture_global_getters 的说明)
    _restore_global_getters()
    # ===== 还原功能开关 =====
    #
    # `POST /api/features/self-evolution` 会**直接改全局 config 单例**
    # (`Config.set_feature_flag` → `setattr(self, ...)`)。
    # 它不是 monkeypatch,所以**不会自动还原** ——
    # 一个测试把开关关掉,后面所有测试的
    # `get_self_evolution_enabled()` 都返回 False。
    #
    # 实测踩过:`test_backend.py` 关了一次自演化,导致
    # `test_critic_isolation.py` 的 3 个用例全部失败
    # (critic 直接短路返回 `diagnosis='self_evolution_disabled'`)——
    # 而单独跑那个文件是全绿的,典型的“测试间污染”。
    _restore_feature_flags()

@pytest.fixture(autouse=True)
def _isolate_tool_health(tmp_path, monkeypatch):
    """把**工具健康账本**隔离到 tmp —— 每个测试独立。

    实机踩过：只要测试里调了 `hub.call_tool()`，流水就会写进真实的
    `memory/tools/health.jsonl`，于是 `/api/tool-health` 里冒出
    `a` / `flaky` / `stub_1` 这些测试用的假工具名 —— 生产数据被污染了。

    和 `mem_store` 那个 fixture 是同一个道理：**测试不许碰真实数据目录**。
    """
    import core.tool_health as th

    store = th.ToolHealthStore(base_path=tmp_path / "tool_health")
    monkeypatch.setattr(th, "_store", store)
    return store


@pytest.fixture(autouse=True)
def _offline_profile_extractor(monkeypatch):
    """画像抽取在测试里走**离线确定性**版本。

    生产用 LLM（`core/profile_llm.extract_with_llm`）。但测试不能打网络：
    既慢、又不确定 —— 同一个输入模型两次可能给出不同措辞，
    断言就会随机失败。

    这里换成旧的规则抽取器，语义与这些测试当年编写时一致。
    """
    import core.memory_writer as mw

    async def _stub(text):
        return mw.extract_candidates(text)

    monkeypatch.setattr(mw, "_extractor", _stub)
    return _stub
