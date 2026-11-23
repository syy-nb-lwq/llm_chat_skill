# 记忆分层与 RAG 召回方案

> 状态：P0 / P1 / P2 / P4 / P5(后端) 已落地（2026-07-22），P3 统一存储层未做
> 范围：`core/context.py`、`core/memory.py`、`core/memory_db.py`、`core/semantic_memory.py`、`core/memory_writer.py`（新）
> 关联任务：`docs/11` M2-01 ~ M2-09；目标架构 `docs/archive/10 §4.3`、`§5.4`、`§6.2`、`§8`

---

## 1. 现状诊断

### 1.1 现在有 4 套互不相通的存储

| 存储 | 位置 | 形态 | 装什么 | 生命周期 |
|---|---|---|---|---|
| 会话上下文 | `core/context.py::Context` | 进程内存 `List[Message]` | 多轮对话原文 | **进程重启即失** |
| 失败/成功记录 | `core/memory.py::MemoryStore` | JSON 文件 + JSONL | 执行诊断、成功路径 | 落盘，按月归档 |
| 语义记忆 | `core/memory_db.py::MemoryDB` | SQLite `memantic_memory.db` | `memories` 表 + FTS5 + BLOB 向量 | 落盘 |
| 教学会话 | `agents/teaching_session.py` | JSON 文件 | 教学状态机 | 落盘 |

业务代码直接读写这 4 种形态（`json.loads` / `sqlite3` / `Path.read_text` 混用），
换存储要动上层——这正是 M2-01 要解决的。

### 1.2 已验证的断点

| # | 问题 | 实测 |
|---|---|---|
| R1 | **生产主链从不写语义记忆** | 全仓 `add_memory()` / `add_sync()` 的调用点**只在测试里**。`agents/manager.py:462` 只有读（`search_context`），没有写 → `semantic_memory.db` 永远是 0 条 |
| R2 | **语义记忆默认关闭** | `infra/config.py:51` `semantic_memory_enabled: bool = False`，连读路径默认也不走 |
| R3 | **embedding 从不初始化** | `infra/embedding.py` 有 `init_embedding_service()`，但启动流程没调用，`SemanticMemoryStore.embedding_service` 恒为 `None` → 向量检索整条路径是死的 |
| R4 | **FTS 排序被拍平** | `core/semantic_memory.py:120` 把所有 FTS 命中的分数写成同一个常数 `fts_weight * 1.0`。`memory_db.search_fts()` 明明用 `ORDER BY rank` 算了 BM25 排序，**算完就扔** → 全文检索只贡献"命中/未命中"，不贡献相关性 |
| R5 | **召回无作用域过滤** | `search()` 的 `user_id` 参数是可选的，调用方 `manager.py:462` **根本没传** → 任何用户的记忆都会被召回 |
| R6 | **短期记忆不持久化** | `Context` 在 `Agent.__init__` 里 new 出来，只活在 `SessionManager` 的进程内字典里（TTL 1 小时）。超时/重启/多实例 → 全丢 |
| R7 | **上下文只截断不折叠** | `Context._truncate()` 超预算就丢早期消息，没有摘要 → 长会话丢失中段信息 |

### 1.3 已有资产（不要重写）

- `MemoryDB` 的 SQLite schema、FTS5 虚表、向量 BLOB 存储、**中文字段不转义**（M0-05 已修）
- `SemanticMemoryStore` 的混合检索骨架、用户维度索引 `idx_memories_user`
- `infra/embedding.py` 的三种 provider（openai / local / mock），mock 可离线跑
- `docs/archive/10 §8` 已经把分层记忆、写入策略、召回策略、遗忘纠错写清楚了

**缺的是"接线"和"统一模型"，不是底层组件。**

---

## 2. 目标设计

### 2.1 五层记忆

| 层 | 内容 | 存储 | 生命周期 | 召回方式 | 现状 |
|---|---|---|---|---|---|
| L0 工作记忆 | 本 turn 的规划、工具结果、教学草稿 | 内存（`Context` 之外） | 1 turn | 直接读 | 有 |
| L1 **短期记忆 = 会话内容** | 用户与助手的完整对话流（原文全留） | SQLite `session_messages` | **长期保留，不按时间淘汰** | `session_id` 直查 | **缺** |
| L2 **长期记忆 = 用户画像** | 用户的习惯、偏好、处理事情的方法 | SQLite `memories`，`type=habit/preference/method` | 长期 | 结构化过滤 + 混合检索 | 半有 |
| L3 执行记录 | 历史任务、执行结果、失败诊断 | SQLite `memories` + `core/memory.py` JSON | 可配 | 给 critic/reflect 用 | 有 |
| L4 程序记忆 | 已发布技能与工具 | YAML + Registry | 版本化长期 | 技能/工具匹配 | 有 |

**分层的依据是"记的是什么"，不是"存多久"**：

- **L1 短期记忆 = 对话内的内容**。用户和助手说过的话，**全部保留**。
  这样用户过一会儿回来还能接着问("上次我们说的那个方案后来怎么样了")。
- **L2 长期记忆 = 用户是谁、怎么做事**。用户习惯用什么格式、遇事怎么处理、
  有哪些雷区 —— 这些在对话之外也持续生效，跨会话累积。

**关键区分：存储 ≠ 注入。**
L1 全部保留，但每次调 LLM 注入的上下文仍然只是「摘要 + 最近 N 轮」——
这是 **token 预算**问题，不是删除。历史轮次永远在库里，需要时再检索出来。

**第三类 L3 是系统运行数据**（执行记录、失败诊断），不是"用户记忆"，
它服务于 critic/reflect 的自进化闭环，不参与对话召回。

### 2.2 统一记忆模型（`MemoryItem`）

现在有 `MemoryEntry`（`core/memory_db.py`）和 `FailureRecord`/`SuccessRecord`（`core/memory.py`）
两套模型，字段不重叠。统一成一个：

```python
@dataclass
class MemoryItem:
    id: str
    tenant_id: str            # 租户（多实例/多组织隔离）
    user_id: str              # 用户（强制隔离维度）
    session_id: str           # 来源会话
    scope: str                # session | user | project | global
    project_id: str = ""      # scope=project 时有效
    type: str                 # episode | preference | fact | project_rule | failure | correction
    content: str              # 可读的原子记忆正文
    source_turn_id: str       # 溯源到具体一轮对话
    source_execution_id: str  # 溯源到具体一次执行
    confidence: float = 0.5   # 0~1
    importance: float = 0.5   # 0~1，用于排序与保底保留
    valid_until: str = ""     # 空 = 永不过期
    supersedes_id: str = ""   # 被本条取代的旧记忆
    status: str = "active"    # active | superseded | deleted | expired
    tags: list[str] = field(default_factory=list)
    created_at: str = ...
    updated_at: str = ...
```

