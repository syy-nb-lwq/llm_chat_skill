"""FastAPI 后端服务 - WebSocket 通信"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict
from concurrent.futures import ThreadPoolExecutor
import json
import asyncio

from core.agent import Agent
from skills.manager import get_skill_store, reset_skill_store
from infra.logger import get_logger, LogEntry

app = FastAPI(title="Skill Agent API")

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    """WebSocket 连接管理器"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
    
    def disconnect(self, client_id: str):
        self.active_connections.pop(client_id, None)
    
    async def send_message(self, message: dict, client_id: str):
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json(message)
    
    async def broadcast(self, message: dict):
        for connection in self.active_connections.values():
            await connection.send_json(message)


manager = ConnectionManager()
executor = ThreadPoolExecutor(max_workers=4)


@app.get("/")
async def root():
    return {"status": "ok", "service": "Skill Agent Backend"}


@app.get("/api/skills")
async def list_skills():
    """获取技能列表"""
    # 重置技能库以确保加载最新文件
    reset_skill_store()
    store = get_skill_store()
    skills = store.list_all()
    return {
        "skills": [
            {
                "name": s.name,
                "capability": s.capability,
                "patterns": s.patterns,
                "tags": s.tags,
                "method": s.method,
                "steps": s.steps
            }
            for s in skills
        ]
    }


@app.get("/api/tools")
async def list_tools():
    """获取可用工具列表"""
    from agents.learning import LearningAgent
    learning = LearningAgent()
    return {
        "tools": [
            {
                "name": name,
                "description": tool.description
            }
            for name, tool in learning.tools.items()
        ]
    }


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket 聊天接口"""
    client_id = str(id(websocket))
    agent = Agent()
    logger = get_logger()
    
    await manager.connect(websocket, client_id)
    
    # 订阅日志
    def on_log_entry(entry: LogEntry):
        """日志回调"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(manager.send_message({
                    "type": "log",
                    "log": entry.to_dict()
                }, client_id))
        except:
            pass
    
    logger.subscribe(on_log_entry)
    
    # 创建发送步骤的回调函数
    async def send_step(step_type: str, message: str):
        """发送思考步骤到前端"""
        await manager.send_message({
            "type": "step",
            "step_type": step_type,
            "message": message
        }, client_id)
    
    try:
        # 发送连接成功消息
        await manager.send_message({
            "type": "connected",
            "message": "连接成功"
        }, client_id)
        
        while True:
            # 接收消息
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "chat":
                user_input = message.get("content", "")
                guidance_context = message.get("context", "")
                
                # 发送思考中状态
                await manager.send_message({
                    "type": "thinking",
                    "message": "正在处理..."
                }, client_id)
                
                # 启动追踪
                trace = logger.start_trace(f"chat-{user_input[:20]}")
                
                try:
                    # 创建同步的 callback 函数用于 agent
                    def sync_send_step(step_type: str, message: str):
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.ensure_future(send_step(step_type, message))
                        except:
                            pass
                    
                    # 在线程池中异步执行 LLM 调用
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        executor, 
                        lambda: agent.chat(user_input, callback=sync_send_step, guidance_context=guidance_context)
                    )
                    
                    # 处理返回结果 (可能是回答或指导提示)
                    if isinstance(result, tuple):
                        response, guidance = result
                        if guidance:
                            # 需要用户指导
                            await manager.send_message({
                                "type": "guidance_request",
                                "message": guidance
                            }, client_id)
                        if response:
                            await manager.send_message({
                                "type": "response",
                                "content": response
                            }, client_id)
                    else:
                        await manager.send_message({
                            "type": "response",
                            "content": result
                        }, client_id)
                    
                except Exception as e:
                    await manager.send_message({
                        "type": "error",
                        "message": str(e)
                    }, client_id)
                finally:
                    # 结束追踪
                    trace_summary = logger.end_trace()
                    
            elif message.get("type") == "reset":
                agent.reset()
                await manager.send_message({
                    "type": "reset",
                    "message": "对话已重置"
                }, client_id)
            
            elif message.get("type") == "get_logs":
                # 获取日志列表
                if trace and trace.entries:
                    await manager.send_message({
                        "type": "logs",
                        "logs": [e.to_dict() for e in trace.entries]
                    }, client_id)
                
    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as e:
        await manager.send_message({
            "type": "error",
            "message": f"连接错误: {str(e)}"
        }, client_id)
        manager.disconnect(client_id)
    finally:
        logger.unsubscribe(on_log_entry)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
