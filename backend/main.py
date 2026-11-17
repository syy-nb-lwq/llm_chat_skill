"""FastAPI backend service."""
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
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


# ===== 用户(假用户服务) =====


@app.get("/api/users")
async def list_users():
    """列出用户(运维/隔离测试用)。"""
    from core.user_service import get_user_service

    users = get_user_service().list_users()
    return {
        "users": [
            {
                "id": u.id,
                "tenant_id": u.tenant_id,
                "display_name": u.display_name,
                "created_at": u.created_at,
                "last_seen_at": u.last_seen_at,
            }
            for u in users
        ]
    }


@app.post("/api/users")
async def create_user(payload: Optional[dict] = None):
    """新建用户并签发用户令牌。

    没有登录体系时,这个接口就是"分发 userid"的入口:
    拿到的 user_token 交给前端存在 localStorage,之后所有连接都认这个用户。
    """
    from core.user_service import get_user_service

    payload = payload or {}
    service = get_user_service()
    user = service.create(
        display_name=payload.get("display_name", ""),
        tenant_id=payload.get("tenant_id", "default"),
    )
    return {
        "user_id": user.id,
        "tenant_id": user.tenant_id,
        "display_name": user.display_name,
        "user_token": service.issue_token(user.id, user.tenant_id),
    }


@app.delete("/api/users/{user_id}")
@require_auth
async def delete_user(user_id: str):
    """删除用户档案(管理操作)。"""
    from core.user_service import get_user_service

    ok = get_user_service().delete(user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"user not found: {user_id}")
    return {"deleted": user_id}


# ===== 会话内容(短期记忆) =====


