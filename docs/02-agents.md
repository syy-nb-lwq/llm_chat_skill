# 02 — Agents 模块设计

> 本文档描述 `agents/` 下 Manager / Learning / Orchestrator 三个流转中枢的现状、问题、改进设计。

---

## 1. 目录与职责

```
agents/
├── __init__.py
├── manager.py        ← Manager Agent (意图识别 + 规划)
├── learning.py       ← Learning Agent (执行工具)
├── orchestrator.py   ← Orchestrator Agent (整合 + 生成回答)
└── skill_trainer.py  ← (新增) Skill Trainer (教导意图识别 + 沉淀)
```

**统一基类**(新增,放 `core/agent_base.py`,被 agents/ 引用):

```
core/
└── agent_base.py     ← BaseAgent (统一 LLM 调用 / Trace / 重试)
```

---

## 2. `BaseAgent` 设计(新增)

### 2.1 为什么需要

当前问题:
- [agents/manager.py](../agents/manager.py) 和 [agents/orchestrator.py](../agents/orchestrator.py) 都直接 `get_llm_client().chat(messages)`
- Manager 手写 JSON 解析 + 容错,Orchestrator 没有;**两套逻辑**
- 所有 LLM 调用都没有 trace_id 关联,日志无法串联

### 2.2 接口

```python
# core/agent_base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from infra.llm import get_llm_client
from infra.logger import get_logger, LogType


class BaseAgent(ABC):
    """所有 Agent 的基类。统一 LLM 调用、Trace、重试。"""

    name: str = "BaseAgent"

    def __init__(self):
        self.llm = get_llm_client()
        self.logger = get_logger()

    @abstractmethod
    def system_prompt(self) -> str: ...

    # ---------- 统一入口 ----------

    def think(
        self,
        user_prompt: str,
        *,
        output_schema: Optional[Dict] = None,
        temperature: Optional[float] = None,
        retries: int = 3,
    ) -> str:
        """单轮 LLM 调用。
        - 若提供 output_schema,自动 JSON 解析 + 校验,失败重试
        - 每次调用都记录 LLM_REQUEST / LLM_RESPONSE 日志
        """
        messages = [
            {"role": "system", "content": self.system_prompt()},
            {"role": "user", "content": user_prompt},
        ]
        for attempt in range(1, retries + 1):
            self.logger.info(
                LogType.LLM_REQUEST, self.name,
                f"LLM request (attempt {attempt})",
                {"messages": messages, "schema": output_schema},
            )
            try:
                raw = self.llm.chat(messages, temperature=temperature)
            except Exception as e:
                self.logger.error(LogType.LLM_ERROR, self.name, str(e))
                if attempt == retries:
                    raise
                continue

            self.logger.info(
                LogType.LLM_RESPONSE, self.name,
                "LLM response", {"raw": raw[:2000]},
            )
            if output_schema is None:
                return raw
            parsed = self._try_parse_json(raw, output_schema)
            if parsed is not None:
                return parsed
            self.logger.warning(
                LogType.LLM_ERROR, self.name,
                f"JSON 不符合 schema,重试 ({attempt}/{retries})",
            )
        raise ValueError(f"{self.name}: 多次重试后仍无法产出符合 schema 的输出")

    def think_json(self, user_prompt: str, schema: Dict) -> Dict:
        return self.think(user_prompt, output_schema=schema)

    # ---------- 工具 ----------

    def _try_parse_json(self, raw: str, schema: Dict) -> Optional[Dict]:
        """尝试从 raw 中提取 JSON 并按 schema 校验。
        1. 用正则找最外层 {...}
        2. json.loads
        3. jsonschema.validate (新增依赖 jsonschema)
        """
        import json, re, jsonschema
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
            jsonschema.validate(obj, schema)
            return obj
        except Exception:
            return None
```

### 2.3 JSON Schema 例子

每个 Agent 的输出 schema 由各 Agent 内部定义并暴露:

