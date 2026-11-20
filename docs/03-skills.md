# Skills 模块

技能是项目中的一等对象，用来表达“完成某类任务的方法”。

## 1. 数据模型

文件：`skills/models.py`

核心对象：

- `Skill`
- `SkillStep`

`Skill` 关键字段：

- `name`
- `version`
- `capability`
- `method`
- `patterns`
- `tags`
- `steps`
- `examples`
- `source`
- `author`
- `created_at`
- `updated_at`

`SkillStep` 关键字段：

- `id`
- `name`
- `description`
- `tool`
- `params`
- `depends_on`
- `parallel_group`
- `fallback`
- `retry`
- `timeout_s`

## 2. 加载方式

文件：`skills/loader.py`

当前会扫描：

- `skills/`
- `skills/builtin/`
- `skills/user/`
- `backend/skills/`

支持两种文件格式：

- `.yaml`
- `.md`

说明：

- YAML 是主格式
- Markdown 主要用于兼容旧技能定义
- 没有显式 `source` 时，loader 会根据路径推断来源

## 3. 注册与匹配

文件：`skills/registry.py`

`SkillRegistry` 当前职责（M1-03/M1-04）：

- 维护 `name -> {version -> Skill}`（同名多版本并存）
- 维护 `active_versions[name] = version` 显式指针
- `match()` 只返回 active 版本
- 基于 `patterns`、`capability`、`method` 做轻量匹配
- YAML 含 `active: true` 时自动切换指针
- 冲突检测：同名同版本不可重复注册

版本不可变原则：更新必须生成新版本，切换 active 指针后才能生效。

## 4. 存储外观层

文件：`skills/manager.py`

`SkillStore` 提供兼容性更好的 CRUD 接口：

- `reload()`
- `list_all()`
- `get_by_name()`
- `match()`
- `delete_by_name()`
- `delete_version()`
- `update_skill()`
- `persist(skill)` —— **落盘的唯一入口**

关于 `persist`：`add()` 只改内存索引，进程重启即失；`persist()` 才写
`skills/user/<name>@<version>.yaml`（`taught`/`harvested`）或 `skills/builtin/`。
任何"把技能真正存下来"的路径都必须走它 —— 历史上 `approve_draft()` 直接调
`add()`，于是出现"批准了、界面也有了、重启之后又空了"。

最近的重要变化：

- 删除技能不再直接改内部字典，而是删除文件后 `reload`
- 支持按名称删除和按版本删除
- `approve_patch` 后会通过 `update_skill()` 持久化回 YAML

## 5. 教学沉淀

教学能力由 `SkillTrainer` 驱动，最终技能会写入：

- `skills/user/{name}@{version}.yaml`

版本策略目前很简单：

- 如果技能不存在，默认 `1.0.0`
- 如果已存在，通常递增 patch 版本

## 6. 与运行时的关系

运行时中技能有两种作用：

1. 作为 `ManagerAgent` 的选择对象
2. 作为 `DAGExecutor` 的结构化步骤来源

当满足下面条件时，`Agent` 会优先把技能步骤翻译成 `ToolTask`：

- `skill_dag_enabled` 为真
- 选中了技能
- 技能包含结构化 `steps`

## 7. 当前缺陷

- `SkillRegistry.match()` 仍是轻量打分，不适合复杂技能路由
- 版本治理（冲突检测/迁移工具）偏弱，M1-03/M1-04 仅解决"不可变注册"问题
- `update_skill()` 目前只做浅层字段覆盖，不支持精细修改 step
- Markdown 技能格式仍是兼容模式，不建议继续扩展

## 8. 方法论自增长（harvest）

文件：`core/skill_harvest.py`

`SkillTrainer` 是"人教"，`harvest` 是"自己攒"：一次多步任务做成了，
就把经验提炼成技能草稿。

### 8.1 链路（记账 → 晋升 → 修订）

旧实现是"任务成功 → 立刻产一条技能"，于是「充值完成处理」和「充值后重生成」
在 27 分钟里沉淀成两条，系统根本不知道它们是同一件事。现在中间多一层**能力档案账本**
（`core/skill_ledger.py`，落盘 `memory/skills/ledger.json`）：

```
任务成功
  → harvest()            闸门：≥2 个不同工具 或 ≥2 轮；且至少一次成功
  → 调 LLM 提炼          一次调用同时做两件事：归到哪个能力档案 + 这次多知道了什么
  → ledger.record()      无论产不产技能，这一笔都记下（含 insight）
  → ledger.readiness()   够格？见 8.6：次数 × 价值
       ├─ 不够 → accumulating，只记账，不产草稿
       ├─ 够 + 无技能 → SkillDraftStore 产出新草稿
       └─ 够 + 已有技能 → 产出修订草稿（版本 +1），不是又添一条
  → 人批准                POST /api/skill-drafts/{id}/approve
  → SkillStore.persist() skills/user/<name>@1.0.0.yaml（落盘 + 激活）
  → ledger.bind_skill()  挂回档案，同一 key 以后走修订
```

"同类"不靠字符串判断：按原话字符 bigram + 工具集合聚类，15 条真实草稿里
最高相似度只有 0.32。所以归类交给 harvest 里那次 LLM 调用，增量成本接近零。

草稿存在独立队列而不是直接写技能库：`list_all()` 只返回激活版本，
`set_active=False` 存进去等于黑盒 —— 界面看不见"待批准"。

### 8.2 method 该写什么

**method 写判断，steps 写步骤。** 这是这条链路唯一重要的设计约束。