字段与 `docs/archive/10 §6.2` 对齐，额外补了 `importance` 和 `source_execution_id`。
`MemoryEntry` 保留为 sqlite 层的读写 DTO，由 Repository 做双向转换。

### 2.3 统一存储层（M2-01）

```
core/memory_repository.py（新增）
  class MemoryRepository(Protocol):
      # 短期
      def append_message(session_id, user_id, role, content, turn_id) -> str
      def get_messages(session_id, limit) -> list[MessageRow]
      def summarize_session(session_id, upto_turn) -> str
      def rollup_session(session_id, keep_recent=N) -> int
      # 长期
      def add(item: MemoryItem) -> str
      def get(id) -> MemoryItem | None
      def update(item) -> bool
      def delete(id) -> bool
      def search(query, *, tenant_id, user_id, types, scope, limit) -> list[(MemoryItem, score)]
      def supersede(old_id, new_id) -> bool
      def cleanup(now) -> int

  class SqliteMemoryRepository: ...   # 主实现
```

上层（`Agent` / `ManagerAgent` / `ExecutionCritic` / `ReflectLoop`）只依赖这个接口，
不再直接 `json.loads` / `sqlite3`。

**迁移策略**：不一次性重写。
1. `MemoryStore`（JSON）先包一层 `JsonMemoryRepository` 适配器，接口对齐
2. `MemoryDB`（SQLite）实现 `SqliteMemoryRepository`
3. 业务代码只依赖接口，通过配置切换
4. 数据迁移脚本：`memory/failures/*.json` → `memories` 表，`type=failure`

> **实际执行结果（2026-11-20，见 `docs/20`）**：第 1、3 步没有做 ——
> 分仓适配器 + 配置开关会让"到底读的哪个库"变成一个运行时问题，
> 比现状更难排查。第 4 步改为：`MemoryStore` 自身换成 SQLite 载体
> （`experience` 表，与画像 `memories` 同库不同表），历史 JSON 用
> `scripts/migrate_experience_to_sqlite.py` 一次性导入。
> 接口没变，所以 critic / API / manager 的调用点零改动。

### 2.4 写入管线（把语义记忆接到主链）

这是 **R1（最大断点）**。按 `docs/archive/10 §8.2`：

```
一轮对话结束 (Agent.handle 返回)
  │
  ├─ 1. 提取候选
  │     ├─ 规则候选：用户纠正语（"不对/不是/应该是"）、显式偏好（"以后都"/"我喜欢"）、
  │     │             任务结果摘要（工具执行成功/失败 + 结论）
  │     └─ LLM 候选：可选，一次调用产出 0~3 条候选（低频触发：仅在任务类 turn）
  │
  ├─ 2. 敏感信息检查
  │     身份证/手机号/银行卡/密钥/API Key/密码 → 拒绝或脱敏
  │     （正则 + 关键词 + 熵值启发）
  │
  ├─ 3. 去重与冲突检测
  │     同 scope + 同 normalized_key → 冲突
  │     规则：confidence 高者胜；同分则新者胜，旧者 supersedes
  │
  ├─ 4. 置信度与有效期判断
  │     confidence < 0.6 → 不入库
  │     明确标注"以后/每次" → valid_until = "" (永久)
  │     明确标注"这次/暂时" → session 作用域
  │
  ├─ 5. 自动保存 or 请求确认
  │     低风险事实（preference/fact）→ 自动保存
  │     高风险/低置信 → 写入 pending 区，等用户确认
  │
  └─ 6. 建索引
        content → embedding（失败降级为无向量，FTS 仍可用）
        同时写 FTS5 虚表
```

**写入必须异步且不阻塞响应**：`Agent.handle()` 末尾 `asyncio.create_task(...)`，
失败只记日志，不影响本轮响应。

### 2.5 召回管线（RAG）

这是 **R4 / R5** 的修复点。完整链路：

```
用户输入
  │
  ├─ ① 查询改写 (Query Rewrite)
  │     用会话上下文把指代变成独立查询：
  │     "上次那个项目怎么样" + 上下文 → "skill_agent 项目的发布流程"
  │     产出 1~2 个检索 query（原始 + 改写）
  │
  ├─ ② 作用域硬过滤（打分之前！）
  │     tenant_id = 当前租户（必填）
  │     user_id   = 当前用户（必填，跨用户绝不召回）
  │     scope     ∈ {user, project}（session 级记忆走 L1 直查，不进 RAG）
  │
  ├─ ③ 并行召回
  │     向量通道：embedding(query) → 余弦相似度 top-20
  │     全文通道：FTS5 MATCH → BM25 top-20
  │
  ├─ ④ RRF 融合（修 R4）
  │     score(d) = Σ_channels 1 / (k + rank_channel(d))     k = 60
  │     RRF 只用排名不用分数，天然规避"向量分和 BM25 分量纲不同"的问题
  │
  ├─ ⑤ 重排 (Rerank)
  │     top-20 → top-5
  │     可选：LLM rerank（"给定 query 和候选记忆，选出最相关的 5 条"）
  │     轻量：按 相关性 × confidence × 时效 × 重要度 加权
  │
  ├─ ⑥ 组装注入
  │     预算：≤ 800 token / ≤ 5 条
  │     标注来源（可追溯）：[记忆:session-2026-07-22#turn-3]
  │     与 L1 会话记忆去重（短期已有的一律不重复注入）
  │
  └─ ⑦ 注入 prompt
        Manager._enrich_with_hints() 现在的挂载点就是对的，替换实现即可
```

**RRF 的好处**：直接解决 R4。现在"向量分×0.7 + BM25×0.3"这种加权求和需要先做分数归一化，
否则向量分（0~1）和 BM25（负数或任意值）根本不可比；而 RRF 只看名次，稳定且无参数敏感。

**作用域过滤必须在打分之前**（R5）。现在 `user_id` 是可选参数且调用方不传，
等于没隔离——这是多用户场景下的**数据泄漏**，不只是"召回不准"。

