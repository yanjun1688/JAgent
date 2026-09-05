# 交接提示词：质量门禁与执行依赖分离设计（ADR-009 Q-01~Q-06）

> 用法：将本文档"## 提示词（从这里开始复制）"之后的内容整体粘贴给一个新的 AI 会话。
> 该会话将是独立任务会话，与本交接文档的作者会话完全分离。

---

## 提示词（从这里开始复制）

你是 Harness v2.1 Agent-First 执行引擎项目的**架构实现者**，承担独立任务会话职责。请严格按 AGENTS.md 协作规范执行，禁止猜测、禁止跨层跳跃、禁止修改未冻结的架构决策。

### 一、项目背景

项目根目录：`D:\Project\JAgent`

必须先阅读以下文件以获取完整上下文：

1. `AGENTS.md` —— 开发协作规范（分层约束、受信边界、测试规范、Bug 根治原则）
2. `JAgent-docs/architecture/ADR-009_质量门禁与执行依赖分离设计.md` —— 本任务的**唯一依据**（Q-01~Q-08 决策）
3. `JAgent-docs/architecture/ADR-008_交付契约与完成语义分层.md` —— 契约模型背景
4. `JAgent-docs/architecture/ADR-007_任务完成语义与执行态正交分层设计.md` —— 执行态背景
5. `JAgent-docs/plans/completion-semantics/JAGENT-2026-Completion-INDEX.md` —— 步骤主控

### 二、已完成的上游工作（禁止回改）

以下内容已实现并通过全量测试（1109 passed / 2 skipped），**不要修改**：

- Q-07 **总计时器已实现**：`SchedulerConfig.phase_timeout_ms` 已移除；`run_timeout_ms` 是唯一总预算；`_phase_call`/`_handle_pause`/`_wait_for_resume` 使用 Run 全局 deadline 的剩余时间。
- Q-08 **无需确认过期已实现**：不引入 confirmation TTL；等待纳入总预算。
- caller/extracted DeliveryContract 统一受信校验（`validate_delivery_contract_input`）。
- RunStarted 先写 + `DeliveryContractsResolved` 事件 + fold 折叠。
- DagExecutor 入口与合并后 Plan 的 PlanGuardrail 重新校验。
- EventStore 事务内分配真实 seq。
- content 不参与交付匹配（D-03/L-02）。

### 三、本任务的 6 项决策（ADR-009 Q-01~Q-06）

#### Q-01：DeliveryContract 定位（确认/维持，无代码改动）

DeliveryContract 是用户要求的唯一权威载体 + 系统最终验收标准（可信）。不参与调度、不读 Answer、不读 LLM task_state。

**动作**：核查现状符合后，在 ADR-009 中把 Q-01 状态标注为"已确认"，无需代码改动。

#### Q-02：`Plan.required_operations` → `declared_operations`（LLM 自检声明）

**目标**：把"LLM 自报操作"与"系统真实交付契约"从命名上彻底分离，避免 `required_operations` 被误当作系统要求。

**改动范围**（必须用 `grep` 全库核对，不能遗漏）：

- `harness/models/plan.py`：`DagPlan.required_operations` 字段 → `declared_operations`
- `harness/models/plan.py`：`RequiredOperation` 类名保留（它是匹配逻辑载体），但注释明确其为"LLM 自检声明"
- `harness/core/planner.py`：解析 LLM 输出时写入 `declared_operations`
- `harness/core/planner.py`：`PlanGuardrail.validate` 中对 `declared_operations` 的自洽检查保留，但**只作为 LLM 计划结构检查**，不承担交付验收
- `harness/core/scheduler/plan.py`：`CompletionVerdict.compute` 中的机械维度检查改用 `declared_operations`（仍属机械维度，不代表交付）
- 所有测试中的 `required_operations=[...]` 夹具同步改名
- 检查是否有 JSON payload 序列化字段（`model_dump`）会被事件/OpenAPI 暴露，若有需同步

**验收**：
- `grep required_operations harness/` 无结果（模型字段层面）
- 新增测试：Planner 输出 `declared_operations` 被解析且 guardrail 自洽检查仍生效
- 完成门 deliverable 维度仍只由 DeliveryContract 驱动，不受改名影响

#### Q-03：唯一执行依赖（确认/维持，无代码改动）

执行顺序/数据依赖只由 `DagStep.depends_on` 表达，无第二套执行依赖模型。

**动作**：核查现状符合后，在 ADR-009 中把 Q-03 状态标注为"已确认"。

#### Q-04：完成门边界（确认/维持，无代码改动）

