"""FastAPI backend service."""
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.session import sessions
from backend.websocket_handler import endpoint, register_handlers
from infra.auth import require_auth
from infra.config import ConfigError, config
from infra.logger import get_logger


app = FastAPI(title="Skill Agent API")
logger = get_logger()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

endpoint.register_route(app, "/pubsub")


class FeatureFlagUpdate(BaseModel):
    enabled: bool
    persist: bool = True


async def _gc_loop():
    while True:
        await asyncio.sleep(60)
        await sessions.gc()


async def _start_reflect_loop() -> bool:
    from core.reflect import SelfReflectLoop

    if getattr(app.state, "reflect_loop", None) is None:
        app.state.reflect_loop = SelfReflectLoop()
    await app.state.reflect_loop.start()
    return True


async def _stop_reflect_loop():
    reflect_loop = getattr(app.state, "reflect_loop", None)
    if reflect_loop is not None:
        await reflect_loop.stop()
        app.state.reflect_loop = None


@app.on_event("startup")
async def _startup():
    try:
        config.validate()
    except ConfigError as exc:
        logger.error("startup", f"config validation failed: {exc}")
        raise

    sessions.ttl_s = int(config.session_ttl_s)
    await register_handlers()

    from infra.providers.registry import init_providers_from_config

    init_providers_from_config()

    app.state.gc_task = asyncio.create_task(_gc_loop())
    app.state.reflect_loop = None

    from tools.hub import get_tool_hub
    from tools.sources.python_source import create_python_source

    hub = get_tool_hub()
    try:
        hub.register_source(
            create_python_source(
                name="builtin",
                directories=[str(Path(__file__).parent.parent / "tools")],
            )
        )
    except ValueError:
        pass

    # MCP 工具源(可选):配了 MCP_SERVERS 才会启用。
    # 每个服务器独立注册 —— 一个连不上不影响其它,也不影响启动。
    _register_mcp_sources(hub)

    try:
        await hub.connect_all()
        logger.info("startup", f"tool hub initialized with {len(hub.names())} tools")
    except Exception as exc:
        logger.warning("startup", f"tool hub connect failed: {exc}")

    # 恢复 agent 自己造过的工具 —— 否则每次重启它就"忘掉"学会的能力，
    # "能力自增长"就是假的。
    try:
        from tools.proposal import restore_published

        restored = restore_published(hub)
        if restored:
            logger.info(
                "startup",
                f"restored {restored} agent-created tools "
                f"(total {len(hub.names())})",
            )
    except Exception as exc:
        logger.warning("startup", f"restore agent tools failed: {exc}")

    if config.self_evolution_enabled:
        try:
            await _start_reflect_loop()
            logger.info("startup", "self reflection loop started")
        except Exception as exc:
            logger.warning("startup", f"self reflection loop start failed: {exc}")

    _init_long_term_memory()


def _register_mcp_sources(hub) -> int:
    """注册 MCP 工具源(可选)。返回成功注册的服务器数。

    设计要点:
    - **一个服务器挂掉不影响其它** —— 逐个 try,失败只记日志
    - **也不影响启动** —— MCP 全部连不上,agent 照常跑(只是少了那些工具)
    - **不静默**:连上了多少、失败多少,都进日志,`/api/health` 也能看到

    安全提醒:MCP 服务器是第三方进程(`npx`/`uvx` 拉的),等于引入了
    第三方代码执行面。**只应由管理员配置**,不要让终端用户自助添加。
    """
    if not config.mcp_enabled:
        return 0

    from tools.sources.mcp_source import MCPSource, parse_mcp_servers

    try:
        sources = parse_mcp_servers(config.mcp_servers)
    except Exception as exc:
        logger.warning("startup", f"解析 MCP_SERVERS 失败,已跳过 MCP: {exc}")
        return 0

    if not sources:
        return 0

    registered = 0
    for src in sources:
        try:
            hub.register_source(MCPSource(src))
            registered += 1
        except ValueError:
            # 同名已注册 —— 跳过,不报错
            continue
        except Exception as exc:
            logger.warning("startup", f"注册 MCP 服务器 {src.name} 失败: {exc}")

    logger.info("startup", f"已注册 {registered}/{len(sources)} 个 MCP 服务器")
    return registered


