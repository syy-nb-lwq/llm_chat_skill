"""用户记忆系统 - 学习用户习惯"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional


class UserMemory:
    """用户记忆系统"""
    
    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.memory_dir = Path("memory")
        self.memory_dir.mkdir(exist_ok=True)
        self.profile_path = self.memory_dir / f"{user_id}_profile.json"
        self.profile = self._load_profile()
        
    def _load_profile(self) -> dict:
        """加载用户画像"""
        if self.profile_path.exists():
            try:
                with open(self.profile_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        
        return {
            "user_id": self.user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "preferences": {
                "answer_style": "normal",  # simple, normal, detailed
                "language": "中文",
                "timezone": "Asia/Shanghai"
            },
            "tool_usage": {},  # {tool_name: count}
            "file_types": {},  # {extension: count}
            "topics": {},  # {topic: count}
            "contexts": [],  # 最近对话上下文
            "patterns": [],  # 学习到的模式
            "custom": {}  # 用户自定义记忆
        }
    
    def _save_profile(self):
        """保存用户画像"""
        self.profile["updated_at"] = datetime.now().isoformat()
        with open(self.profile_path, 'w', encoding='utf-8') as f:
            json.dump(self.profile, f, ensure_ascii=False, indent=2)
    
    def learn(self, event_type: str, data: dict):
        """学习新信息"""
        if event_type == "tool_use":
            tool = data.get("tool_name", "")
            self.profile["tool_usage"][tool] = self.profile["tool_usage"].get(tool, 0) + 1
            
        elif event_type == "file_type":
            ext = data.get("extension", "").lower()
            self.profile["file_types"][ext] = self.profile["file_types"].get(ext, 0) + 1
            
        elif event_type == "topic":
            topic = data.get("topic", "")
            self.profile["topics"][topic] = self.profile["topics"].get(topic, 0) + 1
            
        elif event_type == "preference":
            key = data.get("key", "")
            value = data.get("value", "")
            self.profile["preferences"][key] = value
            
        elif event_type == "context":
            context = data.get("context", "")
            self.profile["contexts"].append({
                "time": datetime.now().isoformat(),
                "content": context
            })
            # 只保留最近20条
            self.profile["contexts"] = self.profile["contexts"][-20:]
            
        elif event_type == "pattern":
            pattern = data.get("pattern", "")
            if pattern not in self.profile["patterns"]:
                self.profile["patterns"].append(pattern)
        
        self._save_profile()
    
    def recall(self, query: str = None, max_results: int = 5) -> dict:
        """检索记忆"""
        results = {
            "preferences": self.profile["preferences"],
            "top_tools": self._top_k(self.profile["tool_usage"], 3),
            "top_file_types": self._top_k(self.profile["file_types"], 5),
            "recent_contexts": self.profile["contexts"][-max_results:],
            "patterns": self.profile["patterns"]
        }
        
        # 如果有查询，搜索相关上下文
        if query:
            relevant = [
                ctx for ctx in self.profile["contexts"]
                if query.lower() in ctx.get("content", "").lower()
            ]
            results["relevant_contexts"] = relevant
        
        return results
    
    def save_custom(self, key: str, value: str):
        """保存自定义记忆"""
        self.profile["custom"][key] = {
            "value": value,
            "time": datetime.now().isoformat()
        }
        self._save_profile()
    
    def clear(self):
        """清除记忆"""
        self.profile = {
            "user_id": self.user_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "preferences": self.profile.get("preferences", {}),
            "tool_usage": {},
            "file_types": {},
            "topics": {},
            "contexts": [],
            "patterns": [],
            "custom": {}
        }
        self._save_profile()
    
    def get_summary(self) -> str:
        """获取画像摘要"""
        top_tools = self._top_k(self.profile["tool_usage"], 3)
        top_files = self._top_k(self.profile["file_types"], 3)
        prefs = self.profile["preferences"]
        
        summary = f"""用户画像摘要:
- 回答风格: {prefs.get('answer_style', 'normal')}
- 语言: {prefs.get('language', '中文')}
- 常用工具: {', '.join([t for t, _ in top_tools]) if top_tools else '暂无'}
- 常用文件: {', '.join([f for f, _ in top_files]) if top_files else '暂无'}
- 学习模式: {len(self.profile['patterns'])} 个
- 自定义记忆: {len(self.profile['custom'])} 条"""
        
        return summary
    
    def _top_k(self, data: dict, k: int) -> list:
        """获取 Top K"""
        sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)
        return sorted_items[:k]
    
    def get_preferred_tools(self) -> list:
        """获取用户偏好的工具"""
        top = self._top_k(self.profile["tool_usage"], 3)
        return [tool for tool, _ in top]
    
    def get_preferred_answer_style(self) -> str:
        """获取用户偏好的回答风格"""
        return self.profile["preferences"].get("answer_style", "normal")


# 全局单例
_memory_store: dict = {}


def get_memory(user_id: str = "default") -> UserMemory:
    """获取用户记忆实例"""
    if user_id not in _memory_store:
        _memory_store[user_id] = UserMemory(user_id)
    return _memory_store[user_id]
