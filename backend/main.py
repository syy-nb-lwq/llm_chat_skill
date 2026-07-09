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
    return {
        "tools": [
            {"name": tool.name, "description": tool.description}
            for tool in learning.tools.all()
        ]
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)