"""FastAPI 后端服务"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.session import sessions
from backend.websocket_handler import endpoint, register_handlers
from infra.config import config, ConfigError
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

# 注册 WebSocket 路由（模块级，注册必须在 app 启动前）
endpoint.register_route(app, "/pubsub")


@app.on_event("startup")
async def _startup():
    try:
        config.validate()
    except ConfigError as e:
        logger.error("startup", f"配置错误: {e}")
    # 在事件循环中注册 PubSub 订阅处理器
    await register_handlers()
    # 启动 Session GC 后台任务
    async def gc_loop():
        while True:
            await asyncio.sleep(60)
            sessions.gc()
    asyncio.create_task(gc_loop())
    
    # 初始化工具中枢 Hub
    from tools.hub import get_tool_hub
    from tools.sources.python_source import create_python_source
    from tools.sources.mcp_source import create_mcp_source
    hub = get_tool_hub()
    
    # 注册 Python 内置工具源
    python_source = create_python_source(
        name="builtin",
        directories=[str(Path(__file__).parent.parent / "tools")],
    )
    hub.register_source(python_source)
    
    # 连接所有工具源
    try:
        await hub.connect_all()
        logger.info("startup", f"工具中枢已初始化,工具数: {len(hub.names())}")
    except Exception as e:
        logger.warning("startup", f"工具源连接失败: {e}")
    
    # 启动自我反思循环(如果启用)
    try:
        from core.reflect import SelfReflectLoop, get_self_evolution_enabled
        if get_self_evolution_enabled():
            reflect_loop = SelfReflectLoop()
            asyncio.create_task(reflect_loop.start())
            logger.info("startup", "自我反思循环已启动")
    except Exception as e:
        logger.warning("startup", f"启动自我反思循环失败: {e}")


# ===== REST API =====
@app.get("/")
async def root():
    return {"status": "ok", "service": "Skill Agent", "version": "1.0"}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/skills")
async def list_skills():
    from skills.manager import get_skill_store
    store = get_skill_store()
    skills = store.list_all()
    return {
        "skills": [{
            "id": getattr(s, "id", None),
            "name": s.name,
            "version": getattr(s, "version", "1.0.0"),
            "capability": s.capability,
            "patterns": s.patterns,
            "tags": s.tags,
            "method": s.method,
            "source": getattr(s, "source", "builtin"),
        } for s in skills]
    }


@app.delete("/api/skills/{name}")
async def delete_skill(name: str):
    from skills.manager import get_skill_store
    store = get_skill_store()
    removed = []
    # 从内存移除
    skill = store._registry._by_name.pop(name, None)
    if skill:
        # 同步移除 by_id
        store._registry._by_id.pop(skill.id, None)
        store._skills.pop(name, None)
    # 从文件删除(扫描所有可能目录)
    for d in [store.base_path, store.base_path / "builtin", store.base_path / "user"]:
        if d.exists():
            for f in d.rglob(f"{name}*.yaml"):
                try:
                    f.unlink()
                    removed.append(str(f))
                except Exception as e:
                    logger.error("skills", f"删除失败: {e}")
            for f in d.rglob(f"{name}*.md"):
                try:
                    f.unlink()
                    removed.append(str(f))
                except Exception as e:
                    logger.error("skills", f"删除失败: {e}")
    # 额外:backend/skills/ 也可能存了 MD
    root = store.base_path.parent
    backend_skills = root / "backend" / "skills"
    if backend_skills.exists():
        for f in backend_skills.rglob(f"{name}*.md"):
            try:
                f.unlink()
                removed.append(str(f))
            except Exception as e:
                logger.error("skills", f"删除失败: {e}")
    if not removed and skill is None:
        raise HTTPException(status_code=404, detail=f"技能 {name} 不存在")
    return {"deleted": name, "files": removed}


@app.post("/api/skills/reload")
async def reload_skills():
    from skills.manager import get_skill_store
    store = get_skill_store()
    store.reload()
    return {"reloaded": True, "count": len(store.list_all())}


@app.get("/api/tools")
async def list_tools():
    from agents.learning import LearningAgent
    learning = LearningAgent()
    # 尝试从 Hub 获取工具
    tools = learning.tools.all()
    if not tools:
        # 兼容:如果 Hub 没有工具,尝试从旧注册表获取
        from tools.base import get_tool_registry
        tools = get_tool_registry().all()
    return {
        "tools": [
            {"name": tool.name, "description": tool.description}
            for tool in tools
        ]
    }


# ===== Patch 管理 API =====
@app.get("/api/patches")
async def list_patches():
    """获取所有待审阅的改进建议"""
    from core.memory import get_memory_store
    store = get_memory_store()
    patches = store.get_pending_patches()
    return {
        "patches": [
            {
                "id": p.id,
                "trace_id": p.trace_id,
                "timestamp": p.timestamp,
                "target_skill": p.target_skill,
                "patch_type": p.patch_type,
                "diagnosis": p.diagnosis,
                "suggestion": p.suggestion,
                "confidence": p.confidence,
                "status": p.status,
            }
            for p in patches
        ]
    }


@app.post("/api/patches/{patch_id}/approve")
async def approve_patch(patch_id: str):
    """批准一个 SkillPatch,使其生效"""
    from core.memory import get_memory_store
    from core.merger import get_self_evolution_enabled
    from skills.manager import get_skill_store
    from pathlib import Path
    import json

    if not get_self_evolution_enabled():
        raise HTTPException(status_code=403, detail="自我进化功能未启用")

    store = get_memory_store()
    store.approve_patch(patch_id, "human")

    applied = False
    # 如果是 improve_skill 类型,尝试应用修改
    patch_file = Path(__file__).parent.parent / "memory" / "skill_patches" / "pending" / f"{patch_id}.json"
    if patch_file.exists():
        data = json.loads(patch_file.read_text(encoding="utf-8"))
        if data.get("status") == "approved":
            suggestion = data.get("suggestion", {})
            if suggestion.get("type") == "improve_skill" and suggestion.get("target_skill"):
                # 尝试应用修改到目标技能
                skill_store = get_skill_store()
                skill = skill_store.get_by_name(suggestion["target_skill"])
                if skill:
                    # 更新技能的 method
                    if "method" in suggestion:
                        skill.method = suggestion["method"]
                    # 重新加载
                    skill_store.reload()
                    applied = True

    return {"approved": patch_id, "applied": applied}


@app.post("/api/patches/{patch_id}/reject")
async def reject_patch(patch_id: str):
    """拒绝一个 SkillPatch"""
    from core.memory import get_memory_store
    from core.merger import get_self_evolution_enabled

    if not get_self_evolution_enabled():
        raise HTTPException(status_code=403, detail="自我进化功能未启用")

    store = get_memory_store()
    store.reject_patch(patch_id, "human")
    return {"rejected": patch_id}


@app.get("/api/memory/stats")
async def memory_stats():
    """获取记忆统计"""
    from core.memory import get_memory_store
    store = get_memory_store()
    stats = store.get_stats()
    return stats


@app.get("/api/reflections")
async def list_reflections():
    """获取反思报告列表"""
    from core.merger import get_self_evolution_enabled
    from pathlib import Path
    import json

    if not get_self_evolution_enabled():
        raise HTTPException(status_code=403, detail="自我进化功能未启用")

    base = Path(__file__).parent.parent / "memory" / "reflections"
    reports = []

    for month_dir in sorted(base.iterdir(), reverse=True):
        if not month_dir.is_dir():
            continue
        for path in sorted(month_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                reports.append(data)
            except Exception:
                pass
            if len(reports) >= 20:  # 最多 20 条
                break
        if len(reports) >= 20:
            break

    return {"reflections": reports}


@app.post("/api/reflections/request")
async def request_reflection():
    """请求立即生成反思报告"""
    from core.reflect import SelfReflectLoop, get_self_evolution_enabled

    if not get_self_evolution_enabled():
        raise HTTPException(status_code=403, detail="自我进化功能未启用")

    loop = SelfReflectLoop()
    report = await loop.request_reflection()

    if report:
        return {"success": True, "report_id": report.id}
    return {"success": False, "message": "无需反思"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)