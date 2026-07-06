"""记忆工具 - 供智能体调用"""
from .memory import get_memory, UserMemory


def memory_tool(operation: str, key: str = None, value: str = None, query: str = None, 
                event_type: str = None, event_data: dict = None) -> str:
    """
    用户记忆系统工具
    
    参数:
    - operation: 操作类型 (recall/save/update/clear/profile/custom)
    - key: 自定义记忆的键
    - value: 自定义记忆的值
    - query: 检索查询词
    - event_type: 学习事件类型 (tool_use/file_type/topic/preference/context/pattern)
    - event_data: 事件数据 {"field": "value"}
    
    返回:
    - 操作结果的字符串描述
    """
    memory = get_memory()
    
    if operation == "recall":
        # 检索记忆
        results = memory.recall(query=query)
        
        if query:
            # 搜索相关上下文
            relevant = results.get("relevant_contexts", [])
            if relevant:
                return f"找到 {len(relevant)} 条相关记忆:\n" + "\n".join(
                    f"- {ctx['content'][:100]}..." for ctx in relevant[:5]
                )
            return f"没有找到与 '{query}' 相关的记忆"
        
        # 返回完整画像
        return memory.get_summary()
    
    elif operation == "save":
        # 保存自定义记忆
        if key and value:
            memory.save_custom(key, value)
            return f"已保存记忆: {key}"
        return "请提供 key 和 value 参数"
    
    elif operation == "update":
        # 更新偏好
        if key and value:
            memory.learn("preference", {"key": key, "value": value})
            return f"已更新偏好: {key} = {value}"
        return "请提供 key 和 value 参数"
    
    elif operation == "clear":
        # 清除记忆
        memory.clear()
        return "已清除所有记忆"
    
    elif operation == "profile":
        # 查看画像
        return memory.get_summary()
    
    elif operation == "custom":
        # 获取自定义记忆
        custom = memory.profile.get("custom", {})
        if not custom:
            return "没有自定义记忆"
        return "自定义记忆:\n" + "\n".join(
            f"- {k}: {v.get('value', '')}" for k, v in custom.items()
        )
    
    elif operation == "learn":
        # 学习新事件
        if event_type and event_data:
            memory.learn(event_type, event_data)
            return f"已学习: {event_type} - {event_data}"
        return "请提供 event_type 和 event_data"
    
    else:
        return f"未知操作: {operation}。可用操作: recall/save/update/clear/profile/custom/learn"