def _init_long_term_memory() -> None:
    """初始化长期记忆(用户画像)的 embedding 并绑定到存储。

    不接线的话 `SemanticMemoryStore.embedding_service` 恒为 None,
    向量检索整条路径是死的(docs/13 §1.2 R3)。

    **但"接线"不等于"该用"**:`EMBEDDING_PROVIDER` 留空时只做 FTS 检索,
    这样反而比接一个随机向量服务准确 —— 详见 `infra/embedding.py` 里
    `is_synthetic` 的说明。失败一律降级为 FTS-only。
    """
    if not config.semantic_memory_enabled:
        logger.info("startup", "semantic memory disabled, skip embedding init")
        return

    provider = (config.embedding_provider or "").strip()
    if not provider:
        logger.info(
            "startup",
            "未配置 EMBEDDING_PROVIDER:长期记忆只用 FTS + 常驻注入"
            "(要语义检索请配 openai/local)",
        )
        return

    try:
        from core.semantic_memory import get_semantic_memory
        from infra.embedding import init_embedding_service

        api_key = config.embedding_api_key or config.openai_api_key
        base_url = config.embedding_base_url or config.openai_base_url
        service = init_embedding_service(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=config.embedding_model,
            dimension=config.embedding_dimension,
        )
        store = get_semantic_memory()
        store.min_similarity = float(config.embedding_min_similarity)
        enabled = store.set_embedding_service(service)
        logger.info(
            "startup",
            f"embedding 已初始化: provider={provider}, 语义通道={'开' if enabled else '关(降级 FTS)'}",
        )
    except Exception as exc:
        logger.warning("startup", f"embedding 初始化失败(降级 FTS-only): {exc}")


@app.on_event("shutdown")
async def _shutdown():
    gc_task = getattr(app.state, "gc_task", None)
    if gc_task is not None:
        gc_task.cancel()
        try:
            await gc_task
        except asyncio.CancelledError:
            pass

    await _stop_reflect_loop()

    from tools.hub import get_tool_hub

    try:
        await get_tool_hub().disconnect_all()
    except Exception as exc:
        logger.warning("shutdown", f"tool hub shutdown failed: {exc}")


@app.get("/")
async def root():
    return {"status": "ok", "service": "Skill Agent", "version": "1.0"}


@app.get("/api/health")
async def health():
    """健康检查端点(M0-06:暴露工具源状态)。

    字段:
      - status: 顶层健康状态("ok" / "degraded")
      - self_evolution_enabled: 是否开启自演化
      - tool_sources: 工具源汇总(total_sources / connected / failed / disconnected / has_failures)
      - sources: 每个源的详细状态(name / type / enabled / connected / state / error / tool_count)
    """
    from tools.hub import get_tool_hub

    try:
        tool_health = get_tool_hub().health_summary()
    except Exception as exc:
        # hub 自身异常不应让健康检查失败,降级返回
        logger.warning("health", f"tool hub health failed: {exc}")
        tool_health = {
            "total_sources": 0,
            "connected": 0,
            "failed": 0,
            "disconnected": 0,
            "sources": {},
            "has_failures": False,
            "error": f"{type(exc).__name__}: {exc}",
        }

    overall = "degraded" if tool_health.get("has_failures") else "ok"

    return {
        "status": overall,
        "self_evolution_enabled": bool(config.self_evolution_enabled),
        "tool_sources": {
            "total_sources": tool_health["total_sources"],
            "connected": tool_health["connected"],
            "failed": tool_health["failed"],
            "disconnected": tool_health["disconnected"],
            "has_failures": tool_health["has_failures"],
        },
        "sources": tool_health["sources"],
    }


@app.get("/api/features")
async def get_features():
    return {
        "self_evolution_enabled": bool(config.self_evolution_enabled),
        "skill_dag_enabled": bool(config.skill_dag_enabled),
        "semantic_memory_enabled": bool(config.semantic_memory_enabled),
    }


# ===== 会话内容(短期记忆) =====