```python
class ManagerAgent(BaseAgent):
    name = "Manager"

    PLAN_SCHEMA = {
        "type": "object",
        "properties": {
            "intent":         {"type": "string"},
            "selected_skill": {"type": "string"},
            "tool_tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type":   {"type": "string", "enum": ["weather_query", "web_search"]},
                        "params": {"type": "object"},
                    },
                    "required": ["type", "params"],
                },
            },
        },
        "required": ["intent", "tool_tasks"],
    }

    def plan(self, user_input: str) -> "PlanResult":
        return self.think_json(user_input, self.PLAN_SCHEMA)
```

---

## 3. Manager Agent

### 3.1 现状

[agents/manager.py](../agents/manager.py) 实现:
- `analyze(user_input) -> PlanResult`
- `should_answer_directly` / `should_learn_skill` 简单关键词判断
- 输出解析用 `re.search + json.loads`,无 schema 校验,无重试

### 3.2 问题

1. LLM 输出若不含 `{...}` 直接走"默认规划"(查厦门天气),**完全错的兜底**
2. `should_answer_directly` 写死关键词列表,扩展性差
3. **没有规划失败的明确语义**(返回什么 = 让上层重试?)
4. JSON 解析和 Orchestrator 重复
5. **闲聊和技能需求未有效区分**,闲聊可能被错误记录到技能库

### 3.3 意图识别增强 ✅

#### 3.3.1 意图类型常量

```python
class IntentType:
    CHITCHAT = "chitchat"     # 闲聊:问候、感谢、道别等
    SKILL = "skill"           # 技能需求:需要执行工具完成任务
    TEACH = "teach"           # 教导:用户教 Agent 新技能
    UNKNOWN = "unknown"        # 未知:需要进一步分析
```

#### 3.3.2 闲聊检测策略

| 策略 | 描述 | 示例 |
|------|------|------|
| 短输入检测 | 长度 <= 5 字符 | "你好", "ok", "hi" |
| 问候关键词 | 打招呼 | "你好", "hello", "早上好" |
| 感谢关键词 | 感谢回复 | "谢谢", "感谢", "thx" |
| 道别关键词 | 道别 | "再见", "拜拜", "bye" |
| 肯定回复 | 简单确认 | "好的", "嗯", "行" |
| 身份询问 | 询问 Agent | "你是谁", "你会什么" |

#### 3.3.3 意图识别流程

```
用户输入
    │
    ▼
┌─────────────────────┐
│ 闲聊检测(不调用 LLM) │─── 是 ───→ 返回 CHITCHAT
└─────────┬───────────┘
          │ 否
          ▼
┌─────────────────────┐
│ 教导检测(关键词匹配) │─── 是 ───→ 返回 TEACH
└─────────┬───────────┘
          │否
          ▼
┌─────────────────────┐
│ LLM 技能规划        │─── 成功 ──→ 返回 SKILL
└─────────┬───────────┘
          │ 失败
          ▼
      返回 UNKNOWN
```

#### 3.3.4 闲聊保护机制

- **闲聊不调用 LLM 技能规划**:节省 token,避免误判
- **闲聊不记录到记忆库**:避免污染 MemoryStore
- **闲聊不触发 ExecutionCritic**:避免错误评估

### 3.4 改进设计

#### 3.3.1 拆出三个职责清晰的方法

```python
class ManagerAgent(BaseAgent):
    name = "Manager"

    # 1. 直接回答判定
    def should_direct_answer(self, user_input: str, history: List[Message]) -> bool:
        """判断是否无需任何工具,可直接回答。
        优先用 LLM 分类(简单 intent),再 fallback 到关键词。
        """
        ...

    # 2. 教导意图判定
    def is_teaching_intent(self, user_input: str) -> bool:
        """识别用户在'教'我们做事,而非'问'。
        输出 bool + 教导内容(供 SkillTrainer 用)。
        """
        ...

    # 3. 任务规划
    def plan(self, user_input: str, context: Context) -> PlanResult:
        """输出 {intent, skill_ref, dag}。
        - skill_ref: Skill 名字
        - dag: 工具调用依赖图,可能含并行分支
        """
        ...
```

#### 3.3.2 输出升级为 DAG