`CompletionGate` 只信 `DeliveryContract + StepResult`。`deliverable_met` 逐条对照契约；空契约 → `unverified`。

**动作**：核查 `scheduler/plan.py` 的 `CompletionVerdict.compute` / `verify_deliverables` 符合后，在 ADR-009 中把 Q-04 状态标注为"已确认"。若发现 deliverable 维度引用了 `declared_operations`，必须修正为只信 DeliveryContract。

#### Q-05：删除 `DeliveryContract.after` 字段

**目标**：契约不再承载时序职责。

**改动范围**：

- `harness/models/intent.py`：删除 `DeliveryContract.after: list[str]` 字段
- 检查所有使用 `.after` 或构造 `DeliveryContract(after=...)` 的位置并清理
- 测试：`tests/test_intent_contract.py::test_delivery_contract_after_field_reserved` 应删除或改写为"after 已移除"
- 历史事件中可能残留 `after` 字段，Pydantic 默认忽略未知字段，**不需要数据迁移**，但要确认 fold 不报错（补充一个历史事件回放测试）

**验收**：
- `grep "\.after" harness/ JAgent-docs/` 中 DeliveryContract 相关无残留（注意区分其他模块的 `after` 变量）
- 历史含 `after` 字段的事件流能正常 fold

#### Q-06：mutating 覆盖只认 DeliveryContract

**目标**：堵住"Reviser 通过自报 `declared_operations` 授权新副作用"的漏洞。

**当前问题位置**：`harness/core/planner.py::validate_revision_invariants`，现在覆盖判定为：

```python
covered = any(step_satisfies(step, c) for c in root_contracts) or any(
    step_satisfies(step, r) for r in declared_ops
)
```

**改为**：

```python
covered = any(step_satisfies(step, c) for c in root_contracts)
```

**改动范围**：

- `harness/core/planner.py::validate_revision_invariants`：mutating 覆盖只认 `root_contracts`
- 相关注释与 `revision_invariant_feedback` 提示文案同步更新
- 检查 `scheduler/plan.py` 中是否有类似 `declared_operations` 参与副作用授权的逻辑

**验收**：
- 新增测试：无契约时 Reviser 新增 mutating step → 被拒；有 DeliveryContract 覆盖时才允许
- 现有测试中"Reviser 自报 required_operations 后新增 mutating step 被放行"的行为如果有，改为"被拒"

### 四、执行纪律

1. **先报告差异，再改代码**：开工前先对照 ADR-009 逐项检查当前代码，输出差异清单（Q-02/Q-05/Q-06 的每个改动点 + 现状），等用户确认。
2. **受信边界**：Planner/Reviser/classify/answer 是非受信组件；PlanGuardrail、ToolExecutor、Scheduler、fold、EventStore 是受信组件。受信组件不读 LLM task_state 做判定。
3. **每项改动配测试**：单测/集成/故障注入，禁止只改代码不补测试。
4. **禁止跨层**：本任务只做契约模型层 + 校验层 + 完成门层。不碰 L6/L7（API/前端）除非有 `model_dump` 暴露需要同步。
5. **禁止修改**：Q-07/Q-08 已实现内容、EventStore 事务逻辑、既有受信校验，除非本任务明确要求。

### 五、验证命令

```bash
# 针对性测试（每完成一项就跑）
python -m pytest -q tests/test_intent_contract.py tests/test_plan_guardrail_structure.py \
  tests/test_reviser_restriction.py tests/test_deliverable_gate.py tests/test_completion_gate.py \
  tests/test_planner.py tests/test_scheduler.py

# 全量（隔离 cache）
python -m pytest -q -p no:cacheprovider

# lint
python -m ruff check harness tests scripts

# 差异检查
git diff --check
```

### 六、交付物

1. ADR-009 更新：Q-01/Q-03/Q-04 标注"已确认"；Q-02/Q-05/Q-06 标注"已实施"
2. `JAgent-docs/Reports/JAGENT-2026-Completion-Final-Report.md` 追加本任务结果
3. 全量测试绿 + ruff 全绿
4. 代码 + 测试改动清单

### 七、最终检查清单

- [ ] Q-02 改名完成，`grep required_operations` 模型层面无残留
- [ ] Q-05 after 字段删除，历史事件回放不报错
- [ ] Q-06 mutating 覆盖只认 DeliveryContract，新增测试通过
- [ ] Q-01/Q-03/Q-04 已核查并标注确认
- [ ] 全量 1109+ 新增测试数通过，ruff 全绿
- [ ] ADR-009 与报告文档已更新
