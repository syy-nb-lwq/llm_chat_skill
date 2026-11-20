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

### 8.1 链路

```
任务成功
  → harvest()            闸门：≥2 个不同工具 或 ≥2 轮；且至少一次成功
  → 调 LLM 提炼          输入 = 用户原话 + 工具轨迹（成功/失败 + 失败原因）
  → _build_skill_from_plan   闸门：质量评估（见 8.3）
  → SkillDraftStore      memory/skills/drafts/<id>.json（**不生效**）
  → 人批准                POST /api/skill-drafts/{id}/approve
  → SkillStore.persist() skills/user/<name>@1.0.0.yaml（落盘 + 激活）
```

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

### 8.3 质量闸门 `assess_method_quality()`

不做闸门的后果是真实的：一批 15 条草稿里 9 条写成了执行日志，攒了三天
一条都没人愿意批准。判据：

| 级别 | 条件 | 处理 |
|---|---|---|
| reject | 正文点名 ≥2 个工具标识符（`python_exec`、`xxx()`） | 不进草稿，`HarvestResult.reason` 说明原因 |
| warn | 点名 1 个工具 / ≥3 步却零判断 / 正文 <100 字 / 触发词照抄用户原话 | 进草稿并标记，界面标黄 |

模型也可以自己给 `worth_keeping: false`（任务太简单、换个场景用不上），
此时不产生草稿 —— 宁缺毋滥。

### 8.4 界面

「📘 技能」tab 顶部是「待批准」区（`SkillDrafts.vue`）：

- 每条草稿显示方法论正文、触发词、自动记录的步骤、质量提示
- 有 `issues` 的标红「建议改写」，批准前二次确认
- 支持逐条批准/丢弃，以及「清理建议改写的 N」（批量丢弃执行日志型草稿）

没有这个入口时，15 条草稿在后端躺了三天，界面始终显示"暂无技能"。

### 8.5 已知边界

- 草稿质量取决于 LLM，闸门只能拦"明显是流水账"的，不能保证留下的都好
- 一次任务只提炼一条；同类任务反复做成不会自动合并成更抽象的版本
- `memory/skill_patches/pending/` 里的补丁若 `target_skill` 为空
  （失败发生在没有技能参与的任务里），即使 `status=approved` 也无法落地