@app.get("/api/sessions")
@require_auth
async def list_sessions(user_id: str = ""):
    """列出会话(可选按 user_id 过滤)。"""
    from core.session_memory import get_session_memory_store

    rows = get_session_memory_store().list_sessions(user_id or None)
    return {
        "sessions": [
            {
                "session_id": r["session_id"],
                "user_id": r["user_id"],
                "message_count": r["message_count"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    }


@app.get("/api/sessions/{session_id}/messages")
@require_auth
async def get_session_messages(session_id: str, limit: int = 100, user_id: str = ""):
    """取某个会话的消息(短期记忆内容,全量保留)。

    ``user_id`` 可选:传了就只返回该用户的,用于隔离校验。
    """
    from core.session_memory import get_session_memory_store

    store = get_session_memory_store()
    if not store.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    msgs = store.get_messages(session_id, limit=limit or 0, user_id=user_id or None)
    return {
        "session_id": session_id,
        "messages": [m.to_dict() for m in msgs],
    }


@app.delete("/api/sessions/{session_id}")
@require_auth
async def delete_session(session_id: str):
    """删除会话及其全部内容(用户主动要求清除时使用)。"""
    from core.session_memory import get_session_memory_store

    ok = get_session_memory_store().delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return {"deleted": session_id}


# ===== 长期记忆(用户画像) =====


@app.get("/api/memories")
@require_auth
async def list_memories(user_id: str = "", limit: int = 50):
    """「你记住了我什么」:列出某用户的画像条目。

    `user_id` 必填 —— 长期记忆是用户数据,不给默认值就查全部
    等于把别人的画像摊开(docs/13 §2.9)。
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    from core.memory_db import PROFILE_MEMORY_TYPES
    from core.semantic_memory import get_semantic_memory

    entries = get_semantic_memory().get_recent(
        user_id=user_id,
        types=list(PROFILE_MEMORY_TYPES),
        limit=limit,
    )
    return {
        "user_id": user_id,
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
    from core.semantic_memory import get_semantic_memory

    store = get_semantic_memory()
    entry = store.memory_db.get(memory_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"memory not found: {memory_id}")
    ok = store.forget(memory_id)
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
async def list_teachings(user_id: str = "default", session_id: str = "default"):
    """列出当前 session 上的活跃教学会话(用于重复技能决策 UI)。"""
    from agents.teaching_session import get_teaching_store
    store = get_teaching_store()
    ts = store.find_active_for(user_id, session_id)
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
async def cancel_teaching(user_id: str = "default", session_id: str = "default"):
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok = t.cancel(user_id, session_id)
    return {"cancelled": ok}


@app.post("/api/teachings/choose")
@require_auth
async def choose_teaching_decision(
    choice: str,
    user_id: str = "default",
    session_id: str = "default",
):
    """处理用户在重复技能上的决策:reuse / update_new / cancel。"""
    from agents.teaching_session import get_teaching_store, TeachingStatus
    store = get_teaching_store()
    ts = store.find_active_for(user_id, session_id)
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
async def confirm_teaching(user_id: str = "default", session_id: str = "default"):
    """用户在草稿 UI 上点确认发布时调用。"""
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok, msg, skill = t.confirm_and_publish(user_id, session_id)
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


# ===== 用户画像面板(A-09 / U-04)=====
#
# 「你记住了我什么 / 忘掉这件事」
#
# **用户级接口,不是管理员接口。** 所以不能用 `require_auth`
# (那是 owner token,管的是删技能/审批补丁这类管理操作)。
# 这里校验的是**会话令牌 / 用户令牌** —— 两者都是 HMAC 签名的,
# payload 里带 user_id,服务端据此认定"你只能看自己的画像"。
#
# **最重要的一条**:user_id **只能从令牌里取**,绝不能从 query 参数取。
# 否则用户 A 传个 user_id=B 就能看/删 B 的画像 —— 那是越权。

#: 画像类型的展示标签(与 build_profile_block 保持一致)
_PROFILE_LABELS = {
    "preference": "偏好",
    "habit": "习惯",
    "method": "方法",
    "constraint": "雷区",
    "fact": "事实",
}


def _resolve_user_id(request: Request) -> str:
    """从请求头里的令牌解析出 user_id;无效则 401。

    接受两种令牌:
    - **会话令牌**(WS 连接期间签发):payload 带 user_id/session_id/client_id
    - **用户令牌**(长期凭证):payload 带 user_id/tenant_id
    """
    from infra.auth import verify_session_token, verify_user_token

    auth = ""
    if hasattr(request, "headers"):
        auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""

    payload = verify_session_token(token) or verify_user_token(token)
    if not payload or not payload.get("user_id"):
        raise HTTPException(
            status_code=401,
            detail="需要 Authorization: Bearer <会话令牌>",
        )
    return str(payload["user_id"])


@app.get("/api/memory/profile")
async def get_profile(request: Request):
    """列出当前用户的画像(长期记忆)条目。

    同时返回**此刻正在注入**的那段文本 —— 让用户看清楚
    "现在到底哪些东西在影响我的回答"。
    """
    from core.semantic_memory import get_semantic_memory

    user_id = _resolve_user_id(request)
    store = get_semantic_memory()

    entries = store.get_profile(user_id, limit=100)
    block = store.build_profile_block(user_id)

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
        "user_id": user_id,
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

    user_id = _resolve_user_id(request)
    store = get_semantic_memory()

    # **先确认这条属于该用户** —— 不然可以拿别的用户的 entry_id 来删
    owned = [
        e for e in store.get_profile(user_id, limit=1000)
        if e.id == entry_id
    ]
    if not owned:
        # 也可能是已被删过/不在画像类型里 —— 一律 404,不泄露存在性
        raise HTTPException(status_code=404, detail="画像条目不存在")

    ok = store.forget(entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="画像条目不存在")
    return {"forgotten": entry_id, "user_id": user_id}


@app.delete("/api/memory/profile")
async def forget_all_profile(request: Request):
    """「忘掉关于我的一切」:清空当前用户的全部画像。"""
    from core.semantic_memory import get_semantic_memory

    user_id = _resolve_user_id(request)
    store = get_semantic_memory()

    entries = store.get_profile(user_id, limit=1000)
    forgotten, failed = [], []
    for e in entries:
        if store.forget(e.id):
            forgotten.append(e.id)
        else:
            failed.append(e.id)

    return {
        "user_id": user_id,
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
