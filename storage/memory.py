"""记忆存储"""
import json
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class UserMemory:
    """用户记忆"""
    user_id: str
    
    # 偏好
    preferences: Dict[str, Any] = field(default_factory=dict)
    
    # 统计
    tool_usage: Dict[str, int] = field(default_factory=dict)
    file_types: Dict[str, int] = field(default_factory=dict)
    topics: Dict[str, int] = field(default_factory=dict)
    
    # 上下文
    contexts: list = field(default_factory=list)
    
    # 自定义
    custom: Dict[str, Any] = field(default_factory=dict)
    
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


class MemoryStore:
    """记忆存储"""
    
    def __init__(self, path: str = "memory"):
        self.path = Path(path)
        self.path.mkdir(exist_ok=True)
        self._memories: Dict[str, UserMemory] = {}
        self._load_all()
    
    def _load_all(self):
        """加载所有记忆"""
        for file in self.path.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                memory = UserMemory(**data)
                self._memories[memory.user_id] = memory
            except Exception as e:
                print(f"加载记忆失败: {file.name}: {e}")
    
    def get(self, user_id: str = "default") -> UserMemory:
        """获取记忆"""
        if user_id not in self._memories:
            self._memories[user_id] = UserMemory(user_id=user_id)
        return self._memories[user_id]
    
    def learn(self, user_id: str, event_type: str, data: Dict):
        """学习"""
        memory = self.get(user_id)
        
        if event_type == "tool_use":
            tool = data.get("tool_name", "")
            memory.tool_usage[tool] = memory.tool_usage.get(tool, 0) + 1
        
        elif event_type == "file_type":
            ext = data.get("extension", "").lower()
            memory.file_types[ext] = memory.file_types.get(ext, 0) + 1
        
        elif event_type == "topic":
            topic = data.get("topic", "")
            memory.topics[topic] = memory.topics.get(topic, 0) + 1
        
        elif event_type == "preference":
            key = data.get("key", "")
            value = data.get("value", "")
            memory.preferences[key] = value
        
        elif event_type == "context":
            context = data.get("context", "")
            memory.contexts.append({
                "time": datetime.now().isoformat(),
                "content": context
            })
            # 只保留最近 20 条
            memory.contexts = memory.contexts[-20:]
        
        memory.updated_at = datetime.now().isoformat()
        self._save(memory)
    
    def _save(self, memory: UserMemory):
        """保存记忆"""
        file = self.path / f"{memory.user_id}.json"
        file.write_text(json.dumps(asdict(memory), ensure_ascii=False, indent=2), encoding="utf-8")
    
    def get_profile(self, user_id: str = "default") -> str:
        """获取用户画像"""
        memory = self.get(user_id)
        
        # 常用工具
        top_tools = sorted(memory.tool_usage.items(), key=lambda x: x[1], reverse=True)[:3]
        tools_str = ", ".join([t[0] for t in top_tools]) if top_tools else "暂无"
        
        # 常用文件
        top_files = sorted(memory.file_types.items(), key=lambda x: x[1], reverse=True)[:3]
        files_str = ", ".join([f[0] for f in top_files]) if top_files else "暂无"
        
        return f"""用户画像:
- 工具使用: {tools_str}
- 文件偏好: {files_str}
- 话题: {list(memory.topics.keys())[:5] or '暂无'}"""


from dataclasses import asdict

# 全局实例
_memory_store: Optional[MemoryStore] = None


def get_memory_store(path: str = "memory") -> MemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(path)
    return _memory_store
