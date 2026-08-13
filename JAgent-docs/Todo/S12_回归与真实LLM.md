# S12 — 回归与真实 LLM（全量验收）

> **所属层**: 全链路收尾（L1-L7）
> **关联**: `scripts/test_real_llm_flow.py` · `tests/` 全量 · `JAGENT-2026-P1-13` 黑盒回归
> **决策编号**: 全部（最终验收）
> **主控**: `JAGENT-2026-Completion-INDEX.md`

---

## 1. 前置依赖

- **S01~S11 全部完成**，INDEX 状态表已标记。
- 交付物快照（上游）：受信 PlanGuardrail（S03/S04）、OperationContract（S02）、DeliveryContract/完成门（S05/S06）、API 契约（S07）、Reviser 限权（S08）、终态守卫（S09）、生命周期取消（S10）、可观测性（S11）。

## 2. 问题背景

所有实现步骤的**最终证明**。AGENTS.md 协作规范明确："不要将 'Prompt 更详细了' '测试通过了' 直接等同于 '生产可用'。必须证明结构化契约、受信校验、生命周期回收和交付状态在弱模型、并发、超时、取消和异常情况下仍然成立。"

## 3. 为什么这么做

- 汇总验证 13 项全局验收标准（INDEX §0）。
- 真实 LLM 回归证明"非 Mock 环境"下契约与强制仍成立（弱模型是测试对象本身）。
- 产出最终验收报告，供统筹判定方案闭环。

## 4. 做之前先检查影响范围

- 全量测试：`pytest`（历史文档曾记录 952 passed / 2 skipped；最终以隔离 cache 的 clean-run 为准）。
- opt-in 真实 LLM 脚本：`scripts/test_real_llm_flow.py`、`scripts/test_llm_dag.py`——沿用，补充本方案场景。
- 事件链检查工具：`scripts/show_dataflow.py`、查询 API。
- 前端：`RunDetail` 对 `deliverable_status` 的展示（若未实现则记录为已知缺口，不阻塞后端验收）。
- 环境：LLM API key、Docker（如跑 Docker 载体）、数据库删除重建（历史数据不迁移口径）。

## 5. 期望达到的目标

- 满足下列 **13 项全局验收标准**（与 INDEX §0 一致）：

| # | 验收标准 | 验证方式 |
|---|----------|----------|
| 1 | 用户原始目标不可被 Planner/Reviser 覆盖 | S05/S08 测试 + 事件流 `intent_raw` 比对 |
| 2 | DeliveryContract 不依赖弱模型主动声明才能存在 | S07 caller 契约测试（fake + 真实 LLM） |
| 3 | PlanGuardrail 在 Executor 之前拒绝所有非法 DAG | S03 纯函数测试 + 端到端 |
| 4 | Reviser 不能弱化或删除交付目标 | S08 测试 |
| 5 | 工具副作用和确认策略按 operation 级别准确表达 | S02 契约测试 |
| 6 | 工具输入、输出和步骤引用均有明确 Schema | S02/S04 + Pydantic/JSON Schema 契约测试 |
| 7 | mechanical_complete 与 deliverable_met 分离 | S06 测试 + RunCompleted payload |
| 8 | Run 超时后所有子任务和资源可验证回收 | S10 测试 + pending_calls 断言 |
| 9 | Run 终态之后不会产生迟到副作用或事件 | S09 测试 |
| 10 | 失败原因和状态可从 EventStore 完整重放 | S11 重放脚本 |
| 11 | 真实 LLM 并发测试无永久卡住/幽灵 Run/资源泄漏 | opt-in 脚本 |
| 12 | 开发 reload 不会监听运行时文件 | S11 reload 探针 |
| 13 | 所有新增契约有单元/集成/故障注入/真实 LLM 回归测试 | 覆盖率汇总 |

- 产出 `JAgent-docs/Reports/` 下的最终验收报告（每项标注通过/未通过 + 证据位置）。

## 6. 实现要点

- 确定性回归：全量 `pytest`；新增契约测试以 fake LLM 为主（确定性）。
- opt-in 真实 LLM 脚本（`scripts/test_llm_dag.py` 或新 `scripts/test_completion_alignment.py`）：
  - 覆盖：write+read 复合目标（Planner 丢 write 必须失败）、Reviser 删契约/改路径被拒、read→list 不误判、非法 DAG、`$` 悬空引用、operation 级副作用 probe、watchdog 取消、并发 1/2/5/10。
  - 每组固定记录 classify/plan/tool/revise/answer 每阶段时间戳与耗时（配合 S11）。
  - 每个 Run 服务端分阶段超时 + 总 watchdog，禁止无限轮询。
  - 脚本通过 `pytest` marker（如 `@pytest.mark.realllm`）或独立脚本模式隔离，不进 CI 默认套件（L-04）。
- 事件链重放验证：对每个回归 Run 断言事件序列（RunStarted→PlanCreated→...→终态）且终态唯一。
- **禁止事项**：
  - 禁止把真实 LLM 测试硬编进 CI 默认套件（网络/成本抖动）。
  - 禁止在本步删改已验收契约（回归）。
  - 禁止在没有完整事件链证据时宣称"生产可用"。

## 7. 验收标准

1. 全量 `pytest` 绿（确定性套件，基线 952 为下限，允许新增后更多）。
2. 真实 LLM opt-in 脚本跑通并产出结构化结果（可挂/可失败项明示，不掩盖）。
3. 13 项验收每项有证据（测试文件 + 行号 / 事件链 / 报告）。
4. `JAgent-docs/Reports/` 新增最终验收报告（本方案编号，如 `JAGENT-2026-Completion-Final-Report.md`），逐项填写。
5. INDEX 状态表 S01~S12 全部标记完成；本步骤标记完成。

## 8. 这么做的后果

- **交付**：方案闭环，验收报告供统筹审查。
- **已知缺口记录**：前端 `deliverable_status` 展示、内容级匹配（L-02）、时序 `after`（L-01）若未实现，在报告中列为后续项，不阻塞本次验收。
- **运维**：真实 LLM 脚本依赖 API key 环境；无 key 时跳过并说明。

## 9. 收尾自检清单

- [ ] 全量确定性测试绿
- [ ] opt-in 真实 LLM 脚本跑通（或明确跳过原因）
- [ ] 13 项验收逐项有证据
- [ ] 最终验收报告落 `Reports/`
- [ ] INDEX 状态表全量更新
- [ ] 未决风险清单（前端展示/L-01/L-02）已记录

## 10. 完成状态

| 项 | 值 |
|----|----|
| 状态 | 已完成（审查修复） |
| 执行会话 | opencode（S12 会话） |
| 完成日期 | 2026-08-13 |
| 备注 | 隔离 cache clean-run：**1101 passed / 2 skipped / 1 warning**；ruff 全绿。已修复空 Plan、契约入口校验、Executor/merge Guardrail、阶段任务追踪、README 和报告证据；内容匹配 L-02 与 `after` 时序 L-01 继续延期，详见 `JAgent-docs/Reviews/completion_alignment_followup_20260813.md`。 |
