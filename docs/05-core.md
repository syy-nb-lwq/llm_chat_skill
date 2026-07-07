# 05 — Core 模块设计

> 本文档描述 `core/` 下 Agent 编排、Context、Capability、Plugin 四个核心组件。

---

## 1. 目录与职责

```
core/
├── agent.py             ← Agent 主类:串联 Manager / Learning / Orchestrator
├── agent_base.py        ← (新增) BaseAgent:统一 LLM 调用/Trace/重试
├── context.py           ← 对话上下文(已实现,需激活)
├── capability.py        ← 能力分析器(已实现,需接入)
├── plugin.py            ← 插件基类(已实现,与 ToolRegistry 合并)
└── dag.py               ← (新增) DAGExecutor:按 skill.steps 执行
```

---

## 2. `Agent` 主类(`core/agent.py`)

### 2.1 现状

[core/agent.py](../core/agent.py) 实现了 `Agent.chat()`,流程:

```
should_answer_directly?
   ├─ Yes → orchestrator.generate_response
   └─ No  → should_learn_skill?
              ├─ Yes → 提示用户给具体步骤(无实现)
              └─ No  → manager.analyze
                       → learning.execute_task (逐个,串行)
                       → orchestrator.orchestrate
```

### 2.2 问题

1. **没有任何 Context 累积**:`self.context` 定义了不用,多轮对话无记忆
2. **每个 WebSocket 连接都 `Agent()`**,不能跨请求共享(导致 context 失效)
3. 工具执行 **逐个串行**,即使 Manager 给出独立任务也串行
4. 回调 `sync_send_step` 用 `asyncio.ensure_future` 嵌套,容易丢消息
5. 失败处理只 catch 顶层异常,**没有 trace_id 关联**

### 2.3 改进设计

```python
# core/agent.py
class Agent:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.logger = get_logger()
        self.trace = self.logger.start_trace(f"session-{session_id}")

        # 三个 Agent(都继承 BaseAgent)
        self.manager      = ManagerAgent()
        self.learning     = LearningAgent()
        self.orchestrator = OrchestratorAgent()
        self.trainer      = SkillTrainer()

        # 真正的上下文
        self.context = Context()

    async def handle(
        self,
        user_input: str,
        on_event: Callable[[dict], Awaitable[None]],
    ):
        """新的 async 入口,所有事件通过 on_event 推送。"""
        turn_trace = self.logger.start_trace(f"turn-{uuid4().hex[:8]}")

        self.context.add_user_message(user_input)
        await on_event({"event": "user_message", "payload": {"content": user_input}})

        try:
            # 1. 教导意图优先
            is_teach, teach_content = self.trainer.detect(user_input)
            if is_teach:
                skill = self.trainer.extract_skill(teach_content)
                self.skill_registry.add(skill)
                await on_event({"event": "skill_learned",
                                "payload": {"name": skill.name, "version": skill.version}})
                return

            # 2. 直接回答判定
            if self.manager.should_direct_answer(user_input, self.context):
                await on_event({"event": "thinking", "payload": {"stage": "direct_answer"}})
                async for delta in self.orchestrator.stream(self.context):
                    await on_event({"event": "message_delta", "payload": {"delta": delta}})
                self.context.add_assistant_message(self._last_assistant_text)
                return

            # 3. 规划
            await on_event({"event": "thinking", "payload": {"stage": "planning"}})
            plan = self.manager.plan(user_input, self.context)
            await on_event({"event": "plan",
                            "payload": {"intent": plan.intent,
                                        "skill": plan.selected_skill,
                                        "tasks": [t.__dict__ for t in plan.tool_tasks]}})

            # 4. 工具 DAG 执行
            results = await self.learning.execute_dag(
                plan.tool_tasks,
                on_event=lambda e: on_event({"event": "tool_call", "payload": e.__dict__}),
            )

            # 5. 整合回答(流式)
            await on_event({"event": "thinking", "payload": {"stage": "synthesizing"}})
            async for delta in self.orchestrator.orchestrate(
                user_input, results, plan.selected_skill, self.context,
            ):
                await on_event({"event": "message_delta", "payload": {"delta": delta}})

            self.context.add_assistant_message(self._last_assistant_text)

        except Exception as e:
            self.logger.error(LogType.FLOW_STEP, "Agent", f"处理失败: {e}")
            await on_event({"event": "error", "payload": {"message": str(e)}})
        finally:
            self.logger.end_trace()
```

