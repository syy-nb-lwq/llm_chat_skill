"""FastAPI 后端服务 - WebSocket 通信(新版统一 event 协议)"""
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.session import sessions, Session
from infra.config import config, ConfigError
from infra.logger import get_logger, LogEntry


app = FastAPI(title="Skill Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logger = get_logger()


# ===== 启动校验 =====
@app.on_event("startup")
async def _startup():
    try:
        config.validate()
    except ConfigError as e:
        logger.error("flow_step", "Startup", f"配置错误: {e}")


# ===== 工具/技能 REST =====
@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Skill Agent Backend",
        "version": "1.1",
        "session_count": len(sessions._sessions),
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "tools": _count_tools(),
        "skills": _count_skills(),
    }


@app.get("/api/skills")
async def list_skills():
    """列出所有技能(包含同 name 多版本)"""
    from skills.manager import get_skill_store
    store = get_skill_store()
    skills = store.list_all()
    return {
        "skills": [_skill_to_dict(s) for s in skills]
    }


def _skill_to_dict(s) -> dict:
    """统一序列化(支持 P2 之后 Skill 结构)"""
    return {
        "id": getattr(s, "id", None),
        "name": s.name,
        "version": getattr(s, "version", "1.0.0"),
        "capability": s.capability,
        "patterns": s.patterns,
        "tags": s.tags,
        "method": s.method,
        "steps": [
            {**st.to_dict(), "description": st.description}
            for st in getattr(s, "steps", [])
        ] if hasattr(s, "steps") else [],
        "source": getattr(s, "source", "builtin"),
        "author": getattr(s, "author", None),
        "created_at": getattr(s, "created_at", None),
        "updated_at": getattr(s, "updated_at", None),
    }


@app.delete("/api/skills/{name}")
async def delete_skill(name: str):
    """删除一个技能(按 name 全删,所有版本)

    注: builtin 技能可删除(因为我们目前是单用户系统),后续多用户时改为鉴权。
    """
    from skills.manager import get_skill_store
    from pathlib import Path
    store = get_skill_store()
    removed = []
    for s in list(store.list_all()):
        if s.name == name:
            # 文件
            for sub in ("builtin", "user"):
                d = store.base_path / sub
                if d.exists():
                    for f in d.glob(f"{name}*.yaml"):
                        try:
                            f.unlink()
                            removed.append(str(f))
                        except Exception as e:
                            logger.error("flow_step", "Skills", f"删除失败 {f}: {e}")
            # 内存
            store._registry._by_name.pop(s.name, None)
            store._registry._by_id.pop(s.id, None)
            store._skills.pop(s.name, None)
    if not removed:
        raise HTTPException(status_code=404, detail=f"技能 {name} 不存在")
    logger.info("flow_step", "Skills", f"删除技能 {name},文件: {removed}")
    return {"deleted": name, "files": removed}


@app.delete("/api/skills/{name}/{version}")
async def delete_skill_version(name: str, version: str):
    """删除指定版本的技能"""
    from skills.manager import get_skill_store
    store = get_skill_store()
    target = store.get_by_name(name)
    if not target or target.version != version:
        raise HTTPException(status_code=404, detail=f"技能 {name}@{version} 不存在")
    removed = []
    for sub in ("builtin", "user"):
        d = store.base_path / sub
        if d.exists():
            for f in d.glob(f"{name}@{version}.yaml"):
                try:
                    f.unlink()
                    removed.append(str(f))
                except Exception as e:
                    logger.error("flow_step", "Skills", f"删除失败 {f}: {e}")
    # 内存:仅删该版本
    store._registry._by_id.pop(target.id, None)
    if target.version == version:
        # 同名多个版本只删这一个
        pass
    store._skills.pop(name, None)
    return {"deleted": f"{name}@{version}", "files": removed}


@app.post("/api/skills/reload")
async def reload_skills():
    """从磁盘重新加载技能"""
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
            {"name": name, "description": tool.description}
            for name, tool in learning.tools.items()
        ]
    }


@app.get("/api/sessions/{client_id}/trace")
async def get_session_trace(client_id: str):
    """获取 session 最近一次 trace"""
    sess = sessions.get(client_id)
    if not sess:
        return {"error": "session not found"}
    # 取当前 trace
    trace = sess.agent.logger.get_trace(sess.agent.logger._current_trace.trace_id) \
        if sess.agent.logger._current_trace else None
    if not trace:
        return {"trace_id": "", "entries": []}
    return {
        "trace_id": trace.trace_id,
        "entries": [e.to_dict() for e in trace.entries],
    }


def _count_tools() -> int:
    from agents.learning import LearningAgent
    return len(LearningAgent().tools)


def _count_skills() -> int:
    from skills.manager import get_skill_store
    return len(get_skill_store().list_all())


# ===== WebSocket 路由 =====
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    session: Session = None
    push_queue: asyncio.Queue = asyncio.Queue()

    async def push(event: str, payload: dict):
        """异步入队,由 sender 协程发送"""
        msg = {
            "type": "event",
            "event": event,
            "trace_id": (session.agent.logger._current_trace.trace_id
                         if session and session.agent.logger._current_trace else ""),
            "ts": datetime.now().isoformat(),
            "payload": payload,
        }
        await push_queue.put(msg)

    def push_sync(event: str, payload: dict):
        """同步事件(用于 on_event 同步回调)。用 run_coroutine_threadsafe 入队。"""
        try:
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(push(event, payload), loop)
        except RuntimeError:
            pass

    async def sender():
        while True:
            msg = await push_queue.get()
            try:
                await websocket.send_json(msg)
            except Exception:
                break

    sender_task = asyncio.create_task(sender())
    log_callback = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await push("error", {"message": "invalid JSON"})
                continue

            mtype = msg.get("type")

            # ----- 初始化 -----
            if mtype == "init":
                client_id = msg.get("client_id") or str(uuid.uuid4())
                session = await sessions.get_or_create(client_id)

                # 订阅日志,把每条 LogEntry 翻译成 event=log 推给前端
                log_callback = lambda entry: push_sync("log", entry.to_dict())
                session.agent.logger.subscribe(log_callback)
                session.log_callbacks.append(log_callback)

                await push("connected", {
                    "client_id": client_id,
                    "session_id": session.agent.session_id,
                })

            # ----- 聊天 -----
            elif mtype == "chat":
                if session is None:
                    await push("error", {"message": "请先发送 init"})
                    continue
                user_input = msg.get("content", "")
                if not user_input.strip():
                    await push("error", {"message": "content 为空"})
                    continue

                # 异步跑 Agent.handle
                try:
                    await session.agent.handle(user_input, push_sync)
                except Exception as e:
                    logger.error("flow_step", "WS", f"handle 失败: {e}")
                    await push("error", {"message": str(e)})

            # ----- 重置 -----
            elif mtype == "reset":
                if session:
                    session.agent.reset()
                await push("reset_ack", {"client_id": session.client_id if session else None})

            # ----- 获取技能 -----
            elif mtype == "ping":
                await push("pong", {})

            else:
                await push("error", {"message": f"未知 type: {mtype}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await push("error", {"message": f"连接异常: {e}"})
        except Exception:
            pass
    finally:
        # 清理订阅,避免泄漏
        if session and log_callback:
            try:
                session.agent.logger.unsubscribe(log_callback)
                session.log_callbacks.remove(log_callback)
            except Exception:
                pass
        sender_task.cancel()


# ===== 后台 GC =====
@app.on_event("startup")
async def _gc_loop():
    async def loop():
        while True:
            await asyncio.sleep(60)
            sessions.gc()
    asyncio.create_task(loop())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)