# 03 — Skills 模块设计

> 本文档描述技能(Skill)的数据模型、加载机制、版本管理、与 Agent 的协作。

---

## 1. 现状

[skills/manager.py](../skills/manager.py) 实现了:

| 能力 | 状态 |
|---|---|
| 从 `.md` 文件加载技能 | ✅ |
| 增删查 + 内存索引 | ✅ |
| `matches(intent)` 关键词匹配 | ✅(简单 in 判断) |
| `version` 字段 | ✅ 但从未递增 |
| `code` 字段 | ❌ README 提到但代码无 |
| 真正的步骤执行 | ❌ 只把 `steps` 当字符串塞进 prompt |

**核心问题**:**Skill 不可执行**,只是给 LLM 的 prompt 片段。

---

## 2. 改进目标

1. Skill 升级为**可执行规格(executable spec)**
2. 步骤声明**输入/输出/依赖/降级**
3. 支持**版本化** + 冲突仲裁
4. 文件格式从 `.md` 升级为 `.yaml`(人可读 + 机器可解析)

---

## 3. 新版数据模型

### 3.1 `Skill` dataclass

```python
# skills/models.py  (从 manager.py 拆出)
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Literal
from datetime import datetime
import uuid


@dataclass
class SkillStep:
    """技能的一个可执行步骤。"""
    id: str                                    # 步骤唯一 id
    name: str
    description: str                           # 给 LLM/人的说明
    tool: Optional[str] = None                 # 关联的工具名
    input_schema:  Dict = field(default_factory=dict)   # JSON Schema
    output_schema: Dict = field(default_factory=dict)
    depends_on:   List[str] = field(default_factory=list)
    parallel_group: Optional[str] = None       # 同组可并行
    template:     Optional[str] = None         # 输出模板
    fallback:     Optional[str] = None         # 失败时跳到的 step id
    retry:        int = 0                      # 失败重试次数
    timeout_s:    int = 30


@dataclass
class Skill:
    name: str
    version: str = "1.0.0"
    capability: str = ""                       # 能力描述(给 Manager 看)
    method:      str = ""                      # 总方法论(给 Orchestrator 看)
    patterns:    List[str] = field(default_factory=list)   # 触发关键词
    tags:        List[str] = field(default_factory=list)

    steps:       List[SkillStep] = field(default_factory=list)
    examples:    List[str] = field(default_factory=list)

    # 元数据
    id:         str = field(default_factory=lambda: f"skill_{uuid.uuid4().hex[:8]}")
    source:     Literal["builtin", "taught", "imported"] = "builtin"
    author:     Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 兼容性:保留旧字段,过渡期使用
    legacy_steps_text: List[str] = field(default_factory=list)  # 原 steps 列表
    legacy_code:       Optional[str] = None                     # 原 code 字段
```

### 3.2 与旧 `Skill` 的兼容

旧 `Skill` 是"扁平 + 自然语言 steps",新 `Skill` 是"嵌套 + 结构化 steps"。

迁移策略:
- 旧 `.md` 加载时:把自然语言步骤包装成 `SkillStep(id="s{i}", name=line, description=line)`,不声明 tool/depends_on
- 升级后的执行器**优先用新结构**;若全部 step 没有 tool 字段,降级走"LLM 按 method 自组织"老路径
- 通过 `Skill.legacy_steps_text` 保留原文,可后续手工升级到结构化

---

## 4. 文件格式(YAML)

### 4.1 路径组织

```
skills/
├── builtin/                ← 内置技能,随项目发布
│   ├── travel_plan.yaml
│   ├── data_analysis.yaml
│   └── simple_chat.yaml
├── user/                   ← 用户教导/导入的技能
│   └── (运行时生成)
└── _disabled/              ← 软删除/失效的技能(手动管理)
```

### 4.2 文件示例:`travel_plan.yaml`