```python
@dataclass
class PlanResult:
    intent: str
    selected_skill: Optional[str] = None
    # 升级:从 List[Task] 升级为可表达依赖的 DAG
    tool_tasks: List[ToolTask] = field(default_factory=list)

@dataclass
class ToolTask:
    id: str                                # 用于依赖引用
    type: str                              # 工具名
    params: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)  # 其它 task id
    parallel_group: Optional[str] = None   # 同 group 可并行
```

例:
```json
{
  "intent": "厦门明天行程规划",
  "selected_skill": "travel_plan",
  "tool_tasks": [
    {"id": "t1", "type": "weather_query", "params": {"city": "厦门", "date": "tomorrow"}},
    {"id": "t2", "type": "web_search", "params": {"query": "厦门景点推荐"}, "depends_on": ["t1"]}
  ]
}
```

> 详见 [04-tools.md](04-tools.md) 的 DAG 执行部分。

#### 3.3.3 错误处理

```python
class PlanError(Exception): ...

# analyze 抛 PlanError 而不是返回错误 PlanResult
def plan(self, user_input: str, context: Context) -> PlanResult:
    try:
        result = self.think_json(...)
    except ValueError:
        # 重试耗尽,降级:无技能 + 不调工具
        return PlanResult(intent=user_input, selected_skill=None, tool_tasks=[])
```

上层 `core/agent.py` 收到 `PlanResult(tool_tasks=[])` 走"直接回答"分支,**不会让请求失败**。

---

## 4. Learning Agent

### 4.1 现状

[agents/learning.py](../agents/learning.py) 实现 `execute_tasks`(串行)和 `execute_task`(单次)。

### 4.2 问题

1. 始终串行,**无法并行**(README 说能并行)
2. 工具返回 `ToolResult` 直接拼字符串,**结构化数据丢失**
3. 无重试、无超时统一控制

### 4.3 改进设计

```python
class LearningAgent(BaseAgent):
    name = "Learning"

    def __init__(self):
        super().__init__()
        self.tools: Dict[str, Tool] = {}
        self._load_builtins()

    def register(self, tool: Tool):
        self.tools[tool.name] = tool

    async def execute_dag(
        self,
        tasks: List[ToolTask],
        *,
        on_event: Optional[Callable[[ToolEvent], Awaitable[None]]] = None,
    ) -> Dict[str, ToolResult]:
        """执行 DAG:
        1. 拓扑排序
        2. 并行执行所有 indegree=0 的 task
        3. 每完成一个,resolve 下游 task 的 params(从结果中取变量)
        4. emit event 给上层
        5. 失败按 task.retry / task.fallback 处理
        """
        ...

    def resolve_params(
        self,
        template_params: Dict[str, Any],
        results: Dict[str, ToolResult],
    ) -> Dict[str, Any]:
        """支持 ${t1.data.city} 这类变量引用,从已完成 task 结果中替换。
        """
        ...
```

参数变量替换是 DAG 表达"先查天气再搜景点"的关键:

```json
{
  "id": "t2",
  "type": "web_search",
  "params": {"query": "${t1.data.city} 明天适合去的景点"},
  "depends_on": ["t1"]
}
```

---

## 5. Orchestrator Agent

### 5.1 现状

[agents/orchestrator.py](../agents/orchestrator.py) 的 `_generate_with_methodology` 只是把 `method + steps` 拼到 prompt,LLM 是否遵循完全靠自觉。

### 5.2 问题

1. 方法论不可执行 —— LLM 可能跳过某些 step
2. 工具输出被 `content` 字符串化,Orchestrator 无法区分不同工具结果的结构
3. 没有按 step 流式输出

### 5.3 改进设计

#### 5.3.1 两阶段生成

```python
class OrchestratorAgent(BaseAgent):
    name = "Orchestrator"

    async def orchestrate(
        self,
        user_input: str,
        step_results: Dict[str, StepResult],   # 每个 skill step 的结果
        skill: Skill,
        context: Context,
        *,
        on_delta: Callable[[str], Awaitable[None]],
    ) -> str:
        """两阶段:
        阶段 A: 按 skill.method 把所有 step 结果映射到模板字段
        阶段 B: 调用 LLM 生成最终自然语言回答(支持流式)
        """
        ...

    async def _stream_llm(self, messages, on_delta): ...
```

