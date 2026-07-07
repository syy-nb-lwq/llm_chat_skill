# 04 — Tools 模块设计

> 本文档描述工具(Tool)的契约、DAG 执行、扩展方式与已有工具的改进。

---

## 1. 现状

[tools/base.py](../tools/base.py):

```python
@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: str = ""

class Tool(ABC):
    name: str = ""
    description: str = ""
    @abstractmethod
    def execute(self, **kwargs) -> ToolResult: ...
    def validate(self, **kwargs) -> tuple[bool, str]: ...
```

已有工具:
- [tools/weather.py](../tools/weather.py) — `weather_query`(wttr.in)
- [tools/search.py](../tools/search.py) — `web_search`(4 后端级联:jina → bing → serpapi → duckduckgo)

### 问题清单

1. **`ToolResult` 在 tools 和 plugins 各定义一份**([core/plugin.py](../core/plugin.py) 也有同名 dataclass)
2. `validate()` 写好了但 LearningAgent 从不调用
3. 工具**没有声明参数 schema**,LLM 只能从 description 推断
4. **没有超时/重试的统一控制**,各工具自己 try/except
5. 工具结果被 Orchestrator 用 `content` 字符串化,**结构化数据丢失**
6. 工具元信息(name/description)只放在类属性,**没有集中注册表**

---

## 2. 改进后的契约

### 2.1 `Tool` 协议

```python
# tools/base.py
from typing import Any, Dict, Optional, Callable, Awaitable
from pydantic import BaseModel, Field


class ToolParam(BaseModel):
    """单个参数的 schema 声明"""
    name: str
    type: str                       # "string" | "number" | "integer" | "boolean" | "object" | "array"
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[list] = None


class ToolSchema(BaseModel):
    """工具对外暴露的完整 schema(给 LLM / 前端看)"""
    name: str
    description: str
    params: list[ToolParam] = Field(default_factory=list)
    returns: Dict[str, str] = Field(default_factory=dict)   # 字段名 -> 类型描述
    examples: list[Dict[str, Any]] = Field(default_factory=list)


@dataclass
class ToolResult:
    success: bool
    data: Any = None                       # 保留原始结构化数据!
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)   # 耗时/重试次数等

    def content(self) -> str:
        """给 LLM 看的字符串形式(保留给 Orchestrator 用)。
        实现:json.dumps(data, ensure_ascii=False, indent=2)
        """
        if self.success:
            return json.dumps(self.data, ensure_ascii=False, indent=2) if self.data is not None else ""
        return f"[错误] {self.error}"


class Tool(ABC):
    name: str = ""
    description: str = ""

    @abstractmethod
    def schema(self) -> ToolSchema: ...

    @abstractmethod
    def execute(self, **kwargs) -> ToolResult: ...

    def validate(self, params: Dict[str, Any]) -> tuple[bool, str]:
        """默认按 schema 校验,工具可覆盖。"""
        ...
```

### 2.2 关键改进

| 改动 | 解决的问题 |
|---|---|
| `ToolSchema` 显式声明参数 | LLM 不再瞎猜 |
| `ToolResult.data` 保留结构化 | Orchestrator 可选择性用结构而非字符串 |
| `ToolResult.meta` | 耗时/重试次数/源(哪个搜索后端成功)可观测 |
| `schema()` 抽象方法 | 工具元信息统一来源,前端/LLM 都能取 |
| 默认 `validate()` 按 schema 校验 | LearningAgent 无需关心各工具细节 |

---

## 3. 工具注册表 `ToolRegistry`

```python
# tools/registry.py
class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool):
        assert tool.name, "tool.name 必须非空"
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已存在")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]: ...
    def all(self) -> List[Tool]: ...
    def schemas(self) -> List[Dict]: ...       # 序列化给 LLM / 前端

    def validate_params(self, name: str, params: Dict) -> tuple[bool, str]:
        tool = self.get(name)
        if not tool:
            return False, f"未知工具: {name}"
        return tool.validate(params)
```

`LearningAgent` 改为持有 `ToolRegistry` 而非 dict,新增工具 = `registry.register(MyTool())`。

---

## 4. DAG 执行(LearningAgent 新能力)

### 4.1 从"串行 list"升级为"依赖图"

```python
@dataclass
class ToolTask:
    id: str                              # DAG 节点 id
    type: str                            # 工具名
    params: Dict[str, Any]               # 支持 ${other_id.data.field} 变量引用
    depends_on: List[str] = field(default_factory=list)
    parallel_group: Optional[str] = None
    retry: int = 0
    timeout_s: int = 30
    fallback_to: Optional[str] = None    # 失败时跳到另一 task id
```

