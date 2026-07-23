# Self Evolution

本文说明当前已经落地的自演化链路，而不是理想化方案。

## 1. 当前能力边界

已经落地：

- 执行后异步评估
- 失败与成功记录持久化
- 待审批 patch 存储
- 人工审批 patch
- 审批后生成新版本草稿（M3-04，不原地改 YAML）
- 后台反思循环
- 前端仪表盘查看统计和反思报告
- `FeedbackEvent` 绑定 `execution_id`（M3-02），用户纠正可回溯到具体执行
- 统一 patch schema（M3-01）
- `ResultValidator`（M3-03），没有工具的任务不再无条件得 100%
- 旧样例 + 用户纠正样例回归 + 风险展示（M3-05），回归失败阻止发布
- 高置信度 `auto_approved` patch 必须经过 Validator + 回归样例 + 审计记录才落盘（M3-06）
- Patch 审计记录 + 回滚 UI（C-03）

尚未做到：

- 自动修改核心代码
- 复杂 patch 的结构化合并
- 高质量自动评估闭环

## 2. 数据流

主链路如下：

```text
Agent.handle()
  -> ExecutionCritic.evaluate()
  -> MemoryRepository.add_episode()           # M2-03 统一存储
  -> MemoryStore.record_failure()/record_success()  # 委托到 Repository
  -> MemoryStore.add_pending_patch()           # 含 auto_approved
  -> FeedbackStore.save()                      # M3-02 用户反馈
  -> ResultValidator.validate()                # M3-03 输出校验
  -> frontend PatchReview / EvolutionDashboard
  -> approve/reject API
  -> 回归样例 + 风险展示（M3-05）
  -> 通过 → SkillStore 新版本草稿（M3-04）
  -> 失败 → 阻止发布，返回原因
```

> 旧链路中的 `MemoryStore` 已委托到 `MemoryRepository`（M2-01 统一存储），业务代码不再直接读写 JSON/JSONL/SQLite。详见 [05-core.md §4](./05-core.md#4-memoryrepositorym2-01--m2-02)。

## 3. 落盘结构

当前目录结构：

```text
memory/
  episodes/
    {execution_id}.json            # M2-03 EpisodeRecord（按 execution_id 命名，不互相覆盖）
  failures/
    YYYY-MM/
      {trace_id}.json
  successes/
    success_index.jsonl
  skill_patches/
    pending/
      {patch_id}.json              # 含 auto_approved（M3-06）
  feedback/
    {execution_id}__{feedback_id}.json   # M3-02 FeedbackEvent
    _index.json                    # 按 execution_id 索引
  reflections/
    YYYY-MM/
      {report_id}.json
  semantic_memory.db               # M2 统一 SQLite（FTS5 + 向量）
```

## 4. ExecutionCritic

文件：`core/critic.py`

当前评估维度：

- success rate
- fallback count
- latency
- user corrected

当前输出：

- 失败记录
- 成功记录
- 待审批 patch（含 `auto_approved` 标记）

说明：

- 评估默认异步运行，不阻塞主回复
- 只有开启 `self_evolution_enabled` 时才生效
- 高置信度 patch 标记 `auto_approved`，但仍进入待审队列（M3-06），不再直接 `_apply_patch()` 落盘
- `build_execution_context()` 把四级标识（`execution_id` / `turn_id` / `user_id` / `session_id` / `parent_execution_id`）+ 工具结果 + latency + task_specs 一起传入

## 5. 统一 patch schema（M3-01）

patch 字段统一为：

- `target_skill`：目标技能名
- `version_target`：目标版本号（如 `1.0.1`）
- `diff`：方法/字段层面的差异描述
- `recommendations`：建议列表
- `evidence_execution_id`：触发该 patch 的执行 id（可回溯到具体执行）

不再因 schema 不匹配而静默失败。

## 6. FeedbackEvent（M3-02）

文件：`core/feedback.py`

`FeedbackEvent` 绑定 `execution_id`，确保用户纠正可回溯到具体执行：

- `type`：`accept / reject / correction / retry / rating`
- `content`：反馈正文
- `rating`：可选 1~5 评分
- `user_id` / `session_id`：会话/记忆隔离

`FeedbackStore` 持久化到 `memory/feedback/{execution_id}__{feedback_id}.json`，并维护 `_index.json` 按 `execution_id` 索引。`correction` 类型反馈会触发 patch 生成。

相关 API：`POST /api/feedback` 🔒 / `GET /api/feedback`。详见 [05-core.md §3.3](./05-core.md#33-feedbackeventm3-02)。

## 7. ResultValidator（M3-03）

文件：`core/result_validator.py`

`ResultValidator.validate(skill, final_output, user_input)` 在执行完成后判断最终输出是否真正满足用户目标：

- `non_empty` / `min_length`：输出非空且不过短
- `capability_coverage`：从 `skill.capability` 提取关键词，检查输出是否覆盖
- `method_steps`：从 `skill.method` 提取步骤标记，检查覆盖率（< 34% 视为不通过）
- `examples_style`：输出与至少一个 `examples` 有关键词重叠

返回 `ResultValidation(passed / score / issues / checks)`。没有工具的任务不再无条件得 100%。

## 8. SelfReflectLoop

文件：`core/reflect.py`

当前行为：

- 后台定时检查近期失败记录
- 达到阈值时生成反思报告
- 用户也可以主动请求生成反思报告

相关 API：

- `GET /api/reflections`
- `POST /api/reflections/request`

## 9. 前端入口

相关组件：

- `frontend/src/components/PatchReview.vue`
- `frontend/src/components/EvolutionDashboard.vue`
- `frontend/src/components/SkillManager.vue`（M1-08）

当前仪表盘能力：

- 查看失败数、成功数、pending patch 数、reflection 数
- 开关自演化 feature flag
- 请求即时 reflection
- 导出当前统计数据

## 10. 审批链路（M3-04 / M3-05 / M3-06 / C-03）

补丁审批相关 API：

- `GET /api/patches`
- `POST /api/patches/{patch_id}/approve` 🔒
- `POST /api/patches/{patch_id}/reject` 🔒

当前 approve 行为：

1. 检查 feature flag
2. 跑旧样例 + 用户纠正样例回归（M3-05）
3. 生成 `regression_results` 与 `risk_summary`，展示 diff 与风险
4. 回归失败 → 阻止发布，返回原因
5. 通过 → 把 patch 状态改为 approved，生成新版本草稿（M3-04，不原地改 YAML）
6. reload skill store，旧版本仍可查看

`auto_approved` patch（M3-06）：

- 不再直接 `_apply_patch()` 落盘
- 进入待审队列，必须经过 Validator + 回归样例 + 审计记录才落盘
- `MemoryStore.get_pending_patches()` 包含 `auto_approved` 项

审计与回滚（C-03）：

- 审批记录字段：提案来源、模型提示版本、diff、测试结果、审批人、发布时间、被替代版本
- `GET /api/skills/{name}/audit`：查看审计记录
- `POST /api/skills/{name}/rollback/{version}` 🔒：回滚到指定版本（保留历史，不删除）

## 11. 风险与限制

- patch 应用目前只支持比较简单的字段更新，复杂 patch 的结构化合并仍待打磨
- 评估逻辑主要是规则判断，泛化能力有限
- 自演化能力默认应该谨慎开启（`self_evolution_enabled=false`）
- `auto_approved` 已收紧到必须经过门禁，但仍建议人工复核高置信度 patch