@app.get("/api/sessions")
@require_auth
async def list_sessions():
    """列出全部会话（本地单用户）。"""
    from core.memorykit import get_memory

    rows = get_memory().short_term.list_sessions()
    return {
        "sessions": [
            {
                "session_id": r["session_id"],
                "message_count": r["message_count"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    }


@app.get("/api/session/messages")
async def get_my_session_messages(request: Request, limit: int = 200):
    """取**当前用户当前会话**的消息 —— 前端刷新后恢复对话用。

    **为什么不用 `/api/sessions/{sid}/messages`**:那条是管理员接口
    (`require_auth` 验的是 owner token)。普通前端只有会话令牌,没有 owner token,
    一旦配了 `OWNER_TOKEN`,前端就取不到自己的历史了。

    session_id 从 query 参数取（本地单用户，无越权问题）。
    """
    from core.memorykit import get_memory

    session_id = _session_id_from_request(request)
    if not session_id:
        # 还没拿到 session_id（首轮连接）—— 空历史比 400 更合适
        return {"session_id": "", "messages": [], "count": 0}

    short_term = get_memory().short_term
    if not short_term.session_exists(session_id):
        # 会话还没落盘(全新会话)—— 空历史比 404 更合适,前端不用特殊处理
        return {"session_id": session_id, "messages": [], "count": 0}

    msgs = short_term.messages(session_id, limit=max(0, min(limit, 1000)))
    return {
        "session_id": session_id,
        "count": len(msgs),
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at}
            for m in msgs
        ],
    }


@app.get("/api/sessions/{session_id}/messages")
@require_auth
async def get_session_messages(session_id: str, limit: int = 100):
    """取某个会话的消息(短期记忆内容全量保留)。"""
    from core.memorykit import get_memory

    short_term = get_memory().short_term
    if not short_term.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    msgs = short_term.messages(session_id, limit=limit or 0)
    return {
        "session_id": session_id,
        "messages": [m.to_dict() for m in msgs],
    }


@app.delete("/api/sessions/{session_id}")
@require_auth
async def delete_session(session_id: str):
    """删除会话及其全部内容(用户主动要求清除时使用)。"""
    from core.memorykit import get_memory

    ok = get_memory().short_term.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return {"deleted": session_id}


# ===== 长期记忆(用户画像) =====


@app.get("/api/memories")
@require_auth
async def list_memories(limit: int = 50):
    """「你记住了我什么」：列出画像条目（本地单用户）。"""

    from core.memorykit import get_memory

    entries = get_memory().long_term.profile_entries(limit=limit)
    return {
        "count": len(entries),
        "memories": [
            {
                "id": e.id,
                "type": e.type,
                "content": e.content,
                "confidence": e.confidence,
                "importance": e.importance,
                "scope": e.scope,
                "status": e.status,
                "source_session_id": e.source_session_id,
                "source_turn_id": e.source_turn_id,
                "created_at": e.created_at,
            }
            for e in entries
        ],
    }


@app.delete("/api/memories/{memory_id}")
@require_auth
async def forget_memory(memory_id: str):
    """「忘掉这件事」:软删除 + 同步清索引(删完不再被召回)。"""
    from core.memorykit import get_memory

    long_term = get_memory().long_term
    entry = long_term.get_profile_entry(memory_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"memory not found: {memory_id}")
    ok = long_term.forget(memory_id)
    if not ok:
        raise HTTPException(status_code=500, detail="forget failed")
    return {"forgotten": memory_id, "type": entry.type}


@app.get("/api/traces")
async def list_traces(limit: int = 50):
    """最近几轮的分阶段耗时(产品化观测接口)。

    没有它就只能靠猜"为什么慢";有了它就能定位到
    L1 多打了一次模型 / 工具超时 / 首 token 迟迟不来。
    """
    from core.trace import get_trace_recorder

    rows = get_trace_recorder().read(limit=max(1, min(limit, 500)))
    return {"count": len(rows), "traces": rows}


@app.post("/api/features/self-evolution")
@require_auth
async def set_self_evolution(payload: FeatureFlagUpdate):
    enabled = config.set_feature_flag(
        "self_evolution_enabled",
        payload.enabled,
        persist=payload.persist,
    )
    if enabled:
        await _start_reflect_loop()
    else:
        await _stop_reflect_loop()
    return {"self_evolution_enabled": enabled, "persisted": payload.persist}


@app.get("/api/skills")
async def list_skills():
    from skills.manager import get_skill_store

    store = get_skill_store()
    # 管理界面要能看到被降级的技能(带状态),所以这里不过滤
    skills = store.list_all_skills(include_demoted=True)
    qualities = {}
    try:
        from core.skill_quality import get_skill_quality_store

        qualities = {
            name: q.to_dict() for name, q in get_skill_quality_store().all().items()
        }
    except Exception as exc:
        logger.warning("skill_quality", f"读取质量账本失败: {exc}")

    return {
        "skills": [
            {
                "id": getattr(skill, "id", None),
                "name": skill.name,
                "version": getattr(skill, "version", "1.0.0"),
                "capability": skill.capability,
                "patterns": skill.patterns,
                "tags": skill.tags,
                "method": skill.method,
                "source": getattr(skill, "source", "builtin"),
                "author": getattr(skill, "author", None),
                "created_at": getattr(skill, "created_at", None),
                "updated_at": getattr(skill, "updated_at", None),
                "steps": [step.to_dict() for step in skill.steps],
                "quality": qualities.get(skill.name),
            }
            for skill in skills
        ]
    }


@app.get("/api/skills/quality")
async def list_skill_quality():
    """技能质量台账 —— 每个技能的通过率与降级状态(docs/14 §2.4)。"""
    from core.skill_quality import get_skill_quality_store

    store = get_skill_quality_store()
    qualities = store.all()
    return {
        "skills": {name: q.to_dict() for name, q in qualities.items()},
        "demoted": sorted(store.demoted_skills()),
    }


@app.delete("/api/skills/{name}")
@require_auth
async def delete_skill(name: str):
    from skills.manager import get_skill_store

    store = get_skill_store()
    removed = store.delete_by_name(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return {"deleted": name, "files": removed}


@app.delete("/api/skills/{name}/{version}")
@require_auth
async def delete_skill_version(name: str, version: str):
    from skills.manager import get_skill_store

    store = get_skill_store()
    removed = store.delete_version(name, version)
    if not removed:
        raise HTTPException(status_code=404, detail=f"skill version not found: {name}@{version}")
    return {"deleted": name, "version": version, "files": removed}


@app.post("/api/skills/reload")
@require_auth
async def reload_skills():
    from skills.manager import get_skill_store

    store = get_skill_store()
    store.reload()
    return {"reloaded": True, "count": len(store.list_all())}


# ===== M1-06 / M1-08: TeachingSession API =====

@app.get("/api/teachings")
async def list_teachings(session_id: str = "default"):
    """列出当前 session 上的活跃教学会话(用于重复技能决策 UI)。"""
    from agents.teaching_session import get_teaching_store
    store = get_teaching_store()
    ts = store.find_active_for(session_id)
    if not ts:
        return {"active": None}
    return {
        "active": {
            "teaching_session_id": ts.teaching_session_id,
            "status": ts.status,
            "missing_fields": ts.missing_fields,
            "current_question": ts.current_question,
            "duplicate_of": ts.duplicate_of,
            "user_choice": ts.user_choice,
            "draft": ts.draft_skill,
        }
    }


@app.post("/api/teachings/cancel")
@require_auth
async def cancel_teaching(session_id: str = "default"):
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok = t.cancel(session_id)
    return {"cancelled": ok}


@app.post("/api/teachings/choose")
@require_auth
async def choose_teaching_decision(
    choice: str,
    session_id: str = "default",
):
    """处理用户在重复技能上的决策:reuse / update_new / cancel。"""
    from agents.teaching_session import get_teaching_store, TeachingStatus
    store = get_teaching_store()
    ts = store.find_active_for(session_id)
    if not ts:
        return {"ok": False, "error": "no active teaching"}
    ts.user_choice = choice
    if choice == "cancel":
        ts.status = TeachingStatus.CANCELLED
    elif choice == "reuse":
        ts.status = TeachingStatus.ACTIVE
    elif choice == "update_new":
        # 标记后续 start_or_continue 走新版本流程
        ts.status = TeachingStatus.COLLECTING
    store.save(ts)
    return {"ok": True, "status": ts.status, "user_choice": ts.user_choice}


@app.post("/api/teachings/confirm")
@require_auth
async def confirm_teaching(session_id: str = "default"):
    """用户在草稿 UI 上点确认发布时调用。"""
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok, msg, skill = t.confirm_and_publish(session_id)
    if not ok:
        return {"ok": False, "error": msg}
    return {
        "ok": True,
        "message": msg,
        "skill": {
            "name": skill.name,
            "version": skill.version,
            "capability": skill.capability,
        } if skill else None,
    }


@app.get("/api/tools")
async def list_tools():
    from agents.learning import LearningAgent
    from tools.base import get_tool_registry

    learning = LearningAgent()
    tools = learning.tools.all() or get_tool_registry().all()
    return {
        "tools": [{"name": tool.name, "description": tool.description} for tool in tools]
    }


# ===== 自制工具提案(agent 自己造的工具)=====
#
# 只读的声明式工具/代码工具会自动发布(见 tools/proposal.py 的风险分级);
# 会改外部状态的(写方法、带密钥)进待审队列,**必须人工批准**。
# 这几个接口是那个"人工批准"的入口。

@app.get("/api/tool-proposals")
async def list_tool_proposals(status: str = ""):
    """列出工具提案(可按 status 过滤: pending / published / rejected)。"""
    from tools.proposal import get_proposal_store

    store = get_proposal_store()
    items = store.list(status or None)
    return {
        "count": len(items),
        "proposals": [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "kind": p.kind,
                "method": p.method,
                "url_template": p.url_template,
                "status": p.status,
                "risk": p.risk,
                "rationale": p.rationale,
                "created_at": p.created_at,
                "reviewed_by": p.reviewed_by,
                "blocked_reason": p.blocked_reason,
                "implementation": (p.implementation or "")[:2000],
                "validation": p.validation,
            }
            for p in items
        ],
    }


@app.post("/api/tool-proposals/{proposal_id}/approve")
@require_auth
async def approve_tool_proposal(proposal_id: str):
    """人工批准一个待审工具（发布后 planner 立刻能看到）。"""
    from tools.hub import get_tool_hub
    from tools.proposal import approve

    ok, msg = approve(proposal_id, get_tool_hub(), reviewer="human")
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"approved": proposal_id, "message": msg}


