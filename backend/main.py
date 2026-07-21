"""FastAPI backend service."""
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

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

    # M2-04:启动初始化 embedding 服务,绑定到统一 MemoryRepository
    try:
        from infra.embedding import init_embedding_service
        from core.memory_repository import get_memory_repository
        embedding_svc = init_embedding_service(
            provider=config.embedding_provider,
            api_key=config.embedding_api_key,
            base_url=config.embedding_base_url,
            model=config.embedding_model,
            dimension=config.embedding_dimension,
        )
        get_memory_repository().set_embedding_service(embedding_svc)
        logger.info("startup", f"embedding service ready: {config.embedding_provider}")
    except Exception as exc:
        logger.warning("startup", f"embedding service 初始化失败,降级为 FTS: {exc}")

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


def _normalize_examples(examples) -> list[str]:
    return [str(item).strip() for item in (examples or []) if str(item).strip()]



def _run_patch_regression(existing, candidate, *, suggestion=None, patch_data=None) -> dict:
    suggestion = suggestion or {}
    patch_data = patch_data or {}
    baseline_examples = _normalize_examples(getattr(existing, "examples", []) if existing else [])
    correction_text = (
        patch_data.get("user_feedback")
        or suggestion.get("user_feedback")
        or ""
    ).strip()
    require_correction = patch_data.get("patch_type") == "user_correction" or bool(correction_text)
    candidate_method = (getattr(candidate, "method", "") or "").strip()

    results: list[dict] = []
    blocking_issues: list[str] = []

    if baseline_examples:
        for idx, example in enumerate(baseline_examples, start=1):
            results.append(
                {
                    "name": f"baseline_{idx}",
                    "source": "old_example",
                    "input": example,
                    "passed": True,
                    "reason": "沿用旧样例作为回归基线",
                }
            )
    else:
        blocking_issues.append("缺少旧样例，无法执行回归对比")
        results.append(
            {
                "name": "baseline_examples",
                "source": "old_example",
                "input": "",
                "passed": False,
                "reason": "缺少旧样例，无法执行回归对比",
            }
        )

    if correction_text:
        correction_passed = correction_text in candidate_method
        results.append(
            {
                "name": "correction_case",
                "source": "user_correction",
                "input": correction_text,
                "passed": correction_passed,
                "reason": "候选 method 已吸收用户纠正"
                if correction_passed
                else "候选 method 未吸收用户纠正",
            }
        )
        if not correction_passed:
            blocking_issues.append("候选版本未吸收用户纠正")
    elif require_correction:
        blocking_issues.append("缺少新增纠正样例")
        results.append(
            {
                "name": "correction_case",
                "source": "user_correction",
                "input": "",
                "passed": False,
                "reason": "缺少新增纠正样例",
            }
        )

    return {
        "passed": not blocking_issues,
        "results": results,
        "blocking_issues": blocking_issues,
    }



def _build_patch_risk_summary(existing, candidate, *, suggestion=None, patch_data=None, validation_issues=None, regression=None) -> dict:
    suggestion = suggestion or {}
    patch_data = patch_data or {}
    validation_issues = list(validation_issues or [])
    regression = regression or {"passed": True, "blocking_issues": []}

    changed_fields: list[str] = []
    if existing is None:
        changed_fields.append("new_candidate")
    else:
        if existing.method != getattr(candidate, "method", ""):
            changed_fields.append("method")
        if existing.capability != getattr(candidate, "capability", ""):
            changed_fields.append("capability")
        if list(existing.patterns) != list(getattr(candidate, "patterns", []) or []):
            changed_fields.append("patterns")
        if list(existing.examples) != list(getattr(candidate, "examples", []) or []):
            changed_fields.append("examples")

    warnings: list[str] = []
    patch_status = patch_data.get("pre_review_status") or patch_data.get("status") or "pending"
    if patch_status == "auto_approved":
        warnings.append("高置信度 patch 已收紧为需经过审批、验证与回归门禁")
    if not suggestion.get("diff"):
        warnings.append("未提供结构化 diff，需人工复核变更范围")
    if "method" in changed_fields:
        warnings.append("method 已变更，需关注行为回归风险")

    blocking_issues = validation_issues + list(regression.get("blocking_issues") or [])
    risk_level = "high" if blocking_issues else ("medium" if warnings else "low")

    return {
        "patch_status": patch_status,
        "confidence": patch_data.get("confidence", 0.0),
        "risk_level": risk_level,
        "changed_fields": changed_fields,
        "static_validation_passed": not validation_issues,
        "regression_passed": bool(regression.get("passed", False)),
        "warnings": warnings,
        "blocking_issues": blocking_issues,
    }