#### 5.3.2 模板化(可选)

对结构化任务(如"行程规划"),技能可以提供 jinja 模板:

```yaml
# travel_plan.yaml
method: "结合天气和景点信息,给出按时段安排的旅游行程"
template: |
  📅 {date} {city} 行程

  🌤 天气: {weather.summary}
  🌡 温度: {weather.temp_min}~{weather.temp_max}℃

  🏛 推荐景点:
  {attractions | bullet_list}

  📝 建议:
  {llm_generate}
```

`llm_generate` 这类保留字段触发 LLM 介入,其余字段直接填充。

#### 5.3.3 流式输出

通过 `on_delta` 回调把 LLM token 增量推到 WebSocket,前端打字机式显示。

---

## 6. Skill Trainer(新增)

[README 承诺](../README.md) 但**代码缺失**的关键能力。

### 6.1 触发识别

```python
TEACHING_KEYWORDS = [
    "以后", "记住", "下次", "按这个", "按我的", "原则", "方法论",
    "教你", "应该", "步骤是", "正确做法", "记住这个",
]

class SkillTrainer(BaseAgent):
    name = "SkillTrainer"

    def detect(self, user_input: str) -> Tuple[bool, Optional[str]]:
        """返回 (是否是教导意图, 候选教导内容)。
        启发式 + LLM 二次确认,避免误判。
        """

    def extract_skill(self, teaching: str) -> Skill:
        """从自然语言教导抽取 Skill:
        - name
        - method
        - capability
        - steps (List[str])
        - patterns (List[str])  触发该技能的关键词
        """
        # 使用 BaseAgent.think_json 配合 SKILL_SCHEMA
```

### 6.2 沉淀

```python
class SkillTrainer:
    def persist(self, skill: Skill) -> None:
        """写入 skills/user/<name>.yaml
        - 自动分配 created_at / updated_at / version=1.0.0 / source=taught
        - 同名技能:版本递增而非覆盖,留历史
        """
```

### 6.3 与 Manager 协作

`core/agent.py` 在收到用户消息时:

```
user_input
   │
   ├─→ SkillTrainer.detect?  ──Yes──► extract_skill → persist → 回复"已记住"
   │
   └─→ Manager.plan?         ──正常路径──► ...
```

UI 上教导后会有一个独立提示事件 `type: "skill_learned"`,前端高亮显示。

---

## 7. 调用关系图

```
                  ┌──────────────┐
                  │ core/agent   │
                  └──────┬───────┘
       ┌─────────────────┼─────────────────┐
       ▼                 ▼                 ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│SkillTrainer  │  │ManagerAgent  │  │Orchestrator  │
└──────────────┘  └──────┬───────┘  └──────────────┘
                         │
                         ▼
                  ┌──────────────┐
                  │LearningAgent │
                  └──────────────┘

所有 Agent 继承 BaseAgent
BaseAgent 引用 infra.llm / infra.logger
```

**禁止**:Agent 之间直接相互 import,避免循环依赖;所有跨 Agent 调用都经 `core/agent.py` 编排。

---

## 8. 测试要点

| Agent | 测试场景 |
|---|---|
| BaseAgent | 正常返回 / JSON 错误重试 / Schema 校验失败 / LLM 抛异常 |
| Manager | 意图识别正确 / 教导意图正确识别 / 规划失败降级 |
| Learning | 并行任务调度 / 依赖变量替换 / 工具失败重试 |
| Orchestrator | 流式输出 / 模板字段填充 / LLM 异常降级 |
| SkillTrainer | 启发式命中 / LLM 确认 / 持久化文件 |

---

## 9. 迁移计划

1. 新建 `core/agent_base.py`,迁移公共逻辑
2. Manager / Orchestrator 改为继承 `BaseAgent`,删除重复的 LLM 调用
3. Learning 新增 `execute_dag` 方法,**保留** `execute_tasks` 不变 → 旧调用方不受影响
4. 引入 `jsonschema` 依赖
5. 全链路跑通后,删除 Manager 里的手写 JSON 解析
6. 新建 SkillTrainer,从 `core/agent.py` 接入