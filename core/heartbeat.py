"""Heartbeat - 主动任务调度器

参考 OpenClaw 的 Heartbeat 机制,定时唤醒 Agent 检查并执行任务。
"""
import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from infra.logger import get_logger


class HeartbeatStatus(Enum):
    """心跳状态"""
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class HeartbeatTask:
    """心跳任务"""
    id: str
    description: str          # 任务描述
    condition: str             # 触发条件 (如 "每天 8:00", "每隔 30 分钟")
    action: str                # 执行动作的描述
    enabled: bool = True
    last_run: Optional[str] = None
    last_result: Optional[str] = None
    last_status: HeartbeatStatus = HeartbeatStatus.IDLE
    error_count: int = 0


@dataclass
class HeartbeatResult:
    """心跳执行结果"""
    tasks_executed: List[str] = field(default_factory=list)
    tasks_skipped: int = 0
    status: HeartbeatStatus = HeartbeatStatus.IDLE
    message: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class HeartbeatLoader:
    """Heartbeat 配置加载器"""
    
    def __init__(self, heartbeat_path: Optional[Path] = None):
        if heartbeat_path is None:
            heartbeat_path = Path(__file__).parent.parent / "heartbeat" / "HEARTBEAT.md"
        self.heartbeat_path = heartbeat_path
        self.logger = get_logger()
    
    def load(self) -> List[HeartbeatTask]:
        """加载 HEARTBEAT.md 配置"""
        if not self.heartbeat_path.exists():
            self.logger.info("HeartbeatLoader", "HEARTBEAT.md 不存在,跳过")
            return []
        
        try:
            content = self.heartbeat_path.read_text(encoding="utf-8")
            return self._parse_content(content)
        except Exception as e:
            self.logger.error("HeartbeatLoader", f"加载失败: {e}")
            return []
    
    def _parse_content(self, content: str) -> List[HeartbeatTask]:
        """解析 HEARTBEAT.md 内容"""
        tasks = []
        lines = content.strip().split("\n")
        
        task_id = 1
        current_desc = None
        current_condition = None
        
        for line in lines:
            stripped = line.strip()
            
            # 跳过空行和注释
            if not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
                continue
            
            # 检查是否是任务行 (以 - 开头)
            if stripped.startswith("-"):
                desc = stripped[1:].strip()
                
                # 检查是否有条件标记
                condition = self._extract_condition(desc)
                
                if condition:
                    task = HeartbeatTask(
                        id=f"task_{task_id}",
                        description=desc,
                        condition=condition,
                        action=desc,
                    )
                    tasks.append(task)
                    task_id += 1
        
        return tasks
    
    def _extract_condition(self, description: str) -> Optional[str]:
        """从描述中提取触发条件"""
        # 常见的条件模式
        patterns = [
            r"每天\s*(\d{1,2}:\d{2})",           # 每天 8:00
            r"每隔\s*(\d+)\s*分钟",              # 每隔 30 分钟
            r"每隔\s*(\d+)\s*小时",              # 每隔 1 小时
            r"每周[一二三四五六日天]\s*(\d{1,2}:\d{2})",  # 每周一 9:00
            r"当.*时|如果.*|当.*后",             # 当某条件满足时
        ]
        
        for pattern in patterns:
            match = re.search(pattern, description)
            if match:
                return description  # 返回完整描述作为条件
        
        # 如果没有明确条件,视为默认条件 (每隔 30 分钟)
        return "每隔 30 分钟"