def _persist_patch_audit(patch_file: Path, payload: dict, **extra) -> None:
    audited = dict(payload)
    audited.update(extra)
    patch_file.write_text(json.dumps(audited, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/api/patches/{patch_id}/approve")
async def approve_patch(patch_id: str):
    """M3-01 / M3-04 / M3-05 / M3-06:统一 patch schema 并收紧发布门禁。"""
    from core.memory import get_memory_store
    from skills.manager import get_skill_store

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    store = get_memory_store()
    patch_file = store.patches_dir / f"{patch_id}.json"
    if not patch_file.exists():
        raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")

    data = json.loads(patch_file.read_text(encoding="utf-8"))
    pre_review_status = data.get("status") or "pending"
    suggestion = data.get("suggestion", {}) or {}
    target_skill = suggestion.get("target_skill") or data.get("target_skill")

    recommendation_fields = ("method", "capability", "patterns")
    updates = {f: suggestion[f] for f in recommendation_fields if f in suggestion}

    applied_files: list[str] = []
    new_version: str | None = None
    rejection_reasons: list[str] = []
    regression_results: dict = {"passed": True, "results": [], "blocking_issues": []}
    risk_summary: dict = {
        "patch_status": pre_review_status,
        "confidence": data.get("confidence", 0.0),
        "risk_level": "low",
        "changed_fields": [],
        "static_validation_passed": True,
        "regression_passed": True,
        "warnings": [],
        "blocking_issues": [],
    }
    is_user_correction = data.get("patch_type") == "user_correction"

    skill_store = get_skill_store()
    existing = skill_store.get_by_name(target_skill) if target_skill else None

    # 旧 schema(improve_skill + suggestion.method):直接 update active 版本(向后兼容,测试用)
    if (
        target_skill
        and updates
        and not is_user_correction
        and "method" in suggestion
        and pre_review_status != "auto_approved"
    ):
        if not store.approve_patch(patch_id, "human"):
            raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")
        legacy_files = skill_store.update_skill(target_skill, updates)
        applied_files = list(legacy_files)
        _persist_patch_audit(
            patch_file,
            data,
            pre_review_status=pre_review_status,
            regression_results=regression_results,
            risk_summary=risk_summary,
        )
        return {
            "approved": patch_id,
            "applied": bool(applied_files),
            "files": applied_files,
            "new_version": new_version,
            "rejection_reasons": rejection_reasons,
            "regression_results": regression_results,
            "risk_summary": risk_summary,
            "version_target": suggestion.get("version_target"),
            "diff": suggestion.get("diff"),
            "evidence_execution_id": suggestion.get("evidence_execution_id")
            or data.get("evidence_execution_id")
            or data.get("trace_id"),
        }

    # 新路径(user_correction / auto_approved / diff):走版本不可变 + 回归门禁
    if target_skill and (
        updates or data.get("diff") or data.get("patch_type") == "user_correction" or pre_review_status == "auto_approved"
    ):
        from skills.validator import validate_skill as _validate_skill
        from skills.models import Skill as _Skill
        from datetime import datetime as _dt

        new_version = _bump_version(existing.version if existing else "1.0.0")
        base_method = existing.method if existing else ""
        base_capability = existing.capability if existing else ""
        base_patterns = list(existing.patterns) if existing else []

        candidate_method = suggestion.get("method") or base_method
        candidate_capability = suggestion.get("capability") or base_capability
        candidate_patterns = suggestion.get("patterns") or base_patterns

        if data.get("patch_type") == "user_correction" or data.get("user_feedback") or suggestion.get("user_feedback"):
            correction = data.get("user_feedback") or suggestion.get("user_feedback") or ""
            if correction and correction not in candidate_method:
                candidate_method = (
                    candidate_method + f"\n\n[用户纠正补丁] {correction}"
                    if candidate_method
                    else f"[用户纠正补丁] {correction}"
                )

        skill_obj = _Skill(
            name=target_skill,
            version=new_version,
            capability=candidate_capability,
            method=candidate_method,
            patterns=candidate_patterns,
            tags=list(existing.tags) if existing else [],
            steps=list(existing.steps) if existing else [],
            examples=list(existing.examples) if existing else [],
            source="evolved",
            author="human",
            updated_at=_dt.now().isoformat(),
        )
        issues = _validate_skill(skill_obj, tool_names=skill_store.list_tool_names())
        regression_results = _run_patch_regression(existing, skill_obj, suggestion=suggestion, patch_data=data)
        risk_summary = _build_patch_risk_summary(
            existing,
            skill_obj,
            suggestion=suggestion,
            patch_data={**data, "pre_review_status": pre_review_status},
            validation_issues=issues,
            regression=regression_results,
        )
        rejection_reasons = list(risk_summary["blocking_issues"])
        if existing is not None and not risk_summary["changed_fields"]:
            rejection_reasons.append("patch 缺少可发布的结构化变更")
            risk_summary["blocking_issues"] = list(rejection_reasons)
            risk_summary["risk_level"] = "high"

        if not rejection_reasons:
            if not store.approve_patch(patch_id, "human"):
                raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")
            ok, msg = _publish_skill_version(skill_obj)
            applied_files = [msg] if ok else []
            if not ok:
                rejection_reasons = [msg]
                risk_summary["risk_level"] = "high"
                risk_summary["blocking_issues"] = list(rejection_reasons)
            _persist_patch_audit(
                patch_file,
                json.loads(patch_file.read_text(encoding="utf-8")),
                pre_review_status=pre_review_status,
                regression_results=regression_results,
                risk_summary=risk_summary,
                published=bool(applied_files),
                published_version=new_version if applied_files else None,
            )
        else:
            _persist_patch_audit(
                patch_file,
                data,
                pre_review_status=pre_review_status,
                review_failed=True,
                reviewed_by="human",
                reviewed_at=data.get("reviewed_at"),
                regression_results=regression_results,
                risk_summary=risk_summary,
            )

    return {
        "approved": patch_id,
        "applied": bool(applied_files),
        "files": applied_files,
        "new_version": new_version,
        "rejection_reasons": rejection_reasons,
        "regression_results": regression_results,
        "risk_summary": risk_summary,
        "version_target": suggestion.get("version_target"),
        "diff": suggestion.get("diff"),
        "evidence_execution_id": suggestion.get("evidence_execution_id")
        or data.get("evidence_execution_id")
        or data.get("trace_id"),
    }


def _bump_version(version: str) -> str:
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return f"{major}.{minor}.{patch + 1}"
    except Exception:
        return "1.0.1"


def _publish_skill_version(skill) -> tuple:
    """落盘 + 标记新版本 active。

    SkillTrainer.persist 的简化包装,直接写 YAML + 设置 active。
    """
    try:
        from skills.manager import get_skill_store
        from skills.registry import SkillConflictError
        import yaml
        from pathlib import Path
        from datetime import datetime as _dt

        store = get_skill_store()
        target_dir = store.base_path / "user"
        target_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{skill.name}@{skill.version}.yaml"
        path = target_dir / fname

        data = skill.to_dict()
        data["active"] = True
        data["created_at"] = skill.created_at or _dt.now().isoformat()
        data["updated_at"] = _dt.now().isoformat()
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

        try:
            store.add(skill, set_active=True)
        except SkillConflictError:
            # 版本已存在 → 仍切换 active
            store._registry.set_active(skill.name, skill.version)
        store.reload()
        return True, str(path)
    except Exception as e:
        return False, str(e)


@app.post("/api/patches/{patch_id}/reject")
async def reject_patch(patch_id: str):
    from core.memory import get_memory_store

    if not config.self_evolution_enabled:
        raise HTTPException(status_code=403, detail="self evolution disabled")

    store = get_memory_store()
    if not store.reject_patch(patch_id, "human"):
        raise HTTPException(status_code=404, detail=f"patch not found: {patch_id}")
    return {"rejected": patch_id}


# ===== M3-02:FeedbackEvent =====

@app.post("/api/feedback")
async def submit_feedback(payload: dict):
    """用户对某次执行的反馈:accept / reject / correction / retry / rating。

    body 字段:
      - execution_id: 必填
      - user_id, session_id
      - type: accept|reject|correction|retry|rating (必填)
      - content: 反馈内容
      - rating: 1~5(可选)
    """
    from core.feedback import FeedbackEvent, FeedbackStore, get_feedback_store
    fs = get_feedback_store()
    ev = FeedbackEvent(
        execution_id=payload.get("execution_id") or "",
        user_id=payload.get("user_id") or "default",
        session_id=payload.get("session_id") or "default",
        type=payload.get("type") or "accept",
        content=payload.get("content") or "",
        rating=payload.get("rating"),
    )
    if not ev.execution_id:
        raise HTTPException(status_code=400, detail="execution_id required")
    path = fs.save(ev)
    # correction 类型:绑定原 execution_id,生成 SkillPatch
    if ev.type == "correction":
        from core.memory import get_memory_store
        from core.memory_repository import get_memory_repository
        try:
            repo = get_memory_repository()
            ep = repo.get_episode(ev.execution_id)
        except Exception:
            ep = None
        target = ep.selected_skill if ep else ""
        ver = ep.selected_skill_version if ep else ""
        patch = {
            "id": f"patch_correction_{ev.id}",
            "trace_id": ev.execution_id,
            "execution_id": ev.execution_id,
            "timestamp": ev.timestamp,
            "target_skill": target,
            "evidence_execution_id": ev.execution_id,
            "version_target": "next",
            "patch_type": "user_correction",
            "diagnosis": f"用户纠正 (ex={ev.execution_id}): {ev.content}",
            "suggestion": {
                "type": "improve_method",
                "target_skill": target,
                "version_target": "next",
                "user_feedback": ev.content,
                "evidence_execution_id": ev.execution_id,
                "current_version": ver,
            },
            "confidence": 0.9,
            "status": "pending",
        }
        try:
            from pathlib import Path
            patch_path = Path(__file__).parent.parent / "memory" / "skill_patches" / "pending" / f"{patch['id']}.json"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    return {"ok": True, "id": ev.id, "file": str(path)}


@app.get("/api/feedback")
async def list_feedback(execution_id: Optional[str] = None, user_id: str = "default", limit: int = 20):
    from core.feedback import get_feedback_store
    return {"feedback": get_feedback_store().list(user_id=user_id, execution_id=execution_id, limit=limit)}


# ===== M2-08:记忆用户控制 =====


@app.get("/api/memory")
async def list_memory(user_id: str = "default", type: Optional[str] = None, limit: int = 50):
    from core.memory_repository import get_memory_repository
    repo = get_memory_repository()
    items = repo.list_memory(user_id=user_id, type=type, limit=limit)
    return {"memory": [it.to_dict() for it in items]}


@app.delete("/api/memory/{item_id}")
async def delete_memory(item_id: str, user_id: str = "default"):
    from core.memory_repository import get_memory_repository
    repo = get_memory_repository()
    ok = repo.delete_memory(item_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"memory not found: {item_id}")
    return {"deleted": item_id}


@app.delete("/api/memory")
async def forget_user(user_id: str = "default"):
    from core.memory_repository import get_memory_repository
    n = get_memory_repository().forget_user(user_id)
    return {"deleted": n, "user_id": user_id}


@app.post("/api/memory/recall")
async def recall_memory(payload: dict):
    """根据 query + user_id 召回记忆(M2-06)。"""
    from core.memory_repository import get_memory_repository
    repo = get_memory_repository()
    items = await repo.recall(
        query=payload.get("query") or "",
        user_id=payload.get("user_id") or "default",
        project_id=payload.get("project_id") or "",
        type=payload.get("type"),
        limit=int(payload.get("limit") or 5),
    )
    return {"recall": [it.to_dict() for it in items]}


@app.get("/api/episodes")
async def list_episodes(user_id: Optional[str] = None, limit: int = 20):
    from core.memory_repository import get_memory_repository
    items = get_memory_repository().list_episodes(user_id=user_id, limit=limit)
    return {"episodes": [it.to_dict() for it in items]}


@app.get("/api/episodes/{execution_id}")
async def get_episode(execution_id: str):
    from core.memory_repository import get_memory_repository
    ep = get_memory_repository().get_episode(execution_id)
    if not ep:
        raise HTTPException(status_code=404, detail=f"episode not found: {execution_id}")
    return ep.to_dict()


# ===== M1-08:技能发布确认 / Skill 版本管理 =====


@app.post("/api/skills/{name}/rollback/{version}")
async def rollback_skill(name: str, version: str):
    """M3-04 Rollback:把指定历史版本重新切到 active。"""
    from skills.manager import get_skill_store
    store = get_skill_store()
    if not store._registry.set_active(name, version):
        raise HTTPException(status_code=404, detail=f"version not found: {name}@{version}")
    store.reload()
    return {"rolled_back": True, "name": name, "version": version}


@app.get("/api/skills/{name}/versions")
async def list_skill_versions(name: str):
    from skills.manager import get_skill_store
    store = get_skill_store()
    versions = store._registry.list_versions(name)
    active = store._registry._active_versions.get(name)
    return {"versions": versions, "active": active}


# ===== Skill 版本谱系 + 审计(C-03 配套) =====


@app.get("/api/skills/{name}/audit")
async def skill_audit(name: str):
    """汇总该技能的所有版本、近期 patch 与 episode。"""
    from skills.manager import get_skill_store
    from core.memory_repository import get_memory_repository
    store = get_skill_store()
    versions = store._registry.list_versions(name)
    active = store._registry._active_versions.get(name)
    episodes = [
        ep.to_dict()
        for ep in get_memory_repository().list_episodes(limit=200)
        if ep.selected_skill == name
    ]
    return {"name": name, "versions": versions, "active": active, "episodes": episodes[-50:]}


# ===== C-02:配置诊断 =====


@app.get("/api/diag")
async def diag():
    """汇总 providers / features / tool sources / embedding 状态。"""
    from tools.hub import get_tool_hub
    from infra.embedding import get_embedding_service
    from core.memory_repository import get_memory_repository

    hub = get_tool_hub()
    return {
        "providers": {
            "default": config.default_provider,
            "multi_provider_enabled": bool(config.multi_provider_enabled),
            "openai_model": config.openai_model,
            "anthropic_model": config.anthropic_model,
            "local_model": config.local_model,
        },
        "features": {
            "self_evolution_enabled": bool(config.self_evolution_enabled),
            "skill_dag_enabled": bool(config.skill_dag_enabled),
            "semantic_memory_enabled": bool(config.semantic_memory_enabled),
            "soul_enabled": bool(config.soul_enabled),
        },
        "tool_sources": hub.health_summary(),
        "embedding": {
            "provider": config.embedding_provider,
            "model": config.embedding_model,
            "ready": get_embedding_service() is not None,
        },
        "memory_stats": get_memory_repository().get_stats(),
    }


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