### 2.6 切分策略

对话记忆**不按字符切**，按**语义单元**切：

| 类型 | 切分单位 | 示例 |
|---|---|---|
| `episode` | 一次完整任务执行 | "用户问北京天气 → 调用 weather_query → 返回晴 25℃" |
| `preference` | 一条原子偏好 | "用户偏好中文回答" |
| `fact` | 一条原子事实 | "用户公司在上海" |
| `correction` | 一条纠正 | "日报要用三段式，不用要点式" |

原则：**一条 MemoryItem = 一个可独立成立的陈述**。混在一起的记忆无法去重、无法冲突检测、
无法单独删除。

### 2.7 短期记忆（L1 = 会话内容）的具体做法

**原则：会话内容全部保留，不按时间淘汰、不删除。**

```sql
CREATE TABLE session_messages (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    turn_id      TEXT NOT NULL,
    role         TEXT NOT NULL,       -- user / assistant / tool / system
    content      TEXT NOT NULL,
    tokens       INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_sess_msg ON session_messages(session_id, turn_id);
CREATE INDEX idx_sess_msg_user ON session_messages(user_id, created_at);

CREATE TABLE session_summaries (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    cover_until_turn TEXT NOT NULL,   -- 摘要覆盖到哪一轮
    summary      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
```

**存储全留，注入有预算** —— 这两件事必须分开：

| | 行为 |
|---|---|
| **存储** | 每一条消息都写进 `session_messages`，永不删除、不按天淘汰 |
| **注入** | 每次调 LLM 只注入「滚动摘要 + 最近 N 轮原文」——这是 token 预算，不是删除 |

组装上下文：

```
[system]
+ session_summaries 里最新的一条（滚动摘要，覆盖早期轮次）
+ 最近 N 轮原文（N 默认 8，或按 token 预算动态调整）
+ 本轮用户输入
```

**滚动摘要**：当 session 消息数超过阈值（如 24 轮），把最早的若干轮压缩成一条摘要
（一次 LLM 调用）。**原始消息仍然留在 `session_messages` 里**，摘要只是给 LLM 用的
一个"压缩视图"。这解决 R7——现在的纯截断会丢中段信息。

**"回来继续问答"怎么实现**：

| 用户问 | 走哪条路 |
|---|---|
| "刚才那个数字是多少" | L1 直查最近几轮（精确、快） |
| "上次我们说的那个方案后来怎么样了" | L1 会话内检索：在本 session 的历史里搜 |
| "我一般习惯怎么处理这类事" | L2 用户画像召回 |

所以 L1 除了"按顺序取最近 N 轮"，还需要**会话内检索**能力。

#### 实现说明（2026-07-22 已完成）

| 文件 | 内容 |
|---|---|
| `core/session_memory.py` | `SessionMemoryStore`：消息读写、摘要、会话内检索。库文件 `memory/sessions.db`（与长期记忆的 `semantic_memory.db` 物理隔离） |
| `core/session_config.py` | 运行参数：恢复条数 / 滚动阈值 / 保留条数 |
| `core/session_rollup.py` | 滚动摘要：覆盖范围用 `context.metadata['rollup_index']` 记录，增量进行不重复摘要 |
| `core/context.py` | 新增 `sink`（持久化回调）与 `summary`（滚动摘要）字段；`summary_block()` 给 LLM 用 |
| `core/agent.py` | `persist_session=True` 时挂上 sink、建 Agent 时恢复历史、`handle()` 开头触发滚动摘要、`reset()` 标记工作上下文起点 |
| `backend/session.py` | `issue_identity(conn_id, user_token, resume_session_id)`：会话**必须属于该用户**才复用 |
| `backend/websocket_handler.py` | `init` 透传 `session_id` |
| `frontend/src/websocket.js` | init 时回传本地存的 `session_id`，`connected` 后更新 |
| `agents/manager.py` | `_search_session_history()`：指代型提问才检索会话历史，避免噪声 |
| `backend/main.py` | `GET /api/sessions`、`GET /api/sessions/{id}/messages`、`DELETE /api/sessions/{id}`（均需 owner token） |

**实际表结构**（比上面的草案多了 `sessions` 表与 `seq` / `reset_at_seq`）：

