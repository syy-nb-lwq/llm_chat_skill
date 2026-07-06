# LLM Chat Skill 架构设计文档

## 1. 项目概述

### 1.1 项目目标

构建一个具有**自主学习能力**的智能助手 Agent，能够：
- 根据用户需求自动学习新技能
- 记忆用户偏好和使用习惯
- 支持多种数据源（网页、文件、数据库）
- 安全执行生成的代码
- 持续进化，不断增强能力

### 1.2 核心特性

| 特性 | 描述 |
|------|------|
| 🌐 多数据源 | 支持网页、PDF、图片、TXT 等 |
| 🧠 记忆系统 | 用户偏好、上下文、知识库 |
| 📚 技能学习 | 自动学习新技能并持久化 |
| 🔧 代码执行 | 安全执行 Python 代码 |
| 💾 向量存储 | 语义检索能力 |
| 🔌 插件系统 | 扩展性强 |

---

## 2. 系统架构

### 2.1 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                         User Interface                              │
│                    (CLI / Web UI / API)                           │
└─────────────────────────────────────────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Core (核心层)                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  Planner    │  │  Executor   │  │  Learner    │              │
│  │  任务规划   │  │  工具执行   │  │  技能学习   │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
│                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │                    Context Manager                             │ │
│  │                 (上下文管理、消息历史)                       │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│                      Plugin Layer (插件层)                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ Web      │  │ File     │  │ Code     │  │ Vector   │  ...    │
│  │ Plugin   │  │ Plugin   │  │ Plugin   │  │ Plugin   │        │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    Plugin Registry                          │   │
│  │                 (插件注册与管理中心)                       │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                  ↓
┌─────────────────────────────────────────────────────────────────────┐
│                    Infrastructure (基础设施)                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │ Config   │  │ Logger   │  │ Storage  │  │ Cache    │            │
│  │ 配置管理  │  │ 日志系统  │  │ 持久化   │  │ 缓存     │            │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘            │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 核心组件

#### 2.2.1 Agent Core

```python
class Agent:
    """Agent 核心"""
    
    def __init__(self, config: AgentConfig):
        self.planner = Planner()      # 任务规划器
        self.executor = Executor()    # 工具执行器
        self.learner = Learner()     # 技能学习器
        self.context = Context()      # 上下文管理
        self.plugin_registry = PluginRegistry()  # 插件注册表
```

#### 2.2.2 Planner (任务规划器)

**职责**：
- 理解用户意图
- 分解任务为子任务
- 选择合适的工具/技能
- 生成执行计划

**接口**：
```python
class Planner:
    def plan(self, user_input: str, context: Context) -> Plan:
        """生成执行计划"""
        
    def select_tool(self, task: Task, available_tools: List[Tool]) -> Tool:
        """选择工具"""
        
    def should_learn_skill(self, task: Task) -> bool:
        """判断是否需要学习新技能"""
```

#### 2.2.3 Executor (执行器)

**职责**：
- 调用工具执行任务
- 处理执行结果
- 管理执行状态
- 错误处理与重试

**接口**：
```python
class Executor:
    def execute(self, plan: Plan) -> ExecutionResult:
        """执行计划"""
        
    def execute_tool(self, tool: Tool, params: dict) -> ToolResult:
        """执行单个工具"""
        
    def retry(self, failed_task: Task) -> ToolResult:
        """重试失败任务"""
```

#### 2.2.4 Learner (学习器)

**职责**：
- 检测新技能需求
- 搜索学习资料
- 生成技能代码
- 验证并保存技能

**接口**：
```python
class Learner:
    def learn(self, requirement: str) -> Skill:
        """学习新技能"""
        
    def search_materials(self, keywords: List[str]) -> List[Material]:
        """搜索学习资料"""
        
    def generate_code(self, requirement: str, materials: List[Material]) -> str:
        """生成代码"""
        
    def verify(self, code: str) -> bool:
        """验证代码"""
```

---

## 3. 插件系统

### 3.1 插件架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Plugin Base                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                   │
│  @abstractmethod                                                │
│  def execute(self, params: dict) -> str: ...                    │
│                                                                   │
│  @abstractmethod                                                │
│  def validate(self, params: dict) -> bool: ...                  │
│                                                                   │
│  @abstractmethod                                                │
│  def get_schema(self) -> dict: ...                              │
└─────────────────────────────────────────────────────────────────────┘
                                  ↓
                    ┌───────────────────┐
                    │   Plugin Registry  │
                    │   (插件注册中心)   │
                    └───────────────────┘
                                  ↓
        ┌─────────────┬─────────────┬─────────────┬─────────────┐
        │ WebPlugin   │ FilePlugin  │ CodePlugin │ VectorPlugin│ ...
        │ (网页)     │ (文件)     │ (代码)    │ (向量)    │
        └─────────────┴─────────────┴─────────────┴─────────────┘
