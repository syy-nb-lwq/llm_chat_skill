"""工具自修复 —— agent 自己造的工具坏了，让它自己改一版。

## 为什么必须只修"自己造的"

内置工具（`tools/*.py`）是**我们的代码**。让 agent 自动改写它等于
让它改自己的权限边界 —— 不可接受，也调试不了。

所以这里划一条硬线：

| 工具来源 | 坏了怎么办 |
|---|---|
| agent 用 `create_tool` 造的 | **自动出 v2**：改代码 → 校验 → 跑测试 → 重新发布 |
| 内置 / MCP | 只**报告 + 提示别用**（`core/tool_health.py` 的 health_block），源码不动 |

## 触发条件（宁可不动，不要乱改）

- 该工具在健康账本里已判为不健康（有样本门槛，见 `tool_health.MIN_SAMPLES`）
- 能找到对应的**已发布提案**（说明是 agent 造的）
- 最近错误信息非空（没有错误样本就无从修起）

## 修完怎么验证

沿用造工具时那套闸门：`validate_proposal`（语法/安全检查）+ 跑 `test_cases`。
**测试不过就不替换** —— 宁可保留坏的旧版并报告，也不要换上一个更坏的新版。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from infra.logger import get_logger


#: 一次最多修几个（别在一轮里烧掉太多调用）
MAX_REPAIRS_PER_RUN = 1


REPAIR_PROMPT = """你在修一个**自己造的、现在跑不通的**工具。

## 工具信息
- 名字：{name}
- 用途：{description}
- 类型：{kind}

## 当前实现
```
{current}
```

## 最近的失败记录（真实调用产生）
{errors}

## 要求

1. 找出上面这些错误的原因，给出**修正后的实现**
2. 保持工具名、用途、参数表不变（调用方已经按这个约定在用）
3. 只输出 JSON：
   - `kind=code` 时：`{{"implementation": "<完整 Python 代码>"}}`
   - `kind=http` 时：`{{"url_template": "...", "params": [...], "response_path": "..."}}`
4. 代码里用 `PARAMS['名']` 取参数，用 `return` 给结果
5. 不要引入新的外部依赖（标准库 + requests/httpx 可用）