```sql
CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL DEFAULT 'default',
    user_id       TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    reset_at_seq  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

`seq` 是会话内单调递增序号（摘要覆盖范围与排序都靠它）；
`reset_at_seq` 记录工作上下文的起点。

**检索为什么不用 FTS5**：SQLite 的 `unicode61` 分词器把整句中文当作一个 token，
`trigram` 又要求查询串 ≥3 字符 —— 都不适合中文短词检索（"北京"、"天气"）。
因此会话内检索走 **CJK 二元组打分**：把查询切成 ASCII 词 + CJK 二元组，
按命中比例排序。规模到十万级再换 `sqlite-vec` / 专用分词器。

**reset 语义**：`reset` 清空的是**工作上下文**，不是存储。
消息仍在库里、可被检索；只是恢复会话时不再把 reset 之前的内容装回上下文
（`sessions.reset_at_seq` 记录起点）。

**界面必须跟着过滤**：一开始只有 Agent 侧按 `reset_at_seq` 跳过，
前端恢复历史用的接口没有 —— 结果是点「新对话」后界面清空、一刷新旧对话全回来，
而模型其实什么都不记得了。用户接着问"刚才那个"，模型一脸茫然。
现在 `GET /api/session/messages`（前端恢复用）按 reset 点过滤，
`GET /api/sessions/{sid}/messages`（管理员看待档案）不过滤 —— 两个口径，
用途不同，别混用。

过滤要写在 SQL 里（`get_messages(from_seq=...)`）：先 `LIMIT` 再在 Python 里筛，
reset 前消息一多就会把新消息挤出窗口，表现成"重置后新说的话也看不见了"。

**两种"从头开始"不是一回事**（2026-11-20 起）：

| 动作 | 做什么 | 结果 |
|---|---|---|
| 「新对话」（输入框旁 `＋` / 侧栏列表顶部按钮） | 对同一连接重发 `init` 且**不带** `session_id`，服务端新签发一个 | 列表里多一条会话；旧会话完整保留；新会话的 `reset_at_seq` 从 0 开始 |
| `reset/{conn}`（协议层，UI 已不再挂按钮） | 在**同一个** `session_id` 上把 `reset_at_seq` 推到当前 `message_count` | 库里还是同一条会话，只是之前的消息不再装回上下文 |

以前界面上只有一个 `↺`，做的是后者 —— 用户点完发现列表毫无变化（当时还没有列表），
上下文却真的没了，于是问"新对话在哪儿建"。现在 `↺` 换成了 `＋`，做的是前者；
`reset` 这条路径留给脚本与测试（口径由 `tests/test_session_api.py` 钉住）。

**保留策略上的取舍**：全部保留意味着库会一直涨。缓解手段：
- 原始消息不生成 embedding（省 embedding 成本），只对**轮次摘要**和**长期记忆**做向量化
- 可选的冷存储归档（90 天后把原文挪到文件）——**默认不启用**，因为"都要保留"是硬要求

### 2.8 长期记忆（L2 = 用户画像）的具体做法
**定义：用户是谁、怎么做事。** 具体包括：

| 子类 | 示例 | 来源 |
|---|---|---|
| `preference` 输出偏好 | "回答用中文"、"先给结论再展开" | 用户显式要求 / 反复纠正 |
| `habit` 习惯 | "习惯每天早上先看日报" | 多次会话反复出现的行为 |
| `method` 方法论 | "做数据分析先看缺失值再画图" | 用户教导 / 纠正 / 示范 |
| `constraint` 雷区 | "不要用 emoji"、"别写超过 300 字" | 用户明确禁止 |
| `fact` 稳定事实 | "用户是做数据分析的" | 明确陈述 |

**与技能的区别**：技能是**可复用的方法论**（`skills/` 目录，版本化，可被检索执行），
长期记忆是**这个用户的个人偏好与习惯**（`memories` 表，作用在对话风格与取舍上）。
两者会互相印证：用户说"以后按三段式写日报"，既是一条 `method` 记忆，
也可能对应一条要沉淀的技能 —— 这正是 `SkillTrainer` 的输入。

**提取策略**（低频高准，宁缺毋滥）：

```
每轮结束后
  ├─ 规则候选（高准）：
  │     "以后…就…" / "记住…" / "不要…" / "别…" / "我习惯…" / "我一般…"
  │     → 直接进候选
  ├─ LLM 候选（低频触发）：
  │     仅在"用户纠正了助手"或"用户给了方法论示范"时调用一次，
  │     让 LLM 产出 0~2 条候选（类型 + 陈述 + 置信度）
  └─ 冲突检测：同类型 + 同主题 → 高置信者胜，旧者 supersedes
```

**召回策略（与 RAG 的"按需召回"不同）**：

用户画像描述的是"用户是谁"，它**每轮都该在场**（常驻注入），而不是等 query 命中才召回：

```
[system]
+ 用户画像（常驻）：最多 ~20 条，≤400 token
  （按 relevance × confidence × importance 排序，取 top-N）
