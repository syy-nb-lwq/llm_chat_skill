# Self Evolution

本文说明当前已经落地的自演化链路，而不是理想化方案。

## 1. 当前能力边界

已经落地：

- 执行后异步评估
- 失败与成功记录持久化
- 待审批 patch 存储
- 人工审批 patch
- 审批后更新 skill YAML
- 后台反思循环
- 前端仪表盘查看统计和反思报告

尚未做到：

- 自动修改核心代码
- 复杂 patch 的结构化合并
- 高质量自动评估闭环

## 2. 数据流

主链路如下：

```text
Agent.handle()
  -> ExecutionCritic.evaluate()
  -> MemoryStore.record_failure()/record_success()
  -> MemoryStore.add_pending_patch()
  -> frontend PatchReview / EvolutionDashboard
  -> approve/reject API
  -> SkillStore.update_skill()
```

## 3. MemoryStore 落盘结构

当前目录结构：

```text
memory/
  failures/
    YYYY-MM/
      {trace_id}.json
  successes/
    success_index.jsonl
  skill_patches/
    pending/
      {patch_id}.json
  reflections/
    YYYY-MM/
      {report_id}.json
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
- 待审批 patch

说明：

- 评估默认异步运行，不阻塞主回复
- 只有开启 `self_evolution_enabled` 时才生效

## 5. SelfReflectLoop

文件：`core/reflect.py`

当前行为：

- 后台定时检查近期失败记录
- 达到阈值时生成反思报告
- 用户也可以主动请求生成反思报告

相关 API：

- `GET /api/reflections`
- `POST /api/reflections/request`

## 6. 前端入口

相关组件：

- `frontend/src/components/PatchReview.vue`
- `frontend/src/components/EvolutionDashboard.vue`

当前仪表盘能力：

- 查看失败数、成功数、pending patch 数、reflection 数
- 开关自演化 feature flag
- 请求即时 reflection
- 导出当前统计数据

## 7. 审批链路

补丁审批相关 API：

- `GET /api/patches`
- `POST /api/patches/{patch_id}/approve`
- `POST /api/patches/{patch_id}/reject`

当前 approve 行为：

1. 检查 feature flag
2. 把 patch 状态改为 approved
3. 读取 patch 文件中的 suggestion
4. 如果存在 `target_skill` 和 `method` 更新，写回 skill YAML
5. reload skill store

## 8. 风险与限制

- patch 应用目前只支持比较简单的字段更新
- 评估逻辑主要是规则判断，泛化能力有限
- 自演化能力默认应该谨慎开启
- 缺少更强的审计与回滚机制