```

### 3.2 插件定义

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

@dataclass
class ToolSchema:
    """工具参数 schema"""
    name: str
    description: str
    parameters: Dict[str, Any]

@dataclass
class ToolResult:
    """工具执行结果"""
    success: bool
    data: Any
    error: Optional[str] = None

class BasePlugin(ABC):
    """插件基类"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """插件名称"""
        pass
    
    @property
    @abstractmethod
    def version(self) -> str:
        """插件版本"""
        pass
    
    @property
    def description(self) -> str:
        """插件描述"""
        return ""
    
    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """获取工具 schema（用于 LLM Function Calling）"""
        pass
    
    @abstractmethod
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        """执行插件"""
        pass
    
    def validate(self, params: Dict[str, Any]) -> bool:
        """验证参数"""
        return True
    
    def on_load(self):
        """插件加载时调用"""
        pass
    
    def on_unload(self):
        """插件卸载时调用"""
        pass
```

### 3.3 内置插件

#### 3.3.1 Web Plugin (网页)

```python
class WebPlugin(BasePlugin):
    """网页抓取插件"""
    
    name = "web"
    version = "1.0.0"
    description = "抓取网页内容，支持 JS 渲染"
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="fetch_webpage",
            description="抓取网页内容，返回标题和正文",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "网页 URL"
                    }
                },
                "required": ["url"]
            }
        )
    
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        url = params["url"]
        # 实现抓取逻辑
        ...
```

#### 3.3.2 File Plugin (文件)

```python
class FilePlugin(BasePlugin):
    """文件处理插件"""
    
    name = "file"
    version = "1.0.0"
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_file",
            description="读取本地文件",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "type": {"type": "string", "description": "文件类型"}
                },
                "required": ["path"]
            }
        )
```

#### 3.3.3 Code Plugin (代码执行)

```python
class CodePlugin(BasePlugin):
    """代码执行插件"""
    
    name = "code"
    version = "1.0.0"
    
    def __init__(self):
        self.sandbox = PythonSandbox()  # 沙箱环境
    
    def execute(self, params: Dict[str, Any]) -> ToolResult:
        code = params.get("code")
        return self.sandbox.execute(code)
```

### 3.4 插件注册表

```python
class PluginRegistry:
    """插件注册表"""
    
    def __init__(self):
        self._plugins: Dict[str, BasePlugin] = {}
        self._schemas: List[Dict] = []
    
    def register(self, plugin: BasePlugin):
        """注册插件"""
        self._plugins[plugin.name] = plugin
        plugin.on_load()
        self._update_schemas()
    
    def unregister(self, name: str):
        """卸载插件"""
        if name in self._plugins:
            self._plugins[name].on_unload()
            del self._plugins[name]
            self._update_schemas()
    
    def get(self, name: str) -> Optional[BasePlugin]:
        """获取插件"""
        return self._plugins.get(name)
    
    def list_all(self) -> List[BasePlugin]:
        """列出所有插件"""
        return list(self._plugins.values())
    
    def get_schemas(self) -> List[Dict]:
        """获取所有工具 schema（用于 LLM）"""
        return self._schemas
    
    def _update_schemas(self):
        self._schemas = [
            {"type": "function", "function": p.get_schema().to_dict()}
            for p in self._plugins.values()
        ]
```

---

## 4. 技能系统

### 4.1 技能定义

```python
@dataclass
class Skill:
    """技能定义"""
    id: str
    name: str
    description: str
    plugin_name: str  # 关联的插件
    code: str         # 代码内容（可选）
    parameters: Dict  # 参数定义
    examples: List[str]  # 使用示例
    version: str = "1.0"
    created_at: str = ""
    updated_at: str = ""
    
    def to_markdown(self) -> str:
        """导出为 Markdown"""
        ...
    
    @classmethod
    def from_markdown(cls, content: str) -> 'Skill':
        """从 Markdown 导入"""
        ...
```

### 4.2 技能存储