@app.post("/api/tool-proposals/{proposal_id}/reject")
@require_auth
async def reject_tool_proposal(proposal_id: str, payload: Optional[dict] = None):
    """人工拒绝一个提案。"""
    from tools.proposal import reject

    reason = str((payload or {}).get("reason") or "")
    if not reject(proposal_id, reviewer="human", reason=reason):
        raise HTTPException(status_code=404, detail=f"提案不存在: {proposal_id}")
    return {"rejected": proposal_id}


@app.delete("/api/tool-proposals/{proposal_id}")
@require_auth
async def delete_tool_proposal(proposal_id: str):
    """删除一个提案；如果它是已发布状态,同时把工具撤下来。"""
    from tools.hub import get_tool_hub
    from tools.proposal import get_proposal_store, unpublish

    store = get_proposal_store()
    proposal = store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提案不存在: {proposal_id}")

    removed = False
    if proposal.status == "published":
        removed = unpublish(proposal, get_tool_hub())
    store.delete(proposal_id)
    return {"deleted": proposal_id, "tool_unregistered": removed}


# ===== 方法论草稿（agent 从成功任务里提炼出来的技能）=====
#
# 「工具自增长」做完之后，方法论也得能自增长 —— 否则每做成一件新事，
# 经验就丢一次。但**垃圾技能比没有技能更糟**（污染检索、占注入预算、
# 可能被误选），所以提炼出来一律进草稿队列，人批准才生效。

