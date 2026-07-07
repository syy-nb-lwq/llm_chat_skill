"""Learning Agent - 流转中枢：工具调用、数据获取"""
from typing import Dict, Any, List
from tools.base import ToolResult
from tools.weather import WeatherTool
from tools.search import SearchTool
from infra.logger import get_logger, LogType


class LearningAgent:
    """
    Learning Agent - 流转中枢
    
    职责：
    1. 执行工具调用 - 根据规划执行具体的工具
    2. 获取数据 - 通过工具获取完成任务所需的数据
    """
    
    def __init__(self):
        self.logger = get_logger()
        
        # 注册可用工具
        self.tools = {
            "weather_query": WeatherTool(),
            "web_search": SearchTool(),
        }
    
    def execute_tasks(self, tasks: List[Dict]) -> Dict[str, ToolResult]:
        """
        执行多个工具任务
        
        Args:
            tasks: 任务列表 [{"type": "weather_query", "params": {...}}, ...]
        
        Returns:
            结果字典 {"weather_query": ToolResult, "web_search": ToolResult}
        """
        results = {}
        
        self.logger.log_flow("Learning", f"开始执行 {len(tasks)} 个工具任务")
        
        for task in tasks:
            task_type = task.get("type", "")
            params = task.get("params", {})
            
            if task_type in self.tools:
                tool = self.tools[task_type]
                self.logger.log_tool_call(task_type, params)
                
                result = tool.execute(**params)
                results[task_type] = result
                
                if result.success:
                    self.logger.log_tool_success(task_type, result.content)
                else:
                    self.logger.log_tool_error(task_type, result.error)
            else:
                results[task_type] = ToolResult(
                    success=False,
                    error=f"未知工具类型: {task_type}"
                )
                self.logger.error(
                    LogType.TOOL_ERROR, 
                    "Learning", 
                    f"未知工具: {task_type}"
                )
        
        return results
    
    def execute_task(self, task_type: str, params: Dict[str, Any]) -> ToolResult:
        """
        执行单个工具任务
        
        Args:
            task_type: 工具类型
            params: 工具参数
        
        Returns:
            ToolResult
        """
        if task_type not in self.tools:
            return ToolResult(
                success=False,
                error=f"未知工具类型: {task_type}"
            )
        
        tool = self.tools[task_type]
        self.logger.log_tool_call(task_type, params)
        
        result = tool.execute(**params)
        
        if result.success:
            self.logger.log_tool_success(task_type, result.content)
        else:
            self.logger.log_tool_error(task_type, result.error)
        
        return result
    
    def list_tools(self) -> List[str]:
        """列出所有可用工具"""
        return list(self.tools.keys())