```python
class SkillStore:
    """技能存储"""
    
    def __init__(self, path: str = "skills"):
        self.path = Path(path)
        self.path.mkdir(exist_ok=True)
        self._skills: Dict[str, Skill] = {}
        self._load_all()
    
    def add(self, skill: Skill) -> str:
        """添加技能"""
        ...
    
    def get(self, skill_id: str) -> Optional[Skill]:
        """获取技能"""
        ...
    
    def list_all(self) -> List[Skill]:
        """列出所有技能"""
        ...
    
    def delete(self, skill_id: str) -> bool:
        """删除技能"""
        ...
    
    def search(self, query: str) -> List[Skill]:
        """搜索技能"""
        ...
```

### 4.3 技能学习流程

```
用户需求
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│  1. 检测是否已有相关技能                                         │
│     → 是：直接使用                                               │
│     → 否：继续                                                   │
└─────────────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│  2. 分析需求，生成搜索关键词                                     │
│     LLM 分析需求 → 生成 3-5 个搜索查询                            │
└─────────────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│  3. 搜索学习资料                                                │
│     搜索 → 获取搜索结果 → 提取关键信息                            │
└─────────────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│  4. 生成技能代码                                                │
│     LLM 综合需求 + 资料 → 生成 Python 代码                         │
└─────────────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│  5. 验证代码                                                    │
│     执行代码 → 失败 → 修复（最多3次）→ 成功                       │
└─────────────────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│  6. 保存技能                                                   │
│     存储到 skills/ 目录 → 持久化                                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 5. 记忆系统

### 5.1 记忆类型

```python
@dataclass
class UserMemory:
    """用户记忆"""
    user_id: str
    
    # 偏好
    preferences: Dict[str, Any] = field(default_factory=dict)
    # 工具使用统计
    tool_usage: Dict[str, int] = field(default_factory=dict)
    # 文件类型偏好
    file_types: Dict[str, int] = field(default_factory=dict)
    # 学习的话题
    topics: Dict[str, int] = field(default_factory=dict)
    # 上下文历史
    contexts: List[Context] = field(default_factory=list)
    # 自定义记忆
    custom: Dict[str, Any] = field(default_factory=dict)
    
    def learn(self, event_type: str, data: Dict):
        """学习新信息"""
        ...
    
    def recall(self, query: str = None) -> Dict:
        """检索记忆"""
        ...
```

### 5.2 知识库

```python
class KnowledgeBase:
    """知识库 - 存储已读取的内容"""
    
    def __init__(self):
        self.entries: Dict[str, KnowledgeEntry] = {}
        self.vector_store: VectorStore  # 向量存储
    
    def add(self, source_id: str, content: str, metadata: Dict):
        """添加知识"""
        ...
    
    def search(self, query: str, top_k: int = 5) -> List[KnowledgeEntry]:
        """语义搜索"""
        ...
```

---

## 6. 代码执行

### 6.1 沙箱设计

```python
class PythonSandbox:
    """Python 代码沙箱"""
    
    def __init__(self):
        self.builtins = self._get_safe_builtins()
        self.globals = {"__builtins__": self.builtins}
    
    def _get_safe_builtins(self) -> Dict:
        """安全的内置函数"""
        return {
            # 数学
            'abs': abs, 'min': min, 'max': max, 'pow': pow, 'round': round,
            'sum': sum, 'divmod': divmod,
            
            # 类型转换
            'int': int, 'float': float, 'str': str, 'bool': bool,
            'list': list, 'dict': dict, 'set': set, 'tuple': tuple,
            
            # 序列
            'len': len, 'enumerate': enumerate, 'zip': zip,
            'map': map, 'filter': filter, 'range': range,
            'sorted': sorted, 'reversed': reversed, 'any': any, 'all': all,
            'slice': slice,
            
            # 对象
            'type': type, 'isinstance': isinstance,
            'getattr': getattr, 'setattr': setattr, 'hasattr': hasattr,
            
            # IO
            'print': self._safe_print,
        }
    
    def execute(self, code: str) -> ExecutionResult:
        """执行代码"""
        ...
    
    def execute_function(self, code: str, func_name: str, kwargs: Dict) -> ExecutionResult:
        """执行函数"""
        ...
```

### 6.2 安全考虑

| 风险 | 措施 |
|------|------|
| 文件系统写入 | 禁用 `open()` 或限制路径 |
| 网络请求 | 限制 `requests` 等库 |
| 系统命令 | 禁用 `os.system`, `subprocess` |
| 无限循环 | 设置超时 |
| 内存溢出 | 限制输出大小 |

---

## 7. 数据流

### 7.1 对话流程

```
用户输入
    ↓
