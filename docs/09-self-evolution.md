# 09 — 自我进化架构设计

> 本文档设计 Skill Agent 从"用户显式教导进化"升级为"自我进化"的完整架构。

---

## 1. 现状分析

### 1.1 已有的进化机制

```
✅ SkillTrainer         用户说"以后做X应该Y" → 抽取为 Skill 持久化
✅ DAG 执行反馈        工具失败 → 重试 → fallback
✅ Feature Flags       SKILL_DAG_ENABLED 等开关可动态切换
✅ 多轮 Context        单 Session 内记住对话历史
```

### 1.2 差距:真正的自我进化需要什么

| 能力 | 当前状态 | 缺失 |
|------|---------|------|
| **从执行结果中学习** | 工具结果直接给 Orchestrator,不评估 | 没有结果质量判断 |
| **从失败中沉淀** | 失败只重试,不生成改进建议 | 没有"失败日志→新/改进 Skill"闭环 |
| **跨 Session 经验** | Session 销毁后无记忆 | 没有长期记忆,经验随 Session 结束丢失 |
| **方法论自动优化** | Skill.method 手动编写,版本手动递增 | 没有 LLM 判断哪个版本更好 |
| **修改自身提示词** | 静态 system_prompt | 没有让 Agent 调整 prompt 的路径 |
| **主动反思** | handle() 一次性执行完 | 没有执行后回头看 trace 的环节 |

### 1.3 结论

> 当前架构能实现**用户显式教导进化**,但不能实现**自我进化**。
>
> 核心缺失两个:
> 1. **ExecutionCritic**:没有 Agent 看执行结果、生成改进建议的环节
> 2. **MemoryStore**:没有跨 Session 的长期记忆,经验无法积累

---

## 2. 目标架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      Self-Evolving Agent                          │
│                                                               │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────────┐  │
│  │   Manager    │───▶│   Learning   │───▶│  Orchestrator  │  │
│  └──────┬───────┘    └──────┬───────┘    └───────┬────────┘  │
│         │                    │                     │            │
│         └────────────────────┼─────────────────────┘            │
│                              ▼                                  │
│                   ┌──────────────────┐                          │
│                   │ ExecutionCritic  │ ← 新增:执行后评估        │
│                   └────────┬────────┘                          │
│                            │                                    │
│         ┌──────────────────┼──────────────────┐                │
│         ▼                  ▼                  ▼                │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────┐      │
│  │ MemoryStore │  │ SkillMerger│  │ SelfReflectLoop   │      │
│  │ (长期记忆)  │  │ (版本合并)  │  │ (主动反思循环)     │      │
│  └────────────┘  └────────────┘  └────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 核心组件设计

### 3.1 ExecutionCritic(执行批评器)

**职责**:在每次 DAG 执行后,评估结果质量,决定是否生成改进建议。

**触发时机**:`Agent.handle()` 末尾,所有工具执行完毕、Orchestrator 输出完成之后。

**评估维度**:

```
success_rate    = 成功工具数 / 总工具数
fallback_count  = 触发了 fallback 的次数
latency_ms     = 总执行耗时
user_feedback  = 用户是否追问/纠正(如果 Context 发现)
```

**评估策略**:

| 条件 | Action |
|------|--------|
| `success_rate < 0.5` | 生成"失败分析报告",存入 MemoryStore |
| `fallback_count > 0` 且结果仍差 | 生成"改进建议 Skill",存入待审阅 |
| `success_rate == 1.0` 且快速 | 记录"成功路径",强化匹配权重 |
| `user_feedback == correction` | 分析用户纠正,生成修正版 Skill |

**Critic 输出格式**:

```json
{
  "trace_id": "xxx",
  "scenario": "旅游规划",
  "evaluation": {
    "success_rate": 0.67,
    "fallback_count": 1,
    "latency_ms": 2300,
    "user_corrected": false
  },
  "diagnosis": "web_search 在景区名上有歧义,导致第二个工具拿到错误参数",
  "suggestion": {
    "type": "improve_skill",
    "target_skill": "travel_plan",
    "patch": {
      "method": "在搜索景点前,先用 entity_extraction 确认景区全称"
    }
  }
}
```

**是否调用 LLM**:仅在 `success_rate < 1.0` 时调用,且异步执行,不影响主流程响应时间。