@app.get("/api/skill-drafts")
async def list_skill_drafts():
    """列出待批准的方法论草稿。"""
    from core.skill_harvest import get_draft_store

    drafts = get_draft_store().list()
    return {"count": len(drafts), "drafts": drafts}


@app.post("/api/skill-drafts/{draft_id}/approve")
@require_auth
async def approve_skill_draft(draft_id: str):
    """批准一个方法论草稿 → 进技能库并激活（planner 立刻能看到）。"""
    from core.skill_harvest import approve_draft

    ok, msg = approve_draft(draft_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"approved": draft_id, "message": msg}


@app.delete("/api/skill-drafts/{draft_id}")
@require_auth
async def delete_skill_draft(draft_id: str):
    """丢弃一个方法论草稿。"""
    from core.skill_harvest import get_draft_store

    if not get_draft_store().delete(draft_id):
        raise HTTPException(status_code=404, detail=f"草稿不存在: {draft_id}")
    return {"deleted": draft_id}


# ===== 自诊断 / 自修复（让"它自己发现并修"这件事可观测）=====

@app.get("/api/tool-health")
async def tool_health(only_unhealthy: bool = False):
    """工具健康度：真实调用记录算出来的成功率与最近错误。"""
    from core.tool_health import get_health_store

    store = get_health_store()
    items = store.unhealthy() if only_unhealthy else store.snapshot()
    return {
        "count": len(items),
        "unhealthy": len(store.unhealthy()),
        "tools": [h.to_dict() for h in items],
    }


@app.get("/api/capability-gaps")
async def capability_gaps(limit: int = 50):
    """能力缺口清单：agent 自己申报的"我缺什么"。"""
    from core.tool_health import get_health_store

    gaps = get_health_store().list_gaps(limit=limit)
    return {"count": len(gaps), "gaps": gaps}


@app.post("/api/tool-repair/{tool_name}")
@require_auth
async def trigger_tool_repair(tool_name: str):
    """手动触发一次工具自修复（只对 agent 造的工具有效）。"""
    from core.tool_repair import repair_tool

    result = await repair_tool(tool_name)
    if not result.attempted:
        return {"tool": tool_name, "attempted": False, "reason": result.reason}
    return result.to_dict()


@app.get("/api/patches")
async def list_patches():
    from core.memory import get_memory_store

    store = get_memory_store()
    patches = store.get_pending_patches()
    return {
        "patches": [
            {
                "id": patch.id,
                "trace_id": patch.trace_id,
                "timestamp": patch.timestamp,
                "target_skill": patch.target_skill,
                "patch_type": patch.patch_type,
                "diagnosis": patch.diagnosis,
                "suggestion": patch.suggestion,
                "confidence": patch.confidence,
                "status": patch.status,
                # M3-05:风险等级与校验结果
                "risk_level": patch.risk_level,
                "validation": patch.validation,
                "blocked_reason": patch.blocked_reason,
            }
            for patch in patches
        ]
    }