┌─────────────────────────────────────────────────────────────────────┐
│                      Agent.receive()                                │
│  1. 保存用户消息到上下文                                           │
│  2. Planner.plan() → 生成执行计划                                    │
│  3. Executor.execute() → 执行工具                                   │
│  4. Learner.learn() → （如需要）学习新技能                           │
│  5. Memory.learn() → 学习用户偏好                                   │
│  6. 返回响应                                                      │
└─────────────────────────────────────────────────────────────────────┘
    ↓
用户响应
```

### 7.2 技能学习流程

```
需求分析
    ↓
检查现有技能 → 匹配 → 返回技能
    ↓ (无匹配)
生成搜索查询
    ↓
搜索学习资料
    ↓
综合生成代码
    ↓
验证代码（3次重试）
    ↓
保存技能
    ↓
返回技能信息
```

---

## 8. 配置管理

### 8.1 配置文件

```yaml
# config.yaml
agent:
  name: "LLM Chat Skill"
  version: "1.0.0"
  max_iterations: 10
  
llm:
  provider: "openai"  # openai / anthropic / local
  model: "gpt-4"
  api_key: "${LLM_API_KEY}"
  base_url: "https://api.openai.com/v1"
  
plugins:
  enabled:
    - web
    - file
    - code
    - vector
    - memory
    
storage:
  skills_path: "skills"
  memory_path: "memory"
  vector_path: "vector_store"
  
sandbox:
  timeout: 30
  max_output_size: 10000
```

### 8.2 环境变量

```bash
# .env
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4
```

---

## 9. 扩展指南

### 9.1 开发新插件

```python
from core.plugin import BasePlugin, ToolResult

class MyPlugin(BasePlugin):
    name = "my_plugin"
    version = "1.0.0"
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(...)
    
    def execute(self, params: Dict) -> ToolResult:
        # 实现逻辑
        return ToolResult(success=True, data="结果")

# 注册插件
agent.plugin_registry.register(MyPlugin())
```

### 9.2 开发新学习器

```python
class AdvancedLearner(Learner):
    """高级学习器"""
    
    def search_materials(self, keywords: List[str]) -> List[Material]:
        # 搜索更多来源
        ...
    
    def generate_code(self, requirement: str, materials: List[Material]) -> str:
        # 更智能的代码生成
        ...
```

---

## 10. 未来规划

| 阶段 | 功能 |
|------|------|
| v1.1 | Docker 沙箱，更安全的代码执行 |
| v1.2 | 多 Agent 协作 |
| v1.3 | API 服务化 |
| v2.0 | 支持更多数据源（数据库、API） |
| v2.1 | 插件市场 |
| v2.2 | 技能分享社区 |

---

## 附录

### A. 目录结构

```
llm_chat_skill/
├── core/                    # 核心模块
│   ├── agent.py            # Agent 核心
│   ├── planner.py          # 任务规划
│   ├── executor.py         # 执行器
│   ├── learner.py          # 学习器
│   ├── context.py          # 上下文
│   └── plugin.py           # 插件基类
├── plugins/                 # 插件
│   ├── __init__.py
│   ├── web.py             # 网页插件
│   ├── file.py            # 文件插件
│   ├── code.py            # 代码插件
│   └── vector.py          # 向量插件
├── storage/                # 存储
│   ├── skill.py           # 技能存储
│   ├── memory.py          # 记忆存储
│   └── knowledge.py       # 知识库
├── infra/                   # 基础设施
│   ├── config.py         # 配置
│   ├── logger.py         # 日志
│   └── sandbox.py        # 沙箱
├── ui/                     # 界面
│   ├── cli.py            # 命令行
│   └── web.py            # Web UI
├── config.yaml
├── requirements.txt
└── README.md
```

### B. API 列表

| 接口 | 方法 | 描述 |
|------|------|------|
| `/chat` | POST | 发送消息 |
| `/skill/learn` | POST | 学习新技能 |
| `/skill/list` | GET | 列出技能 |
| `/skill/execute` | POST | 执行技能 |
| `/memory/profile` | GET | 获取用户画像 |

### C. 参考资料

- LangChain Agent 设计
- OpenAI Function Calling
- Anthropic Tool Use
- AutoGPT 架构
