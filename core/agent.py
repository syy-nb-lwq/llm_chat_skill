"""Agent 核心"""
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

from core.plugin import PluginRegistry, ToolResult
from core.context import Context, Message


@dataclass
class Task:
    """任务"""
    description: str
    params: Dict[str, Any]
    

@dataclass
class Plan:
    """执行计划"""
    tasks: List[Task]
    current_index: int = 0


class Agent:
    """Agent 核心"""
    
    def __init__(
        self,
        llm_client,
        registry: PluginRegistry,
        system_prompt: str = ""
    ):
        self.llm = llm_client
        self.registry = registry
        self.context = Context()
        self.system_prompt = system_prompt
        self.max_iterations = 10
        
    def set_system_prompt(self, prompt: str):
        """设置系统提示"""
        self.system_prompt = prompt
    
    def chat(self, user_input: str, callback: Optional[Callable] = None) -> str:
        """对话"""
        self.context.add_user_message(user_input)
        
        iteration = 0
        while iteration < self.max_iterations:
            iteration += 1
            
            # 构建消息
            messages = self._build_messages()
            
            # 调用 LLM
            response = self.llm.chat(
                messages=messages,
                tools=self.registry.get_schemas()
            )
            
            assistant_msg = response["message"]
            self.context.add_assistant_message(
                assistant_msg["content"] or "",
                assistant_msg.get("tool_calls")
            )
            
            # 如果没有工具调用，返回回答
            if not assistant_msg.get("tool_calls"):
                return assistant_msg["content"]
            
            # 执行工具调用
            for tool_call in assistant_msg["tool_calls"]:
                result = self._execute_tool(tool_call)
                self.context.add_tool_message(str(result.data if result.success else result.error))
                
                if callback:
                    callback(f"\n🔧 调用工具: {tool_call['function']['name']}\n")
                    callback(f"📋 结果: {result.data if result.success else result.error}\n")
        
        return "达到最大迭代次数"
    
    def _build_messages(self) -> List[Dict]:
        """构建消息"""
        messages = []
        
        # 系统提示
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        
        # 历史消息
        messages.extend(self.context.to_llm_format())
        
        return messages
    
    def _execute_tool(self, tool_call: Dict) -> ToolResult:
        """执行工具"""
        func = tool_call["function"]
        name = func["name"]
        args = func.get("arguments", {})
        
        if isinstance(args, str):
            import json
            args = json.loads(args)
        
        plugin = self.registry.get(name)
        if not plugin:
            return ToolResult(
                success=False,
                error=f"工具不存在: {name}"
            )
        
        try:
            return plugin.execute(args)
        except Exception as e:
            return ToolResult(
                success=False,
                error=str(e)
            )
    
    def reset(self):
        """重置"""
        self.context.clear()
