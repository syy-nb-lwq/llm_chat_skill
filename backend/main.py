"""FastAPI backend service."""
import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.session import sessions
from backend.websocket_handler import endpoint, register_handlers
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

    from infra.providers.registry import init_providers

    init_providers(
        openai_api_key=config.openai_api_key,
        openai_base_url=config.openai_base_url,
        openai_model=config.openai_model,
        anthropic_api_key=config.anthropic_api_key,
        anthropic_model=config.anthropic_model,
        local_base_url=config.local_base_url,
        local_model=config.local_model,
        default_provider=config.default_provider,
    )

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
    return {
        "status": "ok",
        "self_evolution_enabled": bool(config.self_evolution_enabled),
    }


@app.get("/api/features")
async def get_features():
    return {
        "self_evolution_enabled": bool(config.self_evolution_enabled),
        "skill_dag_enabled": bool(config.skill_dag_enabled),
        "semantic_memory_enabled": bool(config.semantic_memory_enabled),
    }


@app.post("/api/features/self-evolution")
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
    skills = store.list_all()
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
            }
            for skill in skills
        ]
    }


@app.delete("/api/skills/{name}")
async def delete_skill(name: str):
    from skills.manager import get_skill_store

    store = get_skill_store()
    removed = store.delete_by_name(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return {"deleted": name, "files": removed}


@app.delete("/api/skills/{name}/{version}")
async def delete_skill_version(name: str, version: str):
    from skills.manager import get_skill_store

    store = get_skill_store()
    removed = store.delete_version(name, version)
    if not removed:
        raise HTTPException(status_code=404, detail=f"skill version not found: {name}@{version}")
    return {"deleted": name, "version": version, "files": removed}


@app.post("/api/skills/reload")
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
async def cancel_teaching(user_id: str = "default", session_id: str = "default"):
    from agents.skill_trainer import SkillTrainer
    t = SkillTrainer()
    ok = t.cancel(user_id, session_id)
    return {"cancelled": ok}


@app.post("/api/teachings/choose")
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
            }
            for patch in patches
        ]
    }


@app.post("/api/patches/{patch_id}/approve")
async def approve_patch(patch_id: str):
    from core.memory import get_memory_store
    from skills.manager import get_skill_store

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    store = get_memory_store()
    if not store.approve_patch(patch_id, "human"):
        raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")

    patch_file = store.patches_dir / f"{patch_id}.json"
    if not patch_file.exists():
        return {"approved": patch_id, "applied": False, "files": []}

    data = json.loads(patch_file.read_text(encoding="utf-8"))
    suggestion = data.get("suggestion", {}) or {}
    target_skill = suggestion.get("target_skill") or data.get("target_skill")
    updates = {}
    if "method" in suggestion:
        updates["method"] = suggestion["method"]

    applied_files = []
    if target_skill and updates:
        applied_files = get_skill_store().update_skill(target_skill, updates)

    return {
        "approved": patch_id,
        "applied": bool(applied_files),
        "files": applied_files,
    }


@app.post("/api/patches/{patch_id}/reject")
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
async def request_reflection():
    from core.reflect import SelfReflectLoop

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    report = await SelfReflectLoop().request_reflection()
    if report:
        return {"success": True, "report_id": report.id}
    return {"success": False, "message": "no reflection generated"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