---

### 3.2 MemoryStore(长期记忆)

**职责**:跨 Session 持久化经验,让 Agent 能记住"上次在 X 场景失败了 Y"。

**存储结构**:

```
memory/
├── failures/          # 失败分析报告
│   └── YYYY-MM/       # 按月归档
│       └── {trace_id}.json
├── successes/         # 成功路径(轻量,只存 pattern → skill 映射)
│   └── success_index.jsonl
├── skill_patches/     # 待审阅的改进建议
│   └── pending/
└── reflections/       # 主动反思结果
```

**核心 API**:

```python
class MemoryStore:
    def record_failure(trace_id, scenario, diagnosis, suggestion)
    def record_success(scenario, matched_skill, latency_ms)
    def get_recent_failures(scenario, top_k=5) -> List[FailureRecord]
    def get_skill_hints(scenario) -> List[str]   # 给 Manager 规划时参考
    def add_pending_patch(patch: SkillPatch)
    def approve_patch(patch_id)   # 手动审核后生效
    def auto_approve_low_risk(patch)  # 高置信度自动生效
```

**Manager 规划时集成**:

```python
# Manager.plan() 在读取 skills 之前,先查 MemoryStore
hints = memory.get_skill_hints(user_input)
if hints:
    user_input = f"{user_input}\n\n[历史失败教训: {hints}]"
```

**容量控制**:每月超过 50 条时,按 `success_rate` 排序,丢弃最低的 20%。

---

### 3.3 SkillMerger(技能版本合并)

**职责**:当同一 Skill 有多个版本时,用 LLM 判断哪个更好或合并为新版本。

**触发时机**:
- 手动:用户请求"优化 travel_plan 技能"
- 自动:同一 Skill 有 3+ 版本时,触发合并评估

**合并流程**:

```
v1.0.0 (手工)  ─┐
                 ├─▶ LLM 评估 ─▶ v1.2.0 (合并版,标记 source="merged")
v1.1.0 (Teach) ─┘
```

**LLM 合并 Prompt 模板**:

```
你是一个技能融合专家。以下是同一技能 {name} 的多个版本:

v{version1}: {method1}
v{version2}: {method2}
...

步骤:
1. 分析每个版本的优劣
2. 取长补短,生成一个融合版本
3. 输出 JSON: {{"method": "...", "patterns": [...], "steps": [...]}}
```

**版本标记**:合并版 Skill 标记 `source="merged"`,保留原始版本供参考。

---

### 3.4 SelfReflectLoop(主动反思循环)

**职责**:在低负载时(如 Session 空闲、凌晨),主动复盘近期经验,生成洞察。

**触发条件**(三者之一):
- 24h 内失败记录 > 5 条
- 同一场景失败 3 次
- 用户请求"复盘近期表现"

**反思输出示例**:

```
## 自我反思报告 [2026-07-09]

### 高频失败场景
1. 景区名歧义 (3次) → 已在 skill_patches/pending/
2. 搜索超时未 fallback (2次) → 建议修改 travel_plan skill.steps[1].retry=2

### 可优化技能
- travel_plan: method 偏模糊,建议加入"先确认实体再搜索"

### 强化技能
- weather_query: 成功率 100%,无需改动

### 记忆摘要
已记录 12 条经验,下次遇到旅游规划时 Manager 会收到历史教训提示
```

**是否写入 Skill**:仅高置信度(`confidence > 0.85`)时写入,其余存入 pending 待审阅。

---

## 4. 执行流程:带自我进化的 Agent.handle

```
用户输入
    │
    ▼
┌──────────────┐  历史教训 hints
│   Manager    │───────────────▶ MemoryStore.get_skill_hints()
└──────┬───────┘
       │ PlanResult
       ▼
┌──────────────┐
│   Learning   │──▶ DAG 执行
│   (DAG)      │
└──────┬───────┘
       │ ToolResults
       ▼
┌──────────────┐
│ Orchestrator │──▶ 用户回答
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────────────┐
│              ExecutionCritic                      │  (异步,不阻塞响应)
│                                                  │
│  评估 success_rate / fallback / latency          │
│  ├─ 差 → 记录失败 + 生成改进建议 ──▶ MemoryStore │
│  ├─ 好 → 记录成功路径 ─────────▶ MemoryStore   │
│  └─ 用户纠正 → 分析修正 ─────────▶ SkillMerger │
└──────────────────────────────────────────────────┘
       │
       ▼ (低优先级后台)
┌──────────────┐
│ SelfReflect  │  触发条件满足时运行
│ Loop         │──▶ 主动反思报告 + SkillPatch
└──────────────┘
```

