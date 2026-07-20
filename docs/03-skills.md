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