### 2.4 后端的 Session 管理

```python
# backend/main.py
class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, Agent] = {}

    def get_or_create(self, client_id: str) -> Agent:
        if client_id not in self._sessions:
            self._sessions[client_id] = Agent(session_id=client_id)
        return self._sessions[client_id]

    def destroy(self, client_id: str):
        self._sessions.pop(client_id, None)
```

WebSocket 路由:

```python
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    client_id = ...   # 客户端发的 UUID4
    session = sessions.get_or_create(client_id)

    async def push(event: dict):
        await ws.send_json({"type": "event", **event, "trace_id": session.trace.trace_id, "ts": now_iso()})

    try:
        ...
    finally:
        logger.unsubscribe(...)   # 避免泄漏
        # Context 不删,允许重连恢复
```

---

## 3. `BaseAgent`(`core/agent_base.py`,新增)

详见 [02-agents.md §2](02-agents.md#2-baseagent设计新增)。

---

## 4. `Context`(`core/context.py`)

### 4.1 现状

已定义 `Context` / `Message`,但 `Agent.chat()` 从不调用。

### 4.2 改进:真正接入并加压缩

```python
class Context:
    def add_user_message(self, content): ...
    def add_assistant_message(self, content, tool_calls=None): ...
    def add_tool_message(self, content, tool_call_id): ...

    def to_llm_messages(self, max_tokens=4000) -> List[Dict]:
        """生成 LLM 输入,token 超限时压缩早期消息。"""
        msgs = self.to_llm_format()
        return self._truncate(msgs, max_tokens)

    def _truncate(self, msgs, max_tokens):
        # 策略:保留 system + 最近 6 条;更早的 user/assistant 对合并成 summary
        # summary 用 LLM 生成(单独调用,不进主流程)
        ...

    def compress_via_llm(self, llm_client):
        """对最早的一段消息生成摘要,替换原文。"""
        ...
```

### 4.3 持久化(中期)

```python
class ContextStore:
    """按 session_id 持久化 Context 到 SQLite/Redis。
    短期内存即可,后期切 Redis。
    """
    def save(self, session_id: str, ctx: Context): ...
    def load(self, session_id: str) -> Optional[Context]: ...
    def delete(self, session_id: str): ...
```

---

## 5. `Capability` 分析器(`core/capability.py`)

### 5.1 现状

[core/capability.py](../core/capability.py) 定义了 8 个基础能力 + `analyze()` 做关键词匹配 → 输出 `gap / suggestions`。

**问题**:
- `analyzer` 是全局实例但**无人调用**
- 关键词匹配简陋,和新版 Skill/Tool 系统重复

### 5.2 改进:作为"技能不可用时的兜底"

```python
class CapabilityAnalyzer:
    BASE_CAPABILITIES = {...}   # 已有 8 个

    def analyze(self, task: str, registry: SkillRegistry, tools: ToolRegistry) -> Dict:
        """新签名:接收 registry 直接查实际可用资源。
        - 用关键词推断需要哪些基础能力
        - 查 registry:有 skill 覆盖该能力吗?
        - 查 tools:有 tool 支持该 skill 吗?
        - 输出 gap + 建议(添加 skill / 添加 tool)
        """
        ...
```

### 5.3 接入点

`core/agent.py` 在 Manager.plan() 之前调用一次,若 `gap` 非空:

- 一方面给前端发 `capability_gap` 事件,告知"我做不到 X,但我可以做 Y"
- 另一方面 LLM 也能在 system prompt 看到"已知能力边界"

---

## 6. `Plugin` 注册表(`core/plugin.py`)

### 6.1 现状

[core/plugin.py](../core/plugin.py) 定义了 `BasePlugin` / `PluginRegistry`,与 `tools/base.py` 的 `Tool` / `ToolResult` **完全重叠**。

### 6.2 决策:**合并到 Tool 系统**

| Plugin 的概念 | 归宿 |
|---|---|
| `BasePlugin.name/version/description` | → `Tool` 已有 + 加 `version` |
| `BasePlugin.execute(params)` | → `Tool.execute(**kwargs)` |
| `BasePlugin.get_schema()` | → `Tool.schema()` |
| `PluginRegistry` | → `ToolRegistry`(tools/registry.py) |
| `BasePlugin.on_load/on_unload` | → Tool 构造/析构时由 Registry 处理 |

迁移:
1. 把 `PluginRegistry` 标记 deprecated,所有调用改走 `ToolRegistry`
2. 删除 `core/plugin.py`
3. `core/capability.py` 里若引用 Plugin,改为 Tool

---

## 7. `DAGExecutor`(`core/dag.py`,新增)

技能步骤的执行器,详见 [03-skills.md §3.2](03-skills.md#32-与旧-skill-的兼容) 和 [04-tools.md §4](04-tools.md#4-dag执行learningagent-新能力)。

放在 `core/` 而不是 `agents/`,因为它是**编排逻辑**而非单一 Agent。

```python
class DAGExecutor:
    def __init__(self, learning: LearningAgent):
        self.learning = learning

    def topological_order(self, steps: List[SkillStep]) -> List[SkillStep]:
        ...

    def has_cycle(self, steps) -> bool: ...

    async def run(self, skill: Skill, user_input: str, ctx: Context,
                  on_event: Callable) -> Dict[str, ToolResult]: ...
```

---

## 8. 模块依赖总览

```
                       ┌──────────────────┐
                       │ backend/main.py  │
                       └────────┬─────────┘
                                │ Agent.handle
                                ▼
                       ┌──────────────────┐
                       │ core/agent.py    │
                       └────────┬─────────┘
            ┌───────────────────┼────────────────────┐
            ▼                   ▼                    ▼
    ┌──────────────┐    ┌──────────────┐     ┌──────────────┐
    │ ManagerAgent │    │LearningAgent │     │Orchestrator  │
    └──────┬───────┘    └──────┬───────┘     └──────┬───────┘
           │                   │                    │
           └───────────────────┴────────────────────┘
                               │
                               ▼
                       ┌──────────────────┐
                       │  BaseAgent       │
                       └────────┬─────────┘
                                │
                                ▼
                       ┌──────────────────┐
                       │ infra (LLM/Log)  │
                       └──────────────────┘

    SkillRegistry / ToolRegistry / Context / CapabilityAnalyzer
    都由 core/agent.py 在启动时构造,注入到各 Agent。
```

---

## 9. 测试要点

| 组件 | 测试 |
|---|---|
| Agent.handle | 正常 / 教导意图 / 直接回答 / 规划失败 |
| Context | 多轮累加 / 压缩 / to_llm_messages |
| CapabilityAnalyzer | 关键词推断正确 / gap 报告正确 |
| DAGExecutor | 拓扑序 / 循环检测 / 并行触发 |
| SessionManager | 同 client_id 复用 / 跨连接保留 context |

---

## 10. 迁移 checklist

- [ ] 新建 `core/agent_base.py`
- [ ] 新建 `core/dag.py`
- [ ] `core/agent.py` 改造为 async + Session 管理
- [ ] `core/context.py` 接入真实使用,加压缩
- [ ] `core/capability.py` 改为接收 registry,真正被调用
- [ ] 弃用 `core/plugin.py`,所有 Tool 走 `tools/registry.py`
- [ ] 写各模块测试
- [ ] 后端切换到 `SessionManager`