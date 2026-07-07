"""Agent 核心 - 多智能体协作"""
from typing import List, Dict, Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor
import time

from agents.manager import ManagerAgent
from agents.learning import LearningAgent
from agents.orchestrator import OrchestratorAgent
from infra.logger import get_logger, LogType


class Agent:
    """
    多智能体协作 Agent
    
    每个智能体都是流转中枢：
    ┌─────────────────────────────────────────────────────────────┐
    │  Manager Agent (流转中枢)                                    │
    │  - 意图识别                                                │
    │  - 选择技能（方法论）                                       │
    │  - 规划工具调用                                            │
    └────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
    ┌─────────────────────────────────────────────────────────────┐
    │  Learning Agent (流转中枢)                                  │
    │  - 执行工具调用                                            │
    │  - 获取数据                                                │
    └────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
    ┌─────────────────────────────────────────────────────────────┐
    │  Orchestrator Agent (流转中枢)                               │
    │  - 整合数据                                                │
    │  - 按技能方法论生成回答                                     │
    └─────────────────────────────────────────────────────────────┘
    """
    
    def __init__(
        self,
        system_prompt: str = "",
        max_workers: int = 4
    ):
        # 初始化三个流转中枢
        self.manager = ManagerAgent()
        self.learning = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        
        # 线程池用于并行执行任务
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        
        self.context: List[Dict] = []
        self.system_prompt = system_prompt
        self.logger = get_logger()
    
    def chat(
        self,
        user_input: str,
        callback: Optional[Callable] = None
    ) -> str:
        """
        多智能体协作处理用户输入
        
        Args:
            user_input: 用户输入
            callback: 步骤回调函数
        
        Returns:
            最终回答
        """
        start_time = time.time()
        
        # 记录输入数据
        self.logger.log_data("Agent", "in", "user_input", user_input)
        self.logger.log_flow("Agent", "开始处理用户请求")
        
        # 辅助函数：安全调用 callback
        def safe_callback(step_type: str, message: str):
            if callback:
                try:
                    callback(step_type, message)
                except:
                    pass
        
        safe_callback("thinking", "正在分析意图...")
        
        # 检查是否直接回答
        if self.manager.should_answer_directly(user_input):
            safe_callback("info", "直接回答")
            self.logger.log_flow("Agent", "直接回答，无需工具调用")
            response = self.orchestrator.generate_response(user_input)
            return response
        
        # 检查是否教导技能
        if self.manager.should_learn_skill(user_input):
            safe_callback("learning", "检测到教导意图...")
            return "请告诉我具体的方法和步骤，我来学习这个技能。"
        
        # ============================================================
        # Step 1: Manager 流转中枢 - 意图识别、技能选择、工具规划
        # ============================================================
        self.logger.log_flow("Manager", "开始流转：意图识别、技能选择、工具规划")
        
        plan = self.manager.analyze(user_input)
        
        safe_callback("intent", f"识别意图: {plan.intent}")
        
        if plan.selected_skill:
            safe_callback("skill", f"选择技能: {plan.selected_skill.name}")
        
        # ============================================================
        # Step 2: Learning 流转中枢 - 工具调用、数据获取
        # ============================================================
        tool_results = {}
        if plan.tool_tasks:
            safe_callback("tool", f"开始执行 {len(plan.tool_tasks)} 个工具任务")
            self.logger.log_flow("Learning", "开始流转：工具调用、数据获取")
            
            for task in plan.tool_tasks:
                task_type = task.get("type", "")
                params = task.get("params", {})
                
                safe_callback("tool", f"调用: {task_type}")
                
                result = self.learning.execute_task(task_type, params)
                tool_results[task_type] = result
                
                if result.success:
                    safe_callback("success", f"✓ {task_type} 完成")
                else:
                    safe_callback("error", f"✗ {task_type} 失败: {result.error}")
        
        # ============================================================
        # Step 3: Orchestrator 流转中枢 - 整合数据、生成回答
        # ============================================================
        safe_callback("plan", "整合数据，生成回答...")
        self.logger.log_flow("Orchestrator", "开始流转：整合数据、生成回答")
        
        response = self.orchestrator.orchestrate(
            user_input=user_input,
            tool_results=tool_results,
            selected_skill=plan.selected_skill
        )
        
        safe_callback("success", "回答生成完成")
        
        # 记录输出数据
        duration_ms = (time.time() - start_time) * 1000
        self.logger.log_data("Agent", "out", "response", response[:200] + "..." if len(response) > 200 else response)
        self.logger.log_flow("Agent", f"处理完成，耗时 {duration_ms:.0f}ms")
        
        return response
    
    def reset(self):
        """重置对话上下文"""
        self.context.clear()
        self.logger.log_flow("Agent", "对话上下文已重置")