+ 会话内容（短期记忆）：摘要 + 最近 N 轮
+ 本轮用户输入
```

只有"事件型记忆"（如"上周三他说要用 A 方案"）才走按需 RAG 召回。
这样既保证"用户画像始终生效"，又不让 token 爆掉。

### 2.9 遗忘与纠错（M2-08）

用户必须能控制自己的记忆：

| 用户说 | 系统动作 |
|---|---|
| "你记住了我什么" | 列出该 user 的所有 active 记忆（含来源轮次） |
| "忘掉这件事" | 指定 id → `status=deleted` + **同步删除向量与 FTS 索引**（RAG 缓存也要失效） |
| "这个偏好已经变了" | 新记忆 `supersedes_id=旧id`，旧记忆 `status=superseded`（保留可追溯） |
| "仅在这个项目用" | `scope=project`, `project_id=当前项目` |

**删除必须同步清理索引**——否则删掉的记忆还会被向量检索召回（`docs/archive/10 §8.4` 明确要求）。

### 2.10 实现说明（P0 / P1 / P4 / P5，2026-07-22 已完成）

| 文件 | 内容 |
|---|---|
| `core/memory_writer.py`（新） | 画像写入管线：规则候选提取 → 敏感检查 → 去重/冲突 → 置信门槛 → 入库 |
| `core/memory_db.py`（改） | 加 `tenant_id/scope/confidence/importance/status/supersedes_id/source_session_id/source_turn_id/normalized_key` 列 + 老库自动补列；`search_fts` 回传 BM25 分；两个检索通道都支持 `user_id` 过滤；新增 `get_by_key` / `supersede` / `soft_delete` / `list_memories` / **`search_cjk`** |
| `core/semantic_memory.py`（改） | 混合检索改 **RRF**；`search(user_id=...)` 必填；新增 `get_profile` / `build_profile_block` / `forget`；**合成向量拒绝启用语义通道** |
| `core/text_match.py`（新） | CJK 二元组切分与命中打分（原本只在会话检索里用） |
| `core/sqlite_util.py`（新） | SQLite 连接工厂：`journal_mode=WAL` + `busy_timeout=10000` |
| `agents/manager.py`（改） | `profile_block()` 公开化 + 常驻注入 L2 规划提示词 |
| `agents/orchestrator.py`（改） | **画像进 system prompt**（回答生成侧也吃画像） |
| `core/agent.py`（改） | `persist_memory` 开关 + `_schedule_memory_write()`（`asyncio.create_task`，不阻塞本轮）+ `_get_profile_block()`（每轮取一次）+ trace 埋点 |
| `core/trace.py`（新） | 分阶段耗时记录（JSONL） |
| `backend/main.py`（改） | 启动初始化 embedding（未配 provider 则只走 FTS）；`GET /api/memories`、`DELETE /api/memories/{id}`、`GET /api/traces` |
| `infra/embedding.py`（改） | `is_synthetic` 标记（`MockEmbedding` 为合成向量） |
| `infra/config.py`（改） | `semantic_memory_enabled` 默认 **True**；`embedding_provider` 默认 **空**（= 不启用向量检索） |

#### 2026-07-22 追加：检索链路的两个致命断点

评审（见 [14-架构评审与改造建议.md](./archive/14-架构评审与改造建议.md)）时挖出两个
**会静默坏掉**的断点，都在检索链路上：

**① 合成向量当语义用 —— 检索比不检索更差。**
`MockEmbedding` 是按文本哈希播种的随机向量，实测**任意两句的余弦相似度 = 0.8132**，
比真实模型的相关句还高。所以"调高阈值"根本无解（放过高了噪声、压低了真命中）。
修法是**类型级拒绝**：`BaseEmbeddingService.is_synthetic` 标记 +
`set_embedding_service()` 拒绝合成向量 + `config.embedding_provider` 默认留空。

**② FTS5 切不开中文 —— 修好 ① 之后中文检索结果为空。**
`unicode61` 把连续汉字当成一个 token，「查询北京天气的记录」和「北京天气」永远匹配不上
（`trigram` 要求 ≥3 字符，两字中文同样无解）。
这意味着关掉噪声向量通道后，**唯一剩下的通道也是死的** ——
长期记忆写进去就再也读不出来。
修法：加一条 **CJK 通道**（`core/text_match.py` 二元组打分），
和 FTS 通道一起进 RRF 融合。

> **教训**：关闭一个检索通道之前，必须先确认另一个通道**真的能用**。
> 两个通道都是"看起来在"，实际都死的情况，静默性最强。

#### 三个刻意的取舍

**① 只写画像，不写 episode。**
会话内容（L1）已经全量落库并可检索，再为每轮存一条"执行记录"记忆是**重复存储**。
长期记忆的价值在"蒸馏出这个人是谁"，所以写路径只产出可独立成立的画像陈述。

**② 冲突检测按「单槽维度」而不是字面。**
"以后回答都用中文" 和 "以后回答都用英文" 字面完全不同，但说的是同一件事的两个版本，
必须让新的取代旧的，否则画像里会躺着两条互相矛盾的指令。
做法：对**能枚举清楚**的维度（语言 / 格式 / 长度 / 表情）归并到同一个 key；
归不上的（"不要用括号" vs "不要用分号"）退回字面骨架 —— 它们是不同的约束，混成一个槽会丢信息。

**③ 作用域不许放大。**（2026-11-20）

用户在前文讨论 minimax（有国内站 / 海外站）时说「我以后都不要用国际站，只用国内站」，
被抽成了：

```
constraint: 用户以后不要使用国际站，只使用国内站    conf=0.95
```

几天后用户说「我要做电商」，助手拿这条去否掉了跨境电商方案。
用户从没说过"不做跨境"，他说的是**某个 AI 服务商用哪个站**。

这是三层问题叠出来的：

| 层 | 问题 |
|---|---|
| 抽取 | 只给了**当前这一句**。"国际站"的主语（minimax）在更早的轮次里，模型只能猜 |
| 校验 | 证据校验只保证"原话里有这段"，**防不了放大** —— 原话确实写着"不要用国际站"，校验照过 |
| 注入 | 所有条目都挂在 `[用户画像](请始终遵守)` 下面，`constraint` 还标成「雷区」。一条场景偏好被升级成全局禁令 |

修法（四层一起改，缺一层都不成立）：

1. **抽取带上下文**：`record_turn(context=...)` 收最近 6 条对白（`Context.history_messages`
   去掉当前句）。上下文**只用于消解指代** —— evidence 仍然只能来自当前句，
   `validate_facts` 里 `haystack` 和 `context_haystack` 是两个变量，不许混。
2. **新增 `applies_to`**：这条只适用于哪个具体产品 / 平台 / 项目。填了就必须在
   （当前句 + 上下文）里逐字出现过，否则整条丢弃 —— 编一个不存在的平台名比不填更危险。
   提示词里把"限定不许丢"放在第一条硬规则，并写明「国际站/国内站是平台站群说法，
   **不等于**跨境电商」。
3. **`content` 自带限定**：写「在 minimax 上优先使用国内站」，不写「不要使用国际站」。
   这是最便宜也最稳的一道防线 —— 即使过滤失效，模型读到的句子本身也是对的。
4. **注入按话题过滤**：`build_profile_block(query=...)` 把条目分两段，
   限定的那条只有 `query` 里出现 `applies_to` 才注入，**不相关就完全不出现**。
   不出现比"出现了但标注适用条件"可靠 —— 模型很可能忽略标注。
   `query` 为空（澄清分支等）时只注入全局条目。

```
query="我要做电商，怎么选品"
  [用户画像](长期记忆)
  - [事实] 用户是做横屏短剧的。
  - [事实] 用户想做电商。          ← 没有国际站那条

query="minimax 的接口怎么调"
  [用户画像](长期记忆)
  - [事实] 用户是做横屏短剧的。
  [当前话题相关](只在本话题成立，不要推广到其他场景)
  - [偏好] 在 minimax 上优先使用国内站
```

配套清理：真实库里 2 条脏数据（上文那条 + 一条旧规则版的碎片
`不要用国际站的  然后 你可以开始生成视频了`）。前者改写为带限定的陈述并登记
`applies_to`，后者 `soft_delete`。备份在 `memory/semantic_memory.db.bak-20261120-1215`。

**残留风险**：`applies_to` 是否填对仍依赖模型判断。兜底是第 3 条（`content` 自带限定）
—— 即使模型漏填，读到的句子也不是全局禁令。这一层的量化要等
`tests/data/profile_eval.jsonl` 补上"限定场景"样例后再做。

**④ 沉淀必须"综合来看"，不是逐句抠。**（2026-11-20）

用户原话：「记忆的总结不要是单独记住一句话，而是说要综合来看才能沉淀。」

旧路径是**每轮调一次 LLM**，输入是「当前这一句 + 前 6 轮（仅供消解指代）」，
而证据被要求逐字出现在**当前这一句**里。这个设计有两个结构性后果：

- **一句话就足以生成一条长期结论**。「这次先这样」只要不含临时词就能变成偏好；
  上下文再努力也只能帮它猜对主语，不能否掉它
- **多轮才看得出的模式反而沉淀不下来**。三轮都在要求"短一点"、两次都在纠正
  同一个错 —— 没有任何单句能支撑这条结论，于是它永远不会入库

现在的分工：

| 时机 | 走哪条路 |
|---|---|
| 用户明说「记住…」 | 立即 `record_turn`（结论的合法性就来自这句话，不该让他等） |
| 其他每一轮 | 进 `DigestWindow` 累积，**攒满 6 轮**再整体归纳 |
| reset / 清空上下文 | `flush()` 把剩下的轮次也交出去（之后它们不会再参与任何归纳） |
| 轮次没攒满、但已陈旧 | 下次对话时 `take_stale()` 先归纳掉旧批次（见下） |

**陈旧兜底（2026-11-23 补）**：`flush()` 只在用户主动 reset 时触发，`add()` 只在攒满
6 轮时触发。一个只聊 2~3 轮就走的会话（如"传报销表 → 加一行 → 记住这个方式"），
轮次会永远卡在窗口里：既不满 6 轮、用户也不点 reset —— 实机表现是"聊了出差报销
什么都没沉淀"。现在 `DigestWindow.take_stale(max_idle_s)` 在距最后一轮超过空闲阈值
（默认 15 分钟，`PROFILE_DIGEST_IDLE_S`）时交出整批归纳，由 `_maybe_persist_profile`
在每轮开头先调用。连续对话仍按 6 轮攒批，只有"聊完就走 / 隔一阵回来"才兜底。

归纳器 `core/profile_digest.py` 的判据从「这句话像不像长期结论」换成
**「这条结论有多少轮撑着」**：

- 证据是数组 `[{turn, quote}, ...]`，`quote` 必须在**它声明的那一轮**里逐字出现。
  不是在整个对话块里找 —— 否则模型能拿第 5 轮的话冒充第 1 轮，跨轮约束形同虚设
- `kind="inferred"`（从多次行为归纳的）**必须 ≥2 个不同轮次**的证据，凑不出就整条丢弃
- `kind="stated"`（用户自己说的身份 / 禁令）允许单轮 —— 这类结论本来就来自那一句话
- `content` 含「这次 / 刚才 / 这一版」直接丢弃：那是在描述状态，不是描述这个人
- 漏标 `kind` 按 `inferred` 处理 —— 不让"忘标"成为绕过跨轮校验的后门

实测（真实 LLM，5 轮对话 = 三轮反复要求简短 + 一句身份 + 一个临时参数）：

```
→ [preference] 用户希望回答简短，只给结论，不讲过程、不做解释。
   turns=[2, 3, 4]        ← 跨三轮归纳出来的
