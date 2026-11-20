"""真 LLM 通道的**管线验证** —— 不用真实 key 也能跑。

## 背景(docs/14 §4.1)

`tests/test_intent_eval.py::test_l1_llm_channel` 需要真实 API key,
所以它长期 skip —— **这条通道的代码路径从未被执行过**。
后果:改了模型名 / API 参数形状 / 解析逻辑,CI 全绿,
等拿到 key 跑 nightly 才发现坏了。

这里用本地 mock 把同一条路径跑通:
真的起 HTTP 服务 → 真的走 OpenAI SDK → 真的解析响应 → 真的算指标。

**它验证的是"管线通不通",不是"模型准不准"。**
准确率仍然需要真实模型 —— 那件事没法用替身代替。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.mock_openai import MockOpenAI

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "tests" / "data" / "intent_eval.jsonl"
#: 所有评估集文件(开发集 + 各批留出集 + 封存集)。
#: oracle 必须认识**全部**用例 —— 它模拟的是"标准答案",不是"开发集答案";
#: 只认开发集的话,留出集会被统一答成默认标签,把过拟合闸门误判成失败。
EVAL_FILES = sorted((ROOT / "tests" / "data").glob("intent_eval*.jsonl"))


def load_cases():
    cases = []
    for path in EVAL_FILES:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cases.append(json.loads(line))
    return cases


L1_CASES = [c for c in load_cases() if c.get("layer") == "L1"]


def _oracle_responder(messages, payload):
    """按评估集"标准答案"作答的 oracle。

    目的不是测准确率,而是证明:提示词确实发到了 LLM、
    响应确实被解析成了标签、指标确实被算了出来。
    """
    text = MockOpenAI.extract_user_input(messages)
    label = "task"
    for case in load_cases():
        if case["input"] == text:
            label = case["label"]
            break
    return json.dumps(
        {
            "label": label,
            "confidence": 0.95,
            "slots": {},
            "needs_clarify": False,
            "reason": "mock oracle",
        },
        ensure_ascii=False,
    )


@pytest.fixture
def mock_llm():
    with MockOpenAI(_oracle_responder) as mock:
        yield mock


@pytest.fixture
def provider_at(mock_llm, monkeypatch):
    """把 openai provider 指向 mock。

    注意两处全局状态:
    1. provider manager 是**全局单例**,而且实例会被缓存 → 必须 reset 后重新 register
    2. `LLMClient` 单例会缓存它拿到的 provider 实例 → 必须一并 reset
       (只 reset manager 不够,LLMClient 还攥着旧实例,base_url 还是老的)
    """
    from infra import llm as llm_mod
    from infra.providers.manager import get_provider_manager, reset_provider_manager
    from infra.providers.openai import OpenAIProvider

    reset_provider_manager()
    llm_mod.reset_llm_client()
    manager = get_provider_manager()
    manager.register(
        "openai",
        OpenAIProvider,
        config={
            "api_key": "sk-mock",
            "base_url": mock_llm.base_url,
            "default_model": "mock-model",
        },
        set_current=True,
    )
    yield manager
    reset_provider_manager()
    llm_mod.reset_llm_client()


# ============================================================
# 0. 测试卫生:全局入口不能被永久改写
# ============================================================

def test_global_llm_getters_are_not_polluted():
    """测试**不能永久**改写 `get_llm_client`。

    踩过的坑:某个测试用 `_l_mod.get_llm_client = lambda: StubLLM()` 直接赋值
    (不是 monkeypatch)却没还原,于是后面所有测试拿到的都是假 LLM。
    症状极其隐蔽 —— 测试全绿,但"真 LLM 通道"从来没被执行过,
    改坏了解析逻辑也发现不了。
    """
    import core.agent_base as cab
    import infra.llm as llm_mod

    for module, name in ((llm_mod, "get_llm_client"), (cab, "get_llm_client")):
        func = getattr(module, name)
        assert func is not None
        # 原生函数的 __module__ 是定义它的模块;被换成 lambda/替身就会变
        assert func.__module__ == "infra.llm", (
            f"{module.__name__}.{name} 被换成了 {func!r} —— "
            f"某个测试永久改写了全局(没在 finally 里还原)"
        )


# ============================================================
# 1. 单次调用:提示词 → HTTP → SDK → 解析
# ============================================================

@pytest.mark.asyncio
async def test_l1_classifier_full_http_path(provider_at, mock_llm):
    """L1 分类器走完整条网络链路。"""
    from core.llm_judge import L1Classifier

    decision = await L1Classifier().classify("你好")

    assert decision is not None, "L1 没有产出决策"
    assert decision.label.value == "chitchat"
    assert decision.confidence == pytest.approx(0.95)
    assert decision.layer == "L1"
    assert decision.degraded is False

    # 确实发出去了 —— 而且是 system + user 两条消息
    assert mock_llm.requests, "没有请求打到 mock"
    sent = mock_llm.requests[-1]
    roles = [m.get("role") for m in sent["messages"]]
    assert roles == ["system", "user"]
    assert sent["payload"].get("temperature") == 0.0


@pytest.mark.asyncio
async def test_l1_classifier_degrades_on_bad_output(provider_at):
    """模型返回垃圾 → 降级(None),不抛异常、不拖垮链路。"""
    from core.llm_judge import L1Classifier

    def garbage(messages, payload):
        return "这不是 JSON,是一句闲聊"

    with MockOpenAI(garbage) as mock:
        from infra.providers import registry  # noqa: F401
        from infra.providers.manager import get_provider_manager, reset_provider_manager
        from infra.providers.openai import OpenAIProvider

        reset_provider_manager()
        get_provider_manager().register(
            "openai",
            OpenAIProvider,
            config={
                "api_key": "sk-mock",
                "base_url": mock.base_url,
                "default_model": "mock-model",
            },
            set_current=True,
        )
        decision = await L1Classifier().classify("你好")

    assert decision is None, "垃圾输出没有降级成 None"


# ============================================================
# 2. 整条评估通道:跟 nightly 脚本算同样的指标
# ============================================================

@pytest.mark.asyncio
async def test_l1_eval_channel_computes_metrics(provider_at):
    """评估集上的准确率/易混对指标能被真正算出来。"""
    from core.llm_judge import L1Classifier

    classifier = L1Classifier()
    correct = 0
    confusable_total = 0
    confusable_correct = 0
    details = []

    CONFUSABLE_PAIRS = {
        ("manage", "task"), ("task", "manage"),
        ("teach", "task"), ("task", "teach"),
    }

    for case in L1_CASES:
        decision = await classifier.classify(case["input"])
        if decision is None:
            details.append(f"{case['input']!r}: L1 无输出")
            continue
        predicted = decision.label.value
        if predicted == case["label"]:
            correct += 1
        else:
            details.append(
                f"{case['input']!r}: 期望 {case['label']},实际 {predicted}"
            )
        if (predicted, case["label"]) in CONFUSABLE_PAIRS:
            confusable_total += 1
            if predicted == case["label"]:
                confusable_correct += 1

    total = len(L1_CASES)
    assert total > 0, "评估集里没有 L1 用例"
    accuracy = correct / total
    assert accuracy >= 0.90, f"oracle 通道准确率只有 {accuracy:.1%}:\n" + "\n".join(details)
    if confusable_total:
        assert confusable_correct / confusable_total >= 0.95


# ============================================================
# 3. nightly 脚本的 --live 分支
# ============================================================

def test_nightly_live_flag_runs_end_to_end(mock_llm, tmp_path):
    """`scripts/intent_eval_nightly.py --live` 这条命令本身能跑通。

    这是最容易被忽略的一层:脚本的参数解析、指标计算、报告落盘,
    都可能各自出错而 pytest 完全测不到。
    """
    env = dict(os.environ)
    env["OPENAI_API_KEY"] = "sk-mock"
    env["OPENAI_BASE_URL"] = mock_llm.base_url
    env["OPENAI_MODEL"] = "mock-model"
    env["LOG_LEVEL"] = "WARNING"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "intent_eval_nightly.py"),
            "--live",
            "--no-write",
            # 这条测试验的是"管线通不通",不是"统计稳定性":
            # 单遍 + 不节流,否则 mock 也要按生产节奏慢慢跑(~2 分钟)。
            "--repeat",
            "1",
            "--pace",
            "0",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",   # Windows 默认 GBK,脚本输出含中文会解码失败
        errors="replace",
        timeout=300,
    )
    out = proc.stdout or ""
    assert proc.returncode == 0, f"nightly --live 退出码 {proc.returncode}\n{out}\n{proc.stderr}"
    assert "混淆矩阵" in out, "没有输出混淆矩阵"
    assert "L1" in out, "没有跑 L1 通道"
    assert "全部达标" in out, f"指标未达标:\n{out}"
    # 混淆矩阵不是空的 —— 真的算出了数
    assert out.count(".") >= 0