`steps` 已经结构化记录了"调过哪些工具、什么顺序"；`method` 再把这串工具名
翻译成"第一步用 A、第二步用 B"，信息量为零 —— 换个场景 A/B 根本不存在。

所以 `HARVEST_PROMPT` 要求写三类东西：

1. **判断规则**：什么信号该走这条路、什么信号该换招
2. **坑**：踩过或差点踩到的地方，怎么发现的
3. **完成标准**：怎么才算真做完，而不是"看起来做完了"

同时 `_build_trajectory()` 会把**失败原因**（脱敏后：去 URL/密钥/绝对路径）
带进提示词。只给"失败"两个字，模型只能提炼出步骤顺序；给了报错，
它才可能写出"报错说找不到文件，所以下一轮先确认了根目录"。

### 8.3 形式闸门 `assess_method_quality()`

只管**写得像不像方法论**，不管内容有没有用。不做闸门的后果是真实的：
一批 15 条草稿里 9 条写成了执行日志，攒了三天一条都没人愿意批准。判据：

| 级别 | 条件 | 处理 |
|---|---|---|
| reject | 正文点名 ≥2 个工具标识符（`python_exec`、`xxx()`） | 不进草稿，`HarvestResult.reason` 说明原因 |
| warn | 点名 1 个工具 / ≥3 步却零判断 / 正文 <100 字 / 触发词照抄用户原话 | 进草稿并标记，界面标黄 |

模型自报 `novelty` 三选一，避免把两种"没价值"混为一谈：

| novelty | 含义 | 处理 |
|---|---|---|
| `new` | 出现了新判断 / 坑 / 完成标准 | 写 method，参与晋升 |
| `same` | 与已有方法论一致，没有新东西 | 沿用档案正文；**不是没价值**，稳定本身是晋升信号 |
| `not_worth` | 这类事一次性、换场景用不上 | 一条都不记 |

实测踩过的坑：模型曾把第 3 次「完全同类、没有新判断」判成不值得沉淀，
导致一条本该晋升的技能永远卡在 2 次 —— 所以 `same` 必须和 `not_worth` 分开。

### 8.4 界面

「📘 技能」tab 顶部是「待批准」区（`SkillDrafts.vue`）：

- 每条草稿显示方法论正文、触发词、自动记录的步骤、质量提示
- 每条草稿带「有价值 / 无价值」标记，点开可见判定理由（命中了哪类信号），供人复核
- 有 `issues` 的标红「建议改写」，批准前二次确认
- 支持逐条批准/丢弃，以及「清理建议改写的 N」（批量丢弃执行日志型草稿）
- 下方「能力档案」区折叠展示：每类事做了几次、还差几次、攒下的 insight 原文

没有这个入口时，15 条草稿在后端躺了三天，界面始终显示"暂无技能"。

### 8.5 价值判断 `assess_value()` —— 凭什么算 skill

这是整个沉淀机制最前置、也最容易做错的一问。用户的产品定义是：

> skill 是做事的方法，不是做过的事。是不断完善的，不是每次新增的。

把这条定义翻译成判据只有一句：

> 一条 method 值钱，当且仅当它含有 planner 光看任务描述推不出来的信息 ——
> 反直觉的判断、踩过的坑、隐式的完成标准。把这些删掉，剩下"先做 A 再做 B"
> 的部分，planner 每次都能自己推出来；固化它只会用上次的路径覆盖这次的计划，
> 让系统变笨。

**注意它和另两个判断的分工**（这三个曾被混为一谈）：

| 判断 | 问的是 | 实现 |
|---|---|---|
| `assess_method_quality` | 写得像不像方法论（形式） | 工具名/步骤词的规则 |
| `assess_value` | 有没有可迁移的判断（实质） | 四类硬信号 + 模型 insight |
| `ledger.readiness` | 这类事够不够格晋升（次数 × 价值） | 3 次观察 + 至少一处价值信号 |

**四类硬信号**（命中任意一类即有价值，`assess_value` 会给出可读理由，界面可复核）：

1. **坑 / 失败模式**：踩过或差点踩的错，以及怎么绕开
2. **反直觉判断**：看起来该这样、其实该那样
3. **按信号分支**：出现信号 A 走这条路、信号 B 换招
4. **完成标准**：怎么算真做完，区别于"看起来做完了"

规则没抓到硬信号、但模型在 `insight` 字段里说清了一句具体洞察（≥15 字），
也算有价值。两者都拿不出 → 判无价值，`harvest` 不会用它覆盖档案里已有的好正文。

**价值信号怎么攒下来的**：`ledger.record(insight=...)` 把洞察去重存进档案的
`insights` 列表；`readiness()` 要求至少有一处 insight 或 method 正文才放行。
于是"做了 10 次但一次都没说出学到了什么"的档案永远晋升不了 —— 频繁 ≠ 有价值。

真实 LLM 实测（`scripts/probe_harvest_classify.py`）：「充值恢复后重新生成」
攒到 3 次时产出草稿，模型给出的 insight 是"充值只恢复执行条件，不会自动替换
之前失败或不可用的产物"——这是反直觉判断，不是流水账；价值判定四类信号全命中。

### 8.6 已知边界

- 价值判据中的"硬信号"是关键词规则，能挡住明显的流水账，但会漏判用另一种
  措辞写出的真判断（模型 insight 字段是第二道补充，不是万能）
- 归类依赖 LLM，成本不为零；同类任务的 key 若漂移，Catalog 只能降低概率、不能杜绝
- `memory/skill_patches/pending/` 里的补丁若 `target_skill` 为空
  （失败发生在没有技能参与的任务里），即使 `status=approved` 也无法落地