→ [fact] 用户从事横屏短剧制作，单集时长为 90 秒。
   turns=[4]              ← 单轮，kind=stated
（"这次先切 3 个镜头"没有被抽成偏好）
```

同一个归纳器喂单轮「帮我写个爬虫，这次先抓前十页就够了」→ **一条都不产出**。

**上传附件不进归纳**：用户传了 `Q3财报.xlsx` 时，消息正文尾部会带一段
`【已上传文件】- 名字 → 绝对路径`（给 `read_document` 用）。归纳前由
`strip_attachments()` 切掉 —— 否则一段传过财务表格的对话会被归纳成
"用户经常处理财务表格"，那是文件名说的，不是用户说的。
`build_transcript` 和 `validate_digest` **必须用同一个剥离版本**，
否则模型引用的原话在校验时对不上，整条结论会被误判成编造。

**已知边界（不假装是零风险）**：

- 「这次用 5 秒」出现两轮，证据形态上完全合法（跨轮、逐字对得上），
  但结论"偏好 5 秒"是错的。这层只能靠提示词 + `content` 临时词拦截
- 累积窗口是**纯内存**的：后端重启会丢掉"已攒但未满窗"的几轮。丢的是
  "少记"，不是"记错"（这些消息本身仍在 `session_messages` 里）
- 窗口缩到 6 轮是折中：聊两三句就走的会话不会沉淀任何画像。这正是"综合"的代价

#### 实测（本地假 OpenAI + 真实 WebSocket/HTTP）

```
1) 说一条偏好 "以后回答都用中文"
   事件: ['thinking', 'plan', 'thinking', 'message_delta', 'message_final']
2) GET /api/memories?user_id=... → count=1
   - [preference] 以后回答都用中文 (conf=0.9)
3) 下一轮 "帮我查一下北京天气" 的规划提示词里:
   ✓ 含 [用户画像] 且含刚写入的偏好
4) DELETE /api/memories/{id} → count=0
5) 再下一轮的规划提示词里已无 [用户画像] 块
```

追加验证（画像下沉到**回答生成**之后）：

```
3') 下一轮闲聊的 **system prompt** 里:
    你是一个专业的回答生成助手。
    …
    [用户画像](长期记忆,请始终遵守)
    - [偏好] 以后回答都用中文

    以上「用户画像」是这个用户长期稳定的偏好与习惯,生成回答时必须遵守…
```

CJK 检索实测：

```
修前: 查询"北京天气" → 0 条结果
修后: 查询"北京天气" → 查询北京天气的记录   ✓
      查询"回答语言" → 以后回答都用中文      ✓
      查询"数据分析师" → 我是数据分析师      ✓
```

#### 顺带修掉的一个意图误判

实测中发现 "以后回答都用中文" 被 L0 判成了 `teach`（用户只是表达说话方式偏好，
却被塞进技能学习流程，实测触发了 `teaching_interactive`）。

根因：教导规则里"约定信号 + 方法论载体"那条 `(以后|下次|之后|每次).{0,24}(按|用|走|遵循|照)`
把"以后都用中文"也吃进去了；而排除规则只在第二个分支生效，第一个分支直接 True。

修法：**排除项优先**（`TEACH_EXCLUDE_RE` 先判），并补上"说话方式偏好"的排除：

```python
r"|^(以后|今后|从现在起)?(回答|回复|输出|回复内容|输出内容)"
r"|(回答|回复|输出)(我)?(时|的时候)?(都|请|要|必须|一律)"
```

"以后写日报都按三段式来" 这类**做事方法**仍然判 `teach`（能被沉淀成技能），
只有"怎么回话"的偏好被排除。

---

## 3. 身份：假用户服务（已实现）

长期记忆要按用户隔离，前提是有一个稳定的"用户是谁"。项目没有登录体系，
所以用**假用户服务**补齐身份。

### 3.1 设计

```
客户端首次连接
  → init 消息带 user_token(可能为空)
  → 服务端 UserService.get_or_create(user_token)
      · 令牌有效 → 认出同一个用户
      · 令牌无效/为空 → 新建用户
  → 下发 connected 事件:user_id / session_id / client_id / token / user_token
  → 客户端把 user_token 存进 localStorage,下次连接带上