class HeartbeatScheduler:
    """心跳调度器"""
    
    HEARTBEAT_OK = "HEARTBEAT_OK"  # 无任务时的特殊标记
    
    def __init__(
        self,
        interval_seconds: int = 1800,  # 默认 30 分钟
        on_tasks_ready: Optional[Callable] = None,  # 任务就绪时的回调
    ):
        self.interval = interval_seconds
        self.on_tasks_ready = on_tasks_ready
        self.loader = HeartbeatLoader()
        self.logger = get_logger()
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            self.logger.warning("HeartbeatScheduler", "调度器已在运行")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info("HeartbeatScheduler", f"启动,间隔 {self.interval} 秒")
    
    async def stop(self) -> None:
        """停止调度器"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.logger.info("HeartbeatScheduler", "已停止")
    
    async def _run_loop(self) -> None:
        """运行循环"""
        while self._running:
            try:
                await self._execute_heartbeat()
            except Exception as e:
                self.logger.error("HeartbeatScheduler", f"执行失败: {e}")
            
            await asyncio.sleep(self.interval)
    
    async def _execute_heartbeat(self) -> HeartbeatResult:
        """执行一次心跳检查"""
        tasks = self.loader.load()
        
        if not tasks:
            return HeartbeatResult(status=HeartbeatStatus.IDLE, message=self.HEARTBEAT_OK)
        
        # 检查哪些任务需要执行
        tasks_to_run = []
        for task in tasks:
            if not task.enabled:
                continue
            if self._should_execute(task):
                tasks_to_run.append(task)
        
        if not tasks_to_run:
            return HeartbeatResult(
                status=HeartbeatStatus.IDLE,
                message=self.HEARTBEAT_OK,
                tasks_skipped=len(tasks),
            )
        
        # 通知回调
        if self.on_tasks_ready:
            try:
                self.on_tasks_ready(tasks_to_run)
            except Exception as e:
                self.logger.error("HeartbeatScheduler", f"回调失败: {e}")
        
        return HeartbeatResult(
            status=HeartbeatStatus.COMPLETED,
            tasks_executed=[t.id for t in tasks_to_run],
            tasks_skipped=len(tasks) - len(tasks_to_run),
        )
    
    def _should_execute(self, task: HeartbeatTask) -> bool:
        """判断任务是否应该执行"""
        if not task.last_run:
            return True
        
        try:
            last = datetime.fromisoformat(task.last_run)
            now = datetime.now()
            
            # 解析条件
            condition = task.condition
            
            if "每隔" in condition and "分钟" in condition:
                match = re.search(r"(\d+)\s*分钟", condition)
                if match:
                    interval_minutes = int(match.group(1))
                    elapsed = (now - last).total_seconds() / 60
                    return elapsed >= interval_minutes
            
            elif "每隔" in condition and "小时" in condition:
                match = re.search(r"(\d+)\s*小时", condition)
                if match:
                    interval_hours = int(match.group(1))
                    elapsed = (now - last).total_seconds() / 3600
                    return elapsed >= interval_hours
            
            elif "每天" in condition:
                match = re.search(r"(\d{1,2}):(\d{2})", condition)
                if match:
                    target_hour = int(match.group(1))
                    target_minute = int(match.group(2))
                    now_time = now.time()
                    target_time = time(target_hour, target_minute)
                    # 检查是否已过目标时间且上次运行不在今天
                    if now_time >= target_time and last.date() < now.date():
                        return True
            
            # 默认:检查是否超过 30 分钟
            elapsed = (now - last).total_seconds() / 60
            return elapsed >= 30
            
        except Exception:
            return True  # 解析失败时默认执行
    
    async def trigger_now(self) -> HeartbeatResult:
        """立即触发一次心跳"""
        return await self._execute_heartbeat()


class HeartbeatAgent:
    """心跳 Agent - 处理主动任务"""
    
    def __init__(self, scheduler: Optional[HeartbeatScheduler] = None):
        self.scheduler = scheduler or HeartbeatScheduler()
        self.logger = get_logger()
        self._executing_tasks: List[HeartbeatTask] = []
    
    async def start(self) -> None:
        """启动心跳处理"""
        await self.scheduler.start()
    
    async def stop(self) -> None:
        """停止心跳处理"""
        await self.scheduler.stop()
    
    async def execute_task(self, task: HeartbeatTask) -> str:
        """执行单个任务"""
        self.logger.info("HeartbeatAgent", f"执行任务: {task.description}")
        
        # 更新状态
        task.last_run = datetime.now().isoformat()
        task.last_status = HeartbeatStatus.RUNNING
        
        try:
            # 这里应该调用 LLM 执行实际任务
            # 目前只是模拟
            result = f"任务 '{task.description}' 执行完成"
            task.last_result = result
            task.last_status = HeartbeatStatus.COMPLETED
            return result
        except Exception as e:
            task.error_count += 1
            task.last_status = HeartbeatStatus.ERROR
            task.last_result = f"执行失败: {e}"
            raise
    
    async def execute_pending_tasks(self) -> List[str]:
        """执行所有待处理任务"""
        result = await self.scheduler.trigger_now()
        
        if result.status == HeartbeatStatus.IDLE:
            self.logger.info("HeartbeatAgent", result.message)
            return []
        
        results = []
        for task_id in result.tasks_executed:
            for task in self.scheduler.loader.load():
                if task.id == task_id:
                    try:
                        r = await self.execute_task(task)
                        results.append(r)
                    except Exception as e:
                        results.append(f"任务失败: {e}")
        
        return results


# ---- 全局单例 ----
_heartbeat_scheduler: Optional[HeartbeatScheduler] = None
_heartbeat_agent: Optional[HeartbeatAgent] = None


def get_heartbeat_scheduler() -> HeartbeatScheduler:
    """获取 HeartbeatScheduler 全局实例"""
    global _heartbeat_scheduler
    if _heartbeat_scheduler is None:
        _heartbeat_scheduler = HeartbeatScheduler()
    return _heartbeat_scheduler


def get_heartbeat_agent() -> HeartbeatAgent:
    """获取 HeartbeatAgent 全局实例"""
    global _heartbeat_agent
    if _heartbeat_agent is None:
        _heartbeat_agent = HeartbeatAgent()
    return _heartbeat_agent


def reset_heartbeat() -> None:
    """重置全局实例"""
    global _heartbeat_scheduler, _heartbeat_agent
    _heartbeat_scheduler = None
    _heartbeat_agent = None