@app.post("/api/patches/{patch_id}/approve")
@require_auth
async def approve_patch(patch_id: str):
    """人工审批补丁(M3-04:审批前跑新旧样例校验,不再盲目写盘)。"""
    from core.memory import get_memory_store
    from core.result_validator import ResultValidator
    from skills.manager import get_skill_store
    from skills.validator import new_issues, split_issues, validate_skill
    from tools.hub import get_tool_hub

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    store = get_memory_store()
    patch_file = store.patches_dir / f"{patch_id}.json"
    if not patch_file.exists():
        raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")

    data = json.loads(patch_file.read_text(encoding="utf-8"))
    suggestion = data.get("suggestion", {}) or {}
    target_skill = suggestion.get("target_skill") or data.get("target_skill")
    updates = {}
    # 支持多种字段更新,不只限于 method
    for field in ("method", "capability", "patterns"):
        if field in suggestion:
            updates[field] = suggestion[field]

    skill_store = get_skill_store()

    # ---- 发布前校验(M3-03/M4)----
    validation: Dict = {}
    risk_level = "unknown"
    if target_skill and updates:
        candidate = skill_store.preview_update(target_skill, updates)
        if candidate is None:
            raise HTTPException(
                status_code=400, detail=f"找不到目标技能: {target_skill}"
            )
        try:
            tool_names = list(get_tool_hub().names() or [])
        except Exception:
            tool_names = []

        # 基线(当前线上版本)已有的问题不阻断补丁,只拦新引入的问题
        baseline = skill_store.get_by_name(target_skill)
        baseline_errors, _ = (
            split_issues(validate_skill(baseline, tool_names)) if baseline else ([], [])
        )
        candidate_errors, warnings = split_issues(validate_skill(candidate, tool_names))
        errors = new_issues(baseline_errors, candidate_errors)

        report = await ResultValidator().validate(
            candidate, static_issues=errors, warnings=warnings
        )
        # 记进技能质量账本 —— 验收样例的历史通过率(docs/14 §2.4)
        try:
            from core.skill_quality import get_skill_quality_store

            get_skill_quality_store().record_sample_run(
                target_skill,
                version=candidate.version,
                total=report.total,
                passed=report.passed,
                failed_cases=[c.name for c in report.cases if not c.passed],
            )
        except Exception as exc:  # 质量记账失败不该阻断发布
            logger.warning("skill_quality", f"记录样例结果失败: {exc}")
        validation = report.to_dict()
        risk_level = report.risk_level
        # 新引入的静态错误 → 硬拦;回归样例失败 → 仅提示(人工已明确批准)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "静态校验未通过,拒绝发布",
                    "issues": errors,
                    "validation": validation,
                },
            )

    # 记录审批动作(即便落盘失败也要留痕)
    if not store.approve_patch(patch_id, "human"):
        raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")

    applied_files = []
    if target_skill and updates:
        applied_files = skill_store.update_skill(target_skill, updates)

    if applied_files:
        store.write_audit(
            "manual_publish",
            {
                "patch_id": patch_id,
                "target_skill": target_skill,
                "diagnosis": data.get("diagnosis", ""),
                "updates": updates,
                "files": applied_files,
                "validation": validation,
                "reviewer": "human",
            },
        )

    return {
        "approved": patch_id,
        "applied": bool(applied_files),
        "files": applied_files,
        "validation": validation,
        "risk_level": risk_level,
    }


@app.post("/api/patches/{patch_id}/reject")
@require_auth
async def reject_patch(patch_id: str):
    from core.memory import get_memory_store

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    store = get_memory_store()
    if not store.reject_patch(patch_id, "human"):
        raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")
    return {"rejected": patch_id}


@app.get("/api/memory/stats")
async def memory_stats():
    from core.memory import get_memory_store

    return get_memory_store().get_stats()


# ===== 产物下载(文件/视频交付)=====
#
# `python_exec` / `write_script` 生成的文件落在 `memory/workspace/`,
# 但这些文件**前端拿不到** —— 只有 agent 自己能访问。
# 所以"帮我生成一个视频/报表"这种需求，产物明明做出来了，用户却看不到。
#
# 这里开一个受控的下载口：
#   - 只允许读 `memory/workspace/` 里面的文件（路径穿越一律拒）
#   - 需要会话令牌（和记忆面板同一套鉴权）
#   - 用 FileResponse，浏览器能直接预览/下载

#: 允许下载的目录(相对项目根)
_ARTIFACT_ROOT = Path(__file__).parent.parent / "memory" / "workspace"

#: 常见类型的 Content-Type(FileResponse 会按扩展名推断，这里只补少数几个)
_ARTIFACT_MEDIA_HINTS = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".html": "text/html; charset=utf-8",
}


def _resolve_artifact(name: str) -> Path:
    """把 URL 里的名字解析成 workspace 内的真实文件。

    **必须挡路径穿越**:`../../.env` 这类输入会让整个鉴权形同虚设。
    """
    raw = (name or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    # 统一分隔符，避免 Windows 反斜杠绕过
    raw = raw.replace("\\", "/")
    if raw.startswith("/") or ".." in raw.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")

    root = _ARTIFACT_ROOT.resolve()
    try:
        target = (root / raw).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="非法路径")

    try:
        target.relative_to(root)
    except ValueError:
        # 解析后跑到工作区外面去了
        raise HTTPException(status_code=403, detail="只能下载工作区内的文件")

    if not target.is_file():
        # 历史消息里的产物链接会一直留在对话里，文件却可能已经被清理/删掉了。
        # 只说"文件不存在"用户不知道该怎么办，所以给出可执行的下一步。
        raise HTTPException(
            status_code=404,
            detail=(
                f"文件不存在: {raw}。可能原因：① 该文件已被清理"
                "（清理产物目录、或换机器后没同步）；"
                "② agent 生成文件时用的目录不在产物区。"
                "解决办法：让 agent 重新生成，或用 publish_artifact 工具"
                "把已有文件登记成可下载产物。"
            ),
        )
    return target


def _resolve_artifact_user(request: Request) -> str:
    """产物接口（本地单用户，无鉴权）。恒返回固定值。"""
    return "local"