```yaml
name: travel_plan
version: 1.2.0
capability: 根据天气和景点信息规划旅游行程
method: |
  结合天气和景点信息,给出按时段安排的旅游行程。
  - 优先考虑天气适宜的户外景点
  - 雨天调整为室内景点
  - 行程按上午/下午/晚上三段组织
patterns:
  - 行程
  - 旅游
  - 旅行
  - 玩几天
  - 怎么玩
tags:
  - 旅游
  - 行程
source: builtin
author: system
steps:
  - id: fetch_weather
    name: 查询目的地天气
    description: 获取用户指定城市和日期的天气
    tool: weather_query
    input_schema:
      type: object
      properties:
        city: {type: string}
        date: {type: string}
      required: [city, date]
    output_schema:
      type: object
      properties:
        city:    {type: string}
        summary: {type: string}
        temp_min: {type: number}
        temp_max: {type: number}
    timeout_s: 15
    retry: 2

  - id: fetch_attractions
    name: 搜索景点
    description: 根据城市搜索推荐景点
    tool: web_search
    input_schema:
      type: object
      properties:
        query: {type: string}
      required: [query]
    depends_on: [fetch_weather]    # 等天气结果(可读 ${fetch_weather.data.city})
    parallel_group: attractions_search

  - id: synthesize_plan
    name: 整合行程
    description: 按 method 整合天气与景点,生成行程
    tool: null
    depends_on: [fetch_weather, fetch_attractions]
    template: |
      📅 {fetch_weather.data.city} 行程

      🌤 天气:{fetch_weather.data.summary}
      🌡 温度:{fetch_weather.data.temp_min}~{fetch_weather.data.temp_max}℃

      🏛 推荐景点:
      {fetch_attractions.data.attractions | bullet_list}
```

### 4.3 加载器

```python
# skills/loader.py
import yaml
from pathlib import Path
from skills.models import Skill, SkillStep


class SkillLoader:
    def __init__(self, base_path: Path):
        self.base_path = base_path

    def load_all(self) -> List[Skill]:
        skills = []
        for sub in ["builtin", "user"]:
            d = self.base_path / sub
            if not d.exists():
                continue
            for f in d.glob("*.yaml"):
                try:
                    skills.append(self._load_one(f))
                except Exception as e:
                    logger.error(LogType.SKILL_LOAD, "Skills", f"加载失败 {f}: {e}")
        return [s for s in skills if s is not None]

    def _load_one(self, f: Path) -> Skill:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        return Skill(
            name=data["name"],
            version=data.get("version", "1.0.0"),
            capability=data.get("capability", ""),
            method=data.get("method", ""),
            patterns=data.get("patterns", []),
            tags=data.get("tags", []),
            examples=data.get("examples", []),
            steps=[self._parse_step(s) for s in data.get("steps", [])],
            source=data.get("source", "builtin"),
            author=data.get("author"),
        )

    @staticmethod
    def _parse_step(raw: dict) -> SkillStep:
        return SkillStep(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            description=raw.get("description", ""),
            tool=raw.get("tool"),
            input_schema=raw.get("input_schema", {}),
            output_schema=raw.get("output_schema", {}),
            depends_on=raw.get("depends_on", []),
            parallel_group=raw.get("parallel_group"),
            template=raw.get("template"),
            fallback=raw.get("fallback"),
            retry=raw.get("retry", 0),
            timeout_s=raw.get("timeout_s", 30),
        )
```

> **过渡期**:加载器同时支持 `.yaml`(新)和 `.md`(旧),旧文件自动转成 legacy Skill。

---

## 5. 注册表 `SkillRegistry`

`SkillStore` 只做 CRUD,**`SkillRegistry`** 才负责"匹配"。

```python
# skills/registry.py
class SkillRegistry:
    def __init__(self, skills: List[Skill]):
        self._by_name = {s.name: s for s in skills}
        self._by_id   = {s.id:   s for s in skills}
        # 倒排索引:pattern -> [skill_name]
        self._pattern_index: Dict[str, List[str]] = {}
        for s in skills:
            for p in s.patterns:
                self._pattern_index.setdefault(p.lower(), []).append(s.name)

    def match(self, user_input: str, top_k: int = 3) -> List[Skill]:
        """匹配候选技能。
        1. 倒排索引粗筛
        2. 排序:pattern 在 input 中出现次数 / skill 优先级
        3. 返回 top_k
        """
        ...

    def match_semantic(self, user_input: str, embedder) -> List[Skill]:
        """可选:用 embedding 找语义最相似的技能,处理同义改写。"""
        ...

    def get(self, name: str) -> Optional[Skill]: ...
    def all(self) -> List[Skill]: ...

    def add(self, skill: Skill):
        """新增(用于教导),并写入文件。
        同名不同 version → 保留两版,标记 latest。
        同名同 version → 拒绝,要求升级 version。
        """
        ...

    def disable(self, name: str, reason: str): ...
```

### 5.1 匹配策略(改进)