如果错误是**环境问题**（缺少外部程序、缺少密钥、接口未授权），
而不是代码问题，请只输出：`{{"unfixable": "<一句话说明缺什么>"}}`
"""


@dataclass
class RepairResult:
    tool: str
    attempted: bool = False
    published: bool = False
    reason: str = ""
    revision: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "attempted": self.attempted,
            "published": self.published,
            "reason": self.reason,
            "revision": self.revision,
        }


async def repair_tool(
    tool_name: str,
    *,
    hub=None,
    store=None,
    llm=None,
) -> RepairResult:
    """尝试修一个 agent 造的工具。修不好就如实报告，不折腾。"""
    logger = get_logger()
    result = RepairResult(tool=tool_name)

    # --- 1. 必须是 agent 造的 ---
    if store is None:
        from tools.proposal import get_proposal_store

        store = get_proposal_store()

    proposal = None
    for p in store.list():
        if p.name == tool_name:
            proposal = p
            break

    if proposal is None:
        result.reason = "不是 agent 造的工具（找不到提案），不动内置源码"
        return result

    # --- 2. 必须真的坏了 ---
    from core.tool_health import MIN_SAMPLES, get_health_store

    health = get_health_store().health(tool_name)
    if health is None or health.total < MIN_SAMPLES:
        result.reason = "样本不足，暂不修"
        return result
    if health.healthy:
        result.reason = f"当前判定健康（{health.reason}），不修"
        return result
    if not health.recent_errors:
        result.reason = "没有可用的错误信息，无从修起"
        return result

    result.attempted = True

    # --- 3. 让 LLM 给出修正版 ---
    current = (
        proposal.implementation
        if proposal.kind in ("code", "script")
        else _http_spec_text(proposal)
    )
    try:
        if llm is None:
            from infra.llm import get_llm_client

            llm = get_llm_client()
        payload = await llm.chat_with_retry(
            [{
                "role": "user",
                "content": REPAIR_PROMPT.format(
                    name=proposal.name,
                    description=proposal.description,
                    kind=proposal.kind,
                    current=current[:4000],
                    errors="\n".join(f"- {e}" for e in health.recent_errors),
                ),
            }],
            temperature=0.1,
            max_retries=2,
        )
    except Exception as exc:
        result.reason = f"调用模型失败：{exc}"
        return result

    from core.skill_harvest import _extract_json

    patch = _extract_json(payload or "")
    if not patch:
        result.reason = "模型输出不是合法 JSON"
        return result

    if patch.get("unfixable"):
        result.reason = f"判定为环境问题，不该改代码：{patch['unfixable']}"
        # 记成能力缺口，让工具自增长/人去看
        try:
            from core.tool_health import get_health_store as _hs

            _hs().record_gap(
                f"{tool_name} 不可用：{patch['unfixable']}",
                tried=f"自动修复 {tool_name}",
                blocked_by=str(patch["unfixable"])[:300],
            )
        except Exception:
            pass
        return result

    # --- 4. 校验候选 ---
    candidate = copy.deepcopy(proposal)
    if proposal.kind in ("code", "script"):
        impl = str(patch.get("implementation") or "").strip()
        if not impl:
            result.reason = "模型没给出 implementation"
            return result
        candidate.implementation = impl
    else:
        if patch.get("url_template"):
            candidate.url_template = str(patch["url_template"])
        if isinstance(patch.get("params"), list):
            candidate.params = patch["params"]
        if patch.get("response_path") is not None:
            candidate.response_path = str(patch["response_path"])

    from tools.proposal import build_tool, validate_proposal

    other_names = [n for n in (hub.names() if hub else []) if n != tool_name]
    errors, warnings = validate_proposal(candidate, existing_names=other_names)
    if errors:
        result.reason = "修正版没通过校验：" + "; ".join(errors[:3])
        logger.warning("ToolRepair", f"{tool_name} 修正版校验失败: {errors[:2]}")
        return result

    # --- 5. 跑测试用例（有的话）---
    test_errors = await _run_tests(candidate)
    if test_errors:
        result.reason = "修正版测试未通过：" + "; ".join(test_errors[:2])
        return result

    # --- 6. 替换 ---
    if hub is None:
        from tools.hub import get_tool_hub

        hub = get_tool_hub()

    try:
        old_rev = int((proposal.validation or {}).get("revision", 0))
        hub.unregister_tool(tool_name)
        hub.register_python_tool(build_tool(candidate))
    except Exception as exc:
        result.reason = f"替换工具失败：{exc}"
        return result

    candidate.validation = dict(candidate.validation or {})
    candidate.validation["revision"] = old_rev + 1
    candidate.validation["repaired_from_errors"] = health.recent_errors[:3]
    candidate.status = "published"
    candidate.reviewed_by = "auto-repair"
    store.save(candidate)

    result.published = True
    result.revision = old_rev + 1
    result.reason = f"已发布 v{result.revision + 1}（原错误：{health.recent_errors[0][:80]}）"
    logger.info("ToolRepair", f"{tool_name} 自动修复成功 -> rev {result.revision}")
    return result


async def auto_repair(*, hub=None, max_repairs: int = MAX_REPAIRS_PER_RUN) -> List[RepairResult]:
    """扫一遍不健康的工具，挑 agent 造的尝试修。"""
    from core.tool_health import get_health_store

    results: List[RepairResult] = []
    for h in get_health_store().unhealthy():
        if len(results) >= max_repairs:
            break
        try:
            r = await repair_tool(h.tool, hub=hub)
        except Exception as exc:
            r = RepairResult(tool=h.tool, reason=f"修复过程异常：{exc}")
        results.append(r)
    return results


async def _run_tests(candidate) -> List[str]:
    """跑提案自带的 test_cases。没有用例就返回空（不阻塞修复）。"""
    cases = [c for c in (candidate.test_cases or []) if isinstance(c, dict)]
    if not cases:
        return []
    from tools.proposal import build_tool

    try:
        tool = build_tool(candidate)
    except Exception as exc:
        return [f"无法构造工具：{exc}"]

    problems = []
    for idx, case in enumerate(cases, 1):
        params = case.get("params") or {}
        expect = bool(case.get("expect_success", True))
        try:
            r = await tool.execute(**params)
        except Exception as exc:
            problems.append(f"用例 {idx} 抛异常：{exc}")
            continue
        if bool(r.success) != expect:
            problems.append(
                f"用例 {idx} 期望 success={expect} 实际 {r.success}"
            )
    try:
        await tool.aclose()
    except Exception:
        pass
    return problems


def _http_spec_text(proposal) -> str:
    return (
        f"method: {proposal.method}\n"
        f"url_template: {proposal.url_template}\n"
        f"params: {proposal.params}\n"
        f"response_path: {proposal.response_path}"
    )