@app.get("/api/artifacts")
async def list_artifacts(request: Request, limit: int = 200):
    """列出工作区里的产物(给前端做「生成的文件」区)。"""
    _resolve_artifact_user(request)
    root = _ARTIFACT_ROOT
    if not root.exists():
        return {"artifacts": [], "count": 0}

    items = []
    for path in sorted(root.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not path.is_file():
            continue
        # `_run` 是 python_exec 的**常驻执行目录**(相对路径要跨调用有效)，
        # 里面的文件会作为产物复制到根目录。列出来会跟根目录重复，所以跳过。
        try:
            if "_run" in path.relative_to(root).parts:
                continue
        except Exception:
            pass
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        items.append({
            "name": path.name,
            "path": rel,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "url": f"/api/artifacts/{rel}",
        })
        if len(items) >= max(1, min(limit, 500)):
            break
    return {"artifacts": items, "count": len(items)}


@app.get("/api/artifacts/{name:path}")
async def download_artifact(name: str, request: Request, download: bool = False):
    """下载/预览工作区里的一个产物。

    `download=true` 强制下载(Content-Disposition: attachment),
    否则浏览器能预览的直接预览(视频、图片、PDF)。
    """
    _resolve_artifact_user(request)
    target = _resolve_artifact(name)

    media_type = _ARTIFACT_MEDIA_HINTS.get(target.suffix.lower())
    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=target.name,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{target.name}"'
                if download
                else "inline"
            ),
            # 产物可能被同名覆盖,别让浏览器缓存旧内容
            "Cache-Control": "no-cache",
        },
    )


# ===== 用户画像面板(A-09 / U-04)=====
#
# 「你记住了我什么 / 忘掉这件事」
#
# 本地单用户：没有令牌、没有用户切换。session_id 从 query 参数取，
# 只用于定位"当前会话"的记忆，不存在跨用户越权问题。

#: 画像类型的展示标签(与 build_profile_block 保持一致)
_PROFILE_LABELS = {
    "preference": "偏好",
    "habit": "习惯",
    "method": "方法",
    "constraint": "雷区",
    "fact": "事实",
}


def _session_id_from_request(request: Request) -> str:
    """从 query 参数取 session_id（本地单用户，无令牌、无租户）。"""
    return (request.query_params.get("session_id") or "").strip()


