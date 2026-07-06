"""能力分析模块 - 分析任务需要的能力与现有能力的差距"""
from typing import List, Dict, Set
from dataclasses import dataclass


@dataclass
class Capability:
    """能力定义"""
    name: str
    description: str
    required_tools: List[str] = None  # 需要的基础工具
    tags: List[str] = None
    
    def __post_init__(self):
        self.required_tools = self.required_tools or []
        self.tags = self.tags or []


class CapabilityAnalyzer:
    """能力分析器"""
    
    # 基础能力库
    BASE_CAPABILITIES = {
        "fetch": Capability(
            name="fetch",
            description="获取外部数据（网页/API）",
            tags=["数据获取", "网络"]
        ),
        "read": Capability(
            name="read",
            description="读取本地文件",
            tags=["文件处理", "数据获取"]
        ),
        "code": Capability(
            name="code",
            description="执行代码",
            tags=["代码执行", "数据处理"]
        ),
        "write": Capability(
            name="write",
            description="写入/保存文件",
            tags=["文件处理"]
        ),
        "search": Capability(
            name="search",
            description="搜索信息",
            tags=["搜索"]
        ),
        "analyze": Capability(
            name="analyze",
            description="分析数据",
            tags=["分析", "数据处理"]
        ),
        "visualize": Capability(
            name="visualize",
            description="数据可视化",
            tags=["可视化", "图表"]
        ),
        "report": Capability(
            name="report",
            description="生成报告",
            tags=["输出"]
        ),
    }
    
    def analyze(self, task: str, plugins: List[str], skills: List[str] = None) -> Dict:
        """
        分析任务需要的能力与现有能力的差距
        
        Args:
            task: 任务描述
            plugins: 已有的插件能力
            skills: 已有的技能
        
        Returns:
            分析结果：
            - needed: 需要的能力
            - available: 可用的能力
            - gap: 能力差距
            - suggestions: 补足建议
        """
        plugins = set(plugins) if plugins else set()
        skills = set(skills) if skills else set()
        
        # 根据任务推断需要的能力
        needed = self._infer_needed_capabilities(task)
        
        # 已有能力
        available = plugins.copy()
        available.update(skills)
        
        # 能力差距
        gap = needed - available
        
        # 补足建议
        suggestions = self._generate_suggestions(gap, task)
        
        return {
            "task": task,
            "needed_capabilities": list(needed),
            "available_capabilities": list(available),
            "gap": list(gap),
            "suggestions": suggestions,
            "can_complete": len(gap) == 0
        }
    
    def _infer_needed_capabilities(self, task: str) -> Set[str]:
        """推断任务需要的能力"""
        task_lower = task.lower()
        needed = set()
        
        # 关键词匹配
        keywords_map = {
            "fetch": ["抓取", "爬取", "获取网页", "访问", "fetch", "crawl"],
            "read": ["读取", "打开", "查看文件", "read"],
            "code": ["执行", "运行代码", "code", "python"],
            "write": ["写入", "保存", "生成文件", "export", "save"],
            "search": ["搜索", "查找", "search"],
            "analyze": ["分析", "统计", "analyze"],
            "visualize": ["可视化", "图表", "画图", "visualize"],
            "report": ["报告", "总结", "report"],
        }
        
        for capability, keywords in keywords_map.items():
            if any(kw in task_lower for kw in keywords):
                needed.add(capability)
        
        return needed
    
    def _generate_suggestions(self, gap: Set[str], task: str) -> List[str]:
        """生成补足建议"""
        suggestions = []
        
        capability_hints = {
            "fetch": "需要添加网页抓取能力",
            "read": "需要添加文件读取能力",
            "code": "需要添加代码执行能力",
            "write": "需要添加文件写入能力",
            "search": "需要添加搜索能力",
            "analyze": "需要添加数据分析技能",
            "visualize": "需要添加可视化技能",
            "report": "需要添加报告生成技能",
        }
        
        for g in gap:
            if g in capability_hints:
                suggestions.append(capability_hints[g])
        
        if suggestions:
            suggestions.append(f"建议学习技能来处理：{task}")
        
        return suggestions


# 全局实例
analyzer = CapabilityAnalyzer()