```

**用户令牌**（`infra/auth.py::issue_user_token` / `verify_user_token`）：
`base64url(payload).base64url(HMAC-SHA256)`，payload 含 `user_id` / `tenant_id`。
与**会话令牌**的区别：

| | 会话令牌 | 用户令牌 |
|---|---|---|
| 生命周期 | 一次 WS 连接 | 长期 |
| 绑定 | `user_id + session_id + client_id` | `user_id + tenant_id` |
| 用途 | 证明"这条连接是谁" | 证明"这个人还是那个人" |
| 过期 | 随连接断开失效 | 默认不过期 |

### 3.2 已实现的组件

| 文件 | 内容 |
|---|---|
| `core/user_service.py` | `User` 档案 + `UserService`（签发/解析令牌、增删查改档案），落盘在 `memory/users/<id>.json` |
| `infra/auth.py` | `issue_user_token()` / `verify_user_token()` |
| `backend/session.py` | `issue_identity(conn_id, user_token)` → 稳定 user_id；`ClientIdentity` 增加 `user_token` / `tenant_id` |
| `backend/websocket_handler.py` | `init` 透传客户端带来的 `user_token` |
| `backend/main.py` | `GET /api/users`（列出）、`POST /api/users`（新建 + 签发令牌）、`DELETE /api/users/{id}`（需 owner token） |
| `frontend/src/websocket.js` | `localStorage['skill_agent_user_token']` 持久化并在 init 时回传 |

### 3.3 修掉的一个真实缺陷

改造前，C-01 的 `issue_identity()` **每次 WS 连接都新生成一个 `user_id`**：

```python
identity = ClientIdentity(user_id=gen_user_id(), ...)   # 每次都是新的
```

浏览器刷新一次 = 新用户 → 长期记忆会按连接碎片化，用户画像永远积累不起来。
引入用户令牌后，**同一浏览器始终是同一个 `user_id`**，
`session_id` / `client_id` 仍然是每次连接新建（会话隔离不受影响）。

### 3.4 后续可演进

- `POST /api/users` 返回的 `user_token` 可以直接交给另一个浏览器/终端 → 模拟多用户，测隔离
- 将来接入真实登录时，只需把 `UserService.get_or_create()` 换成"从登录态取 user_id"，
  上层无感
- `tenant_id` 目前固定 `default`（单机个人环境），字段已预留

---

## 4. 落地改动清单

| 文件 | 改动 |
|---|---|
| `core/user_service.py` ✅ | 假用户服务（已实现） |
| `infra/auth.py` ✅ | 用户令牌签发/校验（已实现） |
| `backend/session.py` ✅ | `issue_identity` 接 user_token，稳定 user_id（已实现） |
| `backend/main.py` ✅ | `/api/users` 三个接口（已实现） |
| `frontend/src/websocket.js` ✅ | user_token 持久化与回传（已实现） |
| `core/session_memory.py` ✅（新） | `SessionMemoryStore`：会话内容全量存储 + 摘要 + 会话内检索（已实现） |
| `core/session_config.py` ✅（新） | 恢复条数 / 滚动阈值 / 保留条数（已实现） |
| `core/session_rollup.py` ✅（新） | 会话滚动摘要（原文仍保留）（已实现） |
| `core/context.py` ✅ | 新增 `sink` + `summary`（已实现） |
| `core/agent.py` ✅ | `persist_session` 开关、恢复历史、触发滚动摘要、`reset` 语义（已实现） |
| `backend/session.py` ✅ | `issue_identity` 支持 `resume_session_id` + 归属校验（已实现） |
| `backend/main.py` ✅ | `/api/sessions` 三个接口（已实现） |
| `core/memory_writer.py` ✅（新） | 用户画像写入管线（已实现） |
| `core/memory_db.py` ✅ | schema 扩展 + 老库补列 + BM25 分回传 + 作用域过滤 + `get_by_key`/`supersede`/`soft_delete`（已实现） |
| `core/semantic_memory.py` ✅ | 混合检索改 RRF；`user_id` 必填；`get_profile`/`build_profile_block`/`forget`（已实现） |
| `agents/manager.py` ✅ | 画像常驻注入 L2 规划提示词（已实现） |
| `core/agent.py` ✅ | `handle()` 里异步触发画像写入（已实现） |
| `backend/main.py` ✅ | 启动初始化 embedding；`/api/memories` 列表 + 删除（已实现） |
| `core/memory_models.py`（新） | `MemoryItem` / `MessageRow` / `SessionSummary` 数据模型 —— **未做**，直接用了 `MemoryEntry` |
| `core/memory_repository.py`（新） | `MemoryRepository` 协议 + 实现 —— **未做**（P3） |
| `frontend` | 记忆管理面板（"你记住了我什么"）—— **未做**，目前只有后端 API |

---

## 5. 分阶段推进

| 阶段 | 内容 | 验收 | 风险 |
|---|---|---|---|
| **P0** ✅ | 接线：启动初始化 embedding + `SemanticMemoryStore` 绑定 + `semantic_memory_enabled` 默认打开 + **主链写入**（画像类） | `semantic_memory.db` 条数 > 0 | 低 |
| **P1** ✅ | 修召回：RRF 融合 + 作用域过滤必填 + BM25 分回传 | 跨用户召回为 0 | 中（改了排序，老的 hint 排序会变） |
| **P2** ✅ | 短期记忆持久化：`session_messages`（**全量保留**）+ 上下文组装改为「摘要 + 最近 N 轮」 | 重启后同一 session 能续上；历史轮次仍可检索（M2-09 用例 1） | 中 |
| **P3** | 统一存储层：Repository 协议 + 三个实现 + 数据迁移 | 上层不再直接读写 JSON/SQLite（M2-01） | 高（改动面大） |
| **P4** ✅ | 长期记忆（用户画像）写入管线：规则候选、敏感检查、冲突检测、置信门槛 | 画像准确率与冲突处理可验证 | 中 |
| **P5** ✅（后端） | 用户可控：`/api/memories` 列表 + 删除同步清索引；**前端面板未做** | 删除后不再召回（M2-09 用例 3） | 低 |

**P0 是最关键的一步**：它把整套 RAG 从"单元测试里的独立组件"变成"Agent 真正在用的记忆"。
没有写入，检索再好也是空的。

**已完成的部分**：P0 / P1 / P2 / P4 / P5(后端)。
短期记忆（会话内容）与长期记忆（用户画像）两条链现在都通了：

| 链路 | 状态 |
|---|---|
| 会话内容全量落库 → 重启/重连续聊 → 会话内检索 | ✅ P2 |
| 用户说偏好/习惯/方法 → **LLM 抽取（证据校验）** → 去重/冲突 → 入库 → **下一轮常驻注入** | ✅ P0/P4 |
| 混合检索（向量 + FTS）→ RRF 融合 → 强制 user_id 过滤 | ✅ P1 |
| 「你记住了我什么」/「忘掉这件事」 | ✅ P5 后端 API；前端面板待做 |

**P3（统一 Repository 层）仍未做** —— 现在 `MemoryStore`(JSON) 和 `MemoryDB`(SQLite) 还是两套，
上层仍能直接摸到 sqlite。这不影响功能，但换存储要动上层。触发条件：接入第二个存储后端时。

---

## 6. 成本与风险

| 项 | 影响 |
|---|---|
| embedding 调用 | 每次写入 1 次；可批量 + 只对入库条目生成；**会话原文不向量化**（只向量化摘要和长期记忆） |
| sqlite 向量检索 | 暴力余弦，1 万条内 < 50ms；超过 10 万条需迁 `sqlite-vec` / Qdrant（给出触发条件） |
| 写入在主链上 | 必须异步 + 失败降级，绝不能拖慢或搞崩响应 |
| 作用域过滤 | 从"可选参数"变"必填参数"，会打破所有现有调用点——这是**故意的**，宁可编译期报错也不要静默泄漏 |
| 会话内容全量保留 | 库会一直涨。缓解：原文不向量化（省 embedding 成本）；可选的冷存储归档**默认不启用** |
| `MemoryStore` JSON → SQLite 迁移 | 历史失败记录要迁；不迁则两套并存，检索时合并 |
| 抽取的精确率 | 改用 LLM 后召回大幅提升，代价是可能编造。**结构性防护**：模型必须给出 evidence，且 evidence 必须能在用户原话里逐字找到，否则整条丢弃 |
| 画像块占 token | 每轮固定 ~400 字符。相比整段对话历史这是小头，但会话很长时要留意总预算 |

---

## 7. 明确不做的事

- **不做**多向量库（pinecone/qdrant）——当前规模 sqlite + numpy 足够，等触发条件再迁
- **不做**知识图谱——关系型记忆的收益要等画像条目稳定后再评估
- **不做**把会话原文塞进长期记忆——L1 存全文，L2 只存蒸馏后的**用户画像**条目
- **不做**给会话内容设过期时间——"会话内容都要保留"是硬要求（可选的冷存储归档默认关闭）
- **不做**让 LLM 直接决定"要不要记这条"——先规则 + 可选 LLM 候选，人工确认区兜底
- **不做**跨 embedding 模型的向量兼容（换模型要重建索引，写进运维文档即可）
- **不做**给每轮对话写 episode 记忆——会话内容已经全量保留（D11）
- **不做**用 LLM 做画像抽取/冲突判定——低频高准优先，规则足够（D14）

---

## 8. 决策记录

| # | 议题 | 决定 | 时间 |
|---|---|---|---|
| D1 | 短期/长期的划分依据 | **按"记的是什么"划分**，不是按存储时效性：短期 = 会话内容，长期 = 用户习惯与做事方法 | 2026-07-22 |
| D2 | 会话内容是否过期 | **不设过期**，全量保留；注入预算与存储保留分离 | 2026-07-22 |
| D3 | 用户身份来源 | **假用户服务**签发 user_token；无登录体系，字段预留 `tenant_id` | 2026-07-22 |
| D4 | embedding provider | 待定（`openai` / `local` / `mock`） | — |
| D5 | 跨用户共享边界 | 程序记忆（技能/工具）全局；用户画像严格按用户隔离 | 2026-07-22 |
| D6 | 会话库与长期记忆库的关系 | **物理隔离**：会话内容 `memory/sessions.db`，长期记忆 `memory/semantic_memory.db`。理由：会话是高频写入的全量数据，长期记忆是低频写入的精选数据，生命周期、备份策略、清理策略都不同 | 2026-07-22 |
| D7 | 会话内容检索用什么 | **Python 侧 CJK 二元组打分**，不用 FTS5。理由：`unicode61` 把整句中文当一个 token、`trigram` 要求 ≥3 字符，中文短词检索两者都不行；规模到十万级再换 `sqlite-vec` | 2026-07-22 |
| D8 | 会话恢复的授权模型 | 客户端可回传 `session_id`，但**服务端必须校验该会话属于当前 user_id**，否则新建。理由：会话内容是用户数据，不能仅凭 session_id 就给读 | 2026-07-22 |
| D9 | `reset` 的语义 | 清空**工作上下文**，不删存储。消息仍在库里、可检索，只是不再装回上下文（`sessions.reset_at_seq` 记录起点） | 2026-07-22 |
| D10 | 持久化的开关 | `Agent(persist_session=True)` **显式开启**，默认关闭。理由：CLI / Streamlit / 单测不该往库里写垃圾数据 | 2026-07-22 |
| D11 | 长期记忆写不写 episode | **不写**。会话内容已全量保留且可检索，再存"执行记录"是重复存储；L2 只存蒸馏后的画像陈述 | 2026-07-22 |
| D12 | 画像冲突检测的粒度 | **按单槽维度归并**（语言/格式/长度/表情），归不上的退回字面骨架。理由：字面比对抓不住"改主意"，语义比对要 LLM（与"不让 LLM 决定要不要记"的决策冲突） | 2026-07-22 |
| D13 | 画像注入方式 | **常驻注入**（每轮都在场，≤400 字符），不走按需 RAG 召回。理由：画像描述"用户是谁"，等 query 命中才召回就晚了 | 2026-07-22 |
| D14 | 画像抽取用不用 LLM | ~~只用规则~~ → **改用 LLM**。实测：规则只认「我习惯…/我喜欢…」这类句式，用户正常下任务时一条都抽不出来；偶尔抽出来的还是错的（把「不要用国际站然后可以开始生成视频」当成长期雷区）。保留原判据「宁可漏不可错」，但手段从"规则保证精确" 换成 "**证据校验保证精确**" | 2026-11-18 |
| D15 | 检索融合算法 | **RRF**（k=60），不用加权求和。理由：余弦分 0~1、BM25 是任意量纲负值，相加要先归一化，而归一化没有可靠依据 | 2026-07-22 |
| D16 | `user_id` 是否必填 | **必填且无默认值**。宁可让老调用点编译期报错，也不要静默跨用户泄漏 | 2026-07-22 |