@app.get("/api/memory/overview")
async def memory_overview(request: Request):
    """记忆全景 —— 让用户看到"系统到底记得什么"。

    ## 为什么需要这个接口

    面板原来只展示"用户画像"（长期记忆）。而画像是**纯规则抽取**的
    （只认"我习惯…/我喜欢…"这类句式，这是刻意的：不用 LLM 抽取，
    避免幻觉污染记忆），日常下任务的对话几乎不会命中。

    于是用户看到的是"一条记忆都没有"，但系统里其实还有：
    会话内容、失败教训、待批方法论、待批补丁 —— 只是没暴露出来。

    **记忆不是一样东西，是分层的**（docs/13）：
    短期 = 会话内容；长期 = 用户画像；此外还有经验层（教训 / 方法论）。
    """
    import json as _json
    from pathlib import Path as _Path

    session_id = _session_id_from_request(request)

    out: Dict[str, Any] = {"session_id": session_id}

    # ---------- 短期记忆：本会话最近的消息 ----------
    short: Dict[str, Any] = {"count": 0, "recent": []}
    try:
        from core.session_memory import get_session_memory_store

        recent = get_session_memory_store().get_recent_messages(
            session_id, limit=40
        ) or []
        short["count"] = len(recent)
        short["recent"] = [
            {
                "role": m.get("role", ""),
                "content": str(m.get("content") or "")[:180],
            }
            for m in recent[-6:]
        ]
    except Exception as exc:
        short["error"] = str(exc)
    out["short_term"] = short

    # ---------- 长期记忆：用户画像 ----------
    profile: Dict[str, Any] = {"count": 0, "entries": [], "injected_chars": 0}
    try:
        from core.semantic_memory import get_semantic_memory

        smem = get_semantic_memory()
        entries = smem.get_profile(limit=100) or []
        block = smem.build_profile_block() or ""
        profile["count"] = len(entries)
        profile["injected_chars"] = len(block)
        profile["entries"] = [
            {
                "id": e.id,
                "type": _PROFILE_LABELS.get(e.type, e.type),
                "content": e.content,
                "confidence": round(float(e.confidence), 3),
            }
            for e in entries
        ]
    except Exception as exc:
        profile["error"] = str(exc)
    out["profile"] = profile

    # ---------- 经验：失败教训 ----------
    lessons: Dict[str, Any] = {"count": 0, "recent": []}
    try:
        fdir = _Path(__file__).parent.parent / "memory" / "failures"
        files = sorted(
            fdir.rglob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        mine = []
        for f in files:
            try:
                raw = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            mine.append(raw)
        lessons["count"] = len(mine)
        lessons["recent"] = [
            {
                "at": str(r.get("timestamp") or "")[:19],
                "diagnosis": str(r.get("diagnosis") or "")[:200],
            }
            for r in mine[:3]
        ]
    except Exception as exc:
        lessons["error"] = str(exc)
    out["lessons"] = lessons

    # ---------- 经验：待批方法论草稿 ----------
    drafts: Dict[str, Any] = {"count": 0, "recent": []}
    try:
        from core.skill_harvest import get_draft_store

        items = get_draft_store().list()
        drafts["count"] = len(items)
        drafts["recent"] = [
            {"name": d.get("name"), "capability": d.get("capability")}
            for d in items[:3]
        ]
    except Exception as exc:
        drafts["error"] = str(exc)
    out["skill_drafts"] = drafts

    # ---------- 经验：待批补丁 ----------
    patches: Dict[str, Any] = {"count": 0, "recent": []}
    try:
        from core.memory import get_memory_store

        items = get_memory_store().get_pending_patches()
        patches["count"] = len(items)
        patches["recent"] = [
            {"type": p.patch_type, "diagnosis": str(p.diagnosis or "")[:160]}
            for p in items[:3]
        ]
    except Exception as exc:
        patches["error"] = str(exc)
    out["patches"] = patches

    # ---------- 工具经验：健康度 ----------
    try:
        from core.tool_health import get_health_store

        store = get_health_store()
        snap = store.snapshot()
        out["tool_health"] = {
            "count": len(snap),
            "unhealthy": len([h for h in snap if not h.healthy]),
            "gaps": len(store.list_gaps(limit=200)),
        }
    except Exception as exc:
        out["tool_health"] = {"error": str(exc)}

    out["total"] = (
        profile.get("count", 0)
        + short.get("count", 0)
        + lessons.get("count", 0)
        + drafts.get("count", 0)
        + patches.get("count", 0)
    )
    return out


@app.get("/api/memory/profile")
async def get_profile(request: Request):
    """列出当前用户的画像(长期记忆)条目。

    同时返回**此刻正在注入**的那段文本 —— 让用户看清楚
    "现在到底哪些东西在影响我的回答"。
    """
    from core.semantic_memory import get_semantic_memory

    store = get_semantic_memory()

    entries = store.get_profile(limit=100)
    block = store.build_profile_block()

    items = []
    for e in entries:
        items.append({
            "id": e.id,
            "type": e.type,
            "label": _PROFILE_LABELS.get(e.type, e.type),
            "content": e.content,
            "confidence": round(float(e.confidence), 3),
            "importance": round(float(e.importance), 3),
            "created_at": e.created_at,
            "source_session_id": e.source_session_id or "",
        })

    return {
        "entries": items,
        "count": len(items),
        # 正在注入的画像块(用户能直观看到"生效中的"内容)
        "injected_block": block,
        "injected_chars": len(block),
    }


@app.delete("/api/memory/profile/{entry_id}")
async def forget_profile_entry(entry_id: str, request: Request):
    """「忘掉这一条」:软删除某条画像。

    用软删除而不是硬删 —— 删完立刻就不再被召回,
    但保留了"曾经记过"的事实,便于排查误删。
    """
    from core.semantic_memory import get_semantic_memory

    store = get_semantic_memory()

    # **先确认这条属于该用户** —— 不然可以拿别的用户的 entry_id 来删
    owned = [
        e for e in store.get_profile(limit=1000)
        if e.id == entry_id
    ]
    if not owned:
        # 也可能是已被删过/不在画像类型里 —— 一律 404,不泄露存在性
        raise HTTPException(status_code=404, detail="画像条目不存在")

    ok = store.forget(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="画像条目不存在")
    return {"forgotten": entry_id}


@app.delete("/api/memory/profile")
async def forget_all_profile(request: Request):
    """「忘掉关于我的一切」:清空当前用户的全部画像。"""
    from core.semantic_memory import get_semantic_memory

    store = get_semantic_memory()

    entries = store.get_profile(limit=1000)
    forgotten, failed = [], []
    for e in entries:
        if store.forget(e.id):
            forgotten.append(e.id)
        else:
            failed.append(e.id)

    return {
        "forgotten": len(forgotten),
        "failed": len(failed),
    }


@app.get("/api/reflections")
async def list_reflections():
    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    base = Path(__file__).parent.parent / "memory" / "reflections"
    if not base.exists():
        return {"reflections": []}

    reports = []
    for month_dir in sorted(base.iterdir(), reverse=True):
        if not month_dir.is_dir():
            continue
        for path in sorted(month_dir.glob("*.json"), reverse=True):
            try:
                reports.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if len(reports) >= 20:
                break
        if len(reports) >= 20:
            break
    return {"reflections": reports}


@app.post("/api/reflections/request")
@require_auth
async def request_reflection():
    from core.reflect import SelfReflectLoop

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    # 复用 app.state 上的实例,保留每日合并计数等状态
    reflect_loop = getattr(app.state, "reflect_loop", None)
    if reflect_loop is None:
        reflect_loop = SelfReflectLoop()
        app.state.reflect_loop = reflect_loop

    report = await reflect_loop.request_reflection()
    if report:
        return {"success": True, "report_id": report.id}
    return {"success": False, "message": "no reflection generated"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