旧的 `matches()` 用 `pattern in intent` 太脆弱。改进为**加权打分**:

```python
def match(self, user_input: str, top_k=3) -> List[Skill]:
    text = user_input.lower()
    scores = []
    for skill in self._by_name.values():
        score = 0
        for p in skill.patterns:
            if p.lower() in text:
                # 完整匹配 > 部分匹配
                score += 1 if text == p.lower() else 0.5
        if skill.capability and any(kw in text for kw in skill.capability.split()[:5]):
            score += 0.2
        if score > 0:
            scores.append((score, skill))
    scores.sort(key=lambda x: -x[0])
    return [s for _, s in scores[:top_k]]
```

未来可加 embedding 语义匹配,见 [08-roadmap.md](08-roadmap.md)。

---

## 6. 版本与冲突

### 6.1 存储格式

`skills/user/<name>@<version>.yaml`,如 `daily_summary@1.1.0.yaml`。

### 6.2 冲突规则

| 情况 | 处理 |
|---|---|
| 同名同 version 教导 | 拒绝,提示"请升级 version 或换 name" |
| 同名新 version | 写入新文件,旧版归档到 `_archive/` |
| 同名 builtin 教导 | 允许,但 source=`taught`,**优先级高于 builtin** |
| builtin 升级 | 写到 `builtin/`,version 递增,旧版归档 |

### 6.3 版本元数据

文件 front-matter 里强制带:

```yaml
_version_history:
  - version: 1.0.0
    date: 2026-01-15
    author: system
    note: 初版
  - version: 1.1.0
    date: 2026-07-01
    author: user
    note: 教导新增步骤
```

由 `SkillTrainer.persist()` 自动追加。

---

## 7. 失效检测

启动时 / 每次调用前做一次轻量校验:

```python
def validate(skill: Skill, tool_registry: ToolRegistry) -> List[str]:
    issues = []
    step_ids = {s.id for s in skill.steps}
    for s in skill.steps:
        if s.tool and s.tool not in tool_registry:
            issues.append(f"step {s.id} 引用未知工具: {s.tool}")
        for dep in s.depends_on:
            if dep not in step_ids:
                issues.append(f"step {s.id} 依赖不存在的 step: {dep}")
        if s.fallback and s.fallback not in step_ids:
            issues.append(f"step {s.id} fallback 不存在: {s.fallback}")
    # 检测循环依赖
    if _has_cycle(skill.steps):
        issues.append(f"skill {skill.name} 存在循环依赖")
    return issues
```

有 issue 的技能不参与 match,在管理接口里可见。

---

## 8. 教导沉淀(与 SkillTrainer 协作)

```python
# skills/registry.py
def add(self, skill: Skill, persist: bool = True) -> None:
    # 同名校验
    existing = self._by_name.get(skill.name)
    if existing and existing.version == skill.version:
        raise SkillConflictError(f"技能 {skill.name} v{skill.version} 已存在")
    skill.updated_at = datetime.now().isoformat()
    self._by_name[skill.name] = skill
    self._by_id[skill.id] = skill
    if persist:
        path = self.base_path / "user" / f"{skill.name}@{skill.version}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(asdict(skill), allow_unicode=True))
```

详见 [02-agents.md §6](02-agents.md#6-skill-trainer新增)。

---

## 9. 迁移 checklist

- [ ] 新建 `skills/models.py`、`skills/loader.py`、`skills/registry.py`
- [ ] 把旧 `manager.py` 中的 dataclass 移到 `models.py`
- [ ] YAML 加载器实现 + 单元测试
- [ ] 旧 `.md` 加载器保留,作为兼容路径
- [ ] `SkillRegistry.match()` 加权打分
- [ ] 版本冲突单元测试
- [ ] 失效检测函数
- [ ] 与 `SkillTrainer` 集成

---

## 10. 测试要点

| 测试 | 输入 | 期望 |
|---|---|---|
| YAML 加载 | 合法 yaml | Skill 对象字段全对 |
| YAML 加载 | 缺字段 | 抛 ValidationError |
| MD 加载 | 旧格式 md | legacy Skill |
| match | "厦门明天怎么玩" | travel_plan 在 top1 |
| 版本冲突 | 同名同 version 二次 add | 抛异常 |
| 失效检测 | 引用未知工具 | 报告 issue 不加载 |
| 循环依赖 | a→b→a | 报告 cycle |