---

## 5. 演进路线图

### Phase 1:MemoryStore + Critic(基础版,低风险)

**目标**:先让系统"记住",不急着"自我修改"。

| 组件 | 任务 | 文件 |
|------|------|------|
| MemoryStore | 实现基础存储,API 同 §3.2 | `core/memory.py` |
| ExecutionCritic | DAG 末尾评估 + 记录 | `core/critic.py` |
| Manager 集成 | 规划前读 hints | `agents/manager.py` |
| 测试 | 端到端验证失败记录被记住 | `tests/test_memory.py` |

**Feature Flag**: `SELF_EVOLUTION_ENABLED=false`(默认关)

---

### Phase 2:SkillMerger + 手动审核(中风险)

**目标**:允许自我改进,但有审核门槛。

| 组件 | 任务 | 文件 |
|------|------|------|
| SkillMerger | LLM 版本合并 | `core/merger.py` |
| Pending 队列 | 待审阅 SkillPatch UI | `frontend/src/components/PatchReview.vue` |
| 自动审核 | 低风险 Patch 自动生效 | `core/critic.py` |

**自动审核策略**:

```
confidence > 0.9  → 直接生效
confidence 0.7-0.9 → 发前端审核
confidence < 0.7   → 丢弃,只存 MemoryStore
```

---

### Phase 3:SelfReflectLoop + 开放自我修改(高风险)

**目标**:Agent 能主动反思并修改自身行为。

| 组件 | 任务 | 文件 |
|------|------|------|
| SelfReflectLoop | 定时复盘 | `core/reflect.py` |
| Prompt 自适应 | Agent 调整自己的 system_prompt | `agents/base.py` |
| 进化仪表盘 | 前端展示进化状态 | `frontend/src/components/EvolutionDashboard.vue` |

**安全护栏**:
- 禁止修改 `SKILL_DAG_ENABLED` / `TOOL_CACHE_ENABLED` 等系统级 flag
- Skill 修改需有 `source="merged"` 标记,供人工审计
- 单日最多自动合并 3 个 Skill 版本

---

## 6. 兼容性设计

**Feature Flag**: `SELF_EVOLUTION_ENABLED`

| 值 | 行为 |
|----|------|
| `false`(默认) | 当前行为完全不变,无额外开销 |
| `"critic_only"` | 仅 ExecutionCritic 评估,不生成改进建议 |
| `true` | 完整自我进化(含 MemoryStore,Critic,Merger) |

**向后兼容**:
- `Skill.source` 新增 `"merged"` 值,不影响现有 `"builtin"/"taught"/"imported"`
- `MemoryStore` 独立目录 `memory/`,不影响现有 `skills/` 和 `logs/`
- ExecutionCritic 完全异步,不影响 `Agent.handle()` 的返回时间

---

## 7. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| LLM 幻觉导致错误 Skill 被写入 | 中 | 高 | Phase 1/2 全部走手动审核或高置信度阈值 |
| 自我修改导致系统不可用 | 低 | 极高 | 禁止修改系统 flag;有 Skill 回滚机制 |
| MemoryStore 无限膨胀 | 中 | 低 | 容量控制 + 按月淘汰策略 |
| 反思循环占用过多 LLM 调用 | 中 | 中 | 低负载时才触发;有调用频率限制 |

---

## 8. 实施优先级建议

| 阶段 | 价值 | 风险 | 建议 |
|------|------|------|------|
| Phase 1 | 高 | 低 | **立即开始**,只记录不修改,稳赚不赔 |
| Phase 2 | 高 | 中 | Phase 1 上线后 2 周 |
| Phase 3 | 中(卖点) | 高 | 慎推,必须有充分人工审核机制 |

**最小可行版本(MVP)**:Phase 1 的 MemoryStore + ExecutionCritic(只记录,不修改),Feature Flag 默认关闭。