### 4.2 执行算法

```python
# agents/learning.py
class LearningAgent(BaseAgent):
    async def execute_dag(
        self,
        tasks: List[ToolTask],
        *,
        on_event: Optional[Callable[[ToolEvent], Awaitable[None]]] = None,
    ) -> Dict[str, ToolResult]:
        """1. 拓扑排序(检测环)
        2. 同 parallel_group 且 indegree=0 的 task 并行
        3. 每完成一个:resolve 其下游 task 的 params(变量替换)
        4. emit event 给上层
        5. 失败:按 retry / fallback 处理
        """
        results: Dict[str, ToolResult] = {}
        in_flight: Dict[str, asyncio.Task] = {}
        pending = {t.id: t for t in tasks}
        completed = set()

        while pending or in_flight:
            # 找出可启动的:无未完成依赖的
            ready = [
                t for tid, t in pending.items()
                if all(d in completed for d in t.depends_on)
            ]
            # 启动(同 parallel_group 用 gather)
            by_group = self._group_by_parallel(ready)
            for grp in by_group:
                if len(grp) > 1:
                    await asyncio.gather(*[self._run_one(t, results, on_event) for t in grp])
                else:
                    await self._run_one(grp[0], results, on_event)
                for t in grp:
                    completed.add(t.id)
                    pending.pop(t.id, None)

            await asyncio.sleep(0)   # 让出事件循环

        return results
```

### 4.3 参数变量解析

```python
import re
_VAR_RE = re.compile(r"\$\{([\w\.]+)\}")

def resolve_params(template: Dict, results: Dict[str, ToolResult]) -> Dict:
    def replace(match):
        path = match.group(1).split(".")
        obj = results.get(path[0])
        if obj is None or not obj.success:
            return ""
        for k in path[1:]:
            if isinstance(obj, ToolResult):
                obj = obj.data
            obj = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
            if obj is None:
                return ""
        return str(obj)
    s = json.dumps(template, ensure_ascii=False)
    s = _VAR_RE.sub(replace, s)
    return json.loads(s)
```

例:`{"query": "${t1.data.city} 景点"}` 当 `t1` 结果是 `ToolResult(data={"city": "厦门"}, success=True)` 时,解析为 `{"query": "厦门 景点"}`。

---

## 5. 事件回调

DAG 执行过程中向上层 emit 事件,经 WebSocket 推前端:

```python
@dataclass
class ToolEvent:
    type: Literal["task_start", "task_success", "task_error", "task_retry", "dag_end"]
    task_id: str
    tool: Optional[str] = None
    params: Optional[Dict] = None
    result: Optional[ToolResult] = None
    error: Optional[str] = None
    duration_ms: float = 0
```

`core/agent.py` 把 `ToolEvent` 翻译成 WebSocket `event=tool_call / tool_result / error`。

---

## 6. 重试与超时

```python
async def _run_with_retry(self, task: ToolTask) -> ToolResult:
    last_err = None
    for attempt in range(task.retry + 1):
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(self.tools[task.type].execute, **task.params),
                timeout=task.timeout_s,
            )
            if res.success:
                return res
            last_err = res.error
        except asyncio.TimeoutError:
            last_err = f"超时({task.timeout_s}s)"
        except Exception as e:
            last_err = str(e)
        self.logger.warning(LogType.TOOL_ERROR, "Learning",
                            f"task {task.id} 第 {attempt+1} 次失败: {last_err}")
    return ToolResult(success=False, error=last_err)
```

`task.fallback_to` 指定后,**整体跳过**,继续后续 step。

---

## 7. 已有工具改造

### 7.1 `weather_query`

```python
class WeatherTool(Tool):
    name = "weather_query"
    description = "查询指定城市和日期的天气,返回温度、天气状况、风力等"

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            params=[
                ToolParam(name="city", type="string", description="城市中文名,如'厦门'"),
                ToolParam(name="date", type="string", description="YYYY-MM-DD 或 'today'/'tomorrow'", required=False),
            ],
            returns={
                "city": "string",
                "date": "string",
                "summary": "string (天气概述)",
                "temp_min": "number",
                "temp_max": "number",
                "humidity": "number",
                "wind": "string",
            },
            examples=[
                {"city": "厦门", "date": "tomorrow"},
                {"city": "北京"},
            ],
        )

    def execute(self, city: str, date: Optional[str] = None) -> ToolResult:
        start = time.time()
        try:
            data = self._fetch(city, date or "today")
            return ToolResult(
                success=True,
                data=data,                     # 保留结构化 dict
                meta={"source": "wttr.in", "duration_ms": (time.time()-start)*1000},
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e),
                              meta={"duration_ms": (time.time()-start)*1000})
```

