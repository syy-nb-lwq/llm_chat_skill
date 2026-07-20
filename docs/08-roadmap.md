# Roadmap 与任务清单

> 更新日期：2026-07-20（本次 M0+M1 里程碑完成后）

本文档聚焦三件事：

- 最近已经完成了什么
- 当前还剩哪些缺陷和开发任务
- 里程碑整体进度

---

## 1. 里程碑进度

| 里程碑 | 任务数 | 状态 |
|--------|--------|------|
| **M0 可信基线** | 8 | M0-01~02 ✅ · M0-03 ✅ · M0-04 ✅ · M0-05 ✅ |
| **M1 技能教学闭环** | 10 | M1-01~09 ✅ · M1-10 进行中 |
| M2 长期记忆 | 9 | 待开始 |
| M3 反馈驱动演进 | 7 | 待开始 |
| M4 受控工具沉淀 | 6 | 待开始 |
| C 持续改进 | 4 | C-01 ✅ · C-02~04 待完成 |

详细任务清单见 [11-开发任务清单.md](./11-开发任务清单.md)。

---

## 2. 本轮已完成的关键功能

### M0 可信基线

| 任务 | 文件 | 说明 |
|------|------|------|
| M0-01 execution_id | `core/identity.py` | 每次 `Agent.handle()` 生成唯一 `execution_id`/`turn_id`/`user_id`/`session_id`，避免失败记录互相覆盖 |
| M0-02 执行记录字段 | `core/critic.py` · `agents/learning.py` | 工具真实名称/retry/fallback/latency_ms 回填到 `result.meta` |
| M0-03 WebSocket 集成测试 | `tests/test_backend.py` | 重写为直接调 dispatcher，跳过 TestClient WS 的死锁问题 |
| M0-04 前端配置化 | `frontend/src/config.js` | 统一 `API_BASE`/`WS_BASE`，消除 10 处 `localhost:8000` 硬编码 |
| M0-05 源码编码 | — | 全部源文件验证为 UTF-8，无乱码 |

### M1 技能教学闭环

| 任务 | 文件 | 说明 |
|------|------|------|
| M1-01 TeachingSession | `agents/teaching_session.py` | 多轮状态机持久化（Collecting→Draft→Active），`teachings/` JSON 落盘 |
| M1-02 无工具技能执行 | `agents/manager.py` | `selected_skill` 不再依赖 `tool_tasks` 是否存在 |
| M1-03 技能版本不可变 | `skills/models.py` · `skills/registry.py` | 同名多版本并存，`name@version.yaml` 不可原地覆盖 |
| M1-04 active 指针 | `skills/loader.py` · `skills/manager.py` | YAML `active: true` 显式激活，`match()` 只返回 active 版本 |
| M1-05 验证流水线 | `skills/validator.py` | DAG 无环/工具存在/schema 校验，`validate_skill()` 返回 issues |
| M1-06 重复决策流程 | `agents/skill_trainer.py` · `backend/main.py` | reuse / update_new / cancel 三路决策，TeachingSession 状态持久 |
| M1-07 _complete_teaching bug | `core/agent.py` | 移除不存在的 `self.llm` 调用和错误的消息角色拼接 |
| M1-08 发布确认 API | `backend/main.py` | `/api/teachings/confirm` 供前端草稿确认发布 |
| M1-09 SkillManager 接入主链 | `agents/manager.py` · `core/agent.py` | `MANAGER` 意图检测 + 主链分支，列出/版本/回滚 |

### C 持续改进

| 任务 | 文件 | 说明 |
|------|------|------|
| C-01 身份层 | `infra/auth.py` | `owner_token` 校验 · `client_id` 服务端签发 · `get_user_from_request/WS` |

---

## 3. 当前剩余缺陷（P0~P2）

### P0

1. **M1-10 e2e 教学闭环未验证** — 教学→重启→召回→执行全链路端到端测试尚待补充
2. **Session 进程内存** — `backend/session.py` 仍是进程内实现，服务重启丢失

### P1

1. **M0-06 ToolHub 启动可观测性** — 工具源连接失败只 warning，未暴露到健康检查
2. **M0-07 e2e 测试夹具** — 缺少隔离的运行时数据夹具
3. **M0-08 前端组件测试** — WebSocket service、PatchReview、EvolutionDashboard 无回归测试

### P2

1. **C-02 配置诊断命令** — CLI 子命令展示 provider/feature flag/embedding 状态
2. **C-03 Patch 审计与回滚 UI** — 完整记录提案来源/diff/审批人，支持回滚操作
3. **M2~M4 尚未开始** — 见 [11-开发任务清单.md](./11-开发任务清单.md)

---

## 4. 下一步建议

按评审文档 `10-目标架构评审与演进方案.md §10` 推荐顺序：

1. **完成 M1-10 e2e** — 验证"教→重启→召回→执行"可重复通过（M1 验收用例）
2. **推进 M2 长期记忆** — 统一 MemoryRepository、embedding 初始化、主链路写入（M2-01~09）
3. **C-02/C-03** — 配置诊断 + Patch 审计 UI（不阻塞 M2）
4. **M3/M4** — 在 M2 完成后推进

**不建议在 M1/M2 完成前做的事**（来自架构评审 §13）：

- 增加更多职责重叠的 Agent 类
- 开启高置信度 patch 自动发布
- 允许模型直接生成并执行 Python 工具
- 无差别写入长期记忆
- 依靠提示词替代状态/版本/权限治理