### 7.2 `web_search`

`web_search` 改造成**返回结构化结果 + 标注来源后端**:

```python
def execute(self, query: str, max_results: int = 5) -> ToolResult:
    start = time.time()
    for backend_name, fn in self.backends:
        t0 = time.time()
        result = fn(query, max_results)
        if result.success:
            return ToolResult(
                success=True,
                data=result.data,                              # 保留搜索结果 list
                meta={
                    "source": backend_name,
                    "duration_ms": (time.time()-t0)*1000,
                    "total_duration_ms": (time.time()-start)*1000,
                },
            )
    return ToolResult(success=False, error="所有搜索后端失败",
                      meta={"total_duration_ms": (time.time()-start)*1000})
```

前端可在工具结果面板看到"用 jina 拿到 N 条结果,耗时 X ms"。

---

## 8. 新增工具的标准流程

1. 在 `tools/<name>.py` 实现 `Tool` 子类,实现 `schema()` + `execute()`
2. 在 `agents/learning.py` 的 `_load_builtins()` 里 `registry.register(YourTool())`
3. 写 `tests/tools/test_<name>.py`
4. 在 `docs/04-tools.md` 表格里登记

**示例**:新增 `pdf_reader.py` 读 PDF:

```python
class PdfReaderTool(Tool):
    name = "pdf_read"
    description = "读取本地 PDF 文件,返回每页文本内容"
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description,
            params=[ToolParam("path", "string", "PDF 文件绝对路径")],
            returns={"pages": "array<{page:number, text:string}>"},
        )
    def execute(self, path: str) -> ToolResult:
        from PyPDF2 import PdfReader
        r = PdfReader(path)
        return ToolResult(success=True, data={
            "pages": [{"page": i+1, "text": p.extract_text()} for i, p in enumerate(r.pages)]
        })
```

Manager 的可用工具列表自动更新(因为 LearningAgent 通过 registry 暴露)。

---

## 9. 工具级缓存(可选,中期)

```python
# tools/cache.py
class ToolCache:
    def __init__(self, ttl_s=300):
        self._store: Dict[Tuple[str, frozenset], ToolResult] = {}
        self._expires: Dict[Tuple[str, frozenset], float] = {}
        self.ttl_s = ttl_s

    def key(self, tool_name: str, params: Dict) -> Tuple[str, frozenset]:
        return (tool_name, frozenset(params.items()))

    def get(self, tool_name, params) -> Optional[ToolResult]:
        k = self.key(tool_name, params)
        if k not in self._store:
            return None
        if time.time() > self._expires[k]:
            self._store.pop(k, None)
            self._expires.pop(k, None)
            return None
        return self._store[k]

    def put(self, tool_name, params, result: ToolResult):
        k = self.key(tool_name, params)
        self._store[k] = result
        self._expires[k] = time.time() + self.ttl_s
```

LearningAgent 在调用工具前后查询/写入,**默认关闭,通过 env 开启**。

---

## 10. 测试要点

| 工具 | 测试 |
|---|---|
| Tool 基类 | validate() 默认行为 |
| weather_query | 正常返回 / 网络失败 / 城市名转换 |
| web_search | 4 后端级联 / 都失败时返回错误 |
| DAG 执行 | 拓扑排序正确 / 循环依赖报错 / 并行生效 |
| 变量替换 | `${t1.data.x}` 解析 / 上游失败时安全降级 |
| 重试 | 重试次数正确 / 超时生效 |
| ToolCache | 命中 / 过期失效 |

---

## 11. 迁移 checklist

- [ ] 新建 `tools/registry.py`
- [ ] `ToolResult` 增 `meta` 字段,旧调用兼容(默认 `None` 的 meta 取空 dict)
- [ ] `Tool` 加 `schema()` 抽象方法,旧工具实现该方法
- [ ] `LearningAgent` 改持 `ToolRegistry`
- [ ] 实现 `execute_dag()` + 参数变量解析
- [ ] 加 `jsonschema`(参数校验)+ `pydantic`(schema 定义)
- [ ] weather / search 工具按新接口改造
- [ ] 写工具测试