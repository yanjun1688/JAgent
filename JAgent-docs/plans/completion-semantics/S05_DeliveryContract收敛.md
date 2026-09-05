# S05 — DeliveryContract 收敛（单一契约模型 + 事件 + fold）

> **所属层**: L1（事件类型）+ L3（运行态模型）
> **关联**: `models/events.py` · `core/fold.py` · `models/plan.py`（`RequiredOperation`）· 新增 `models/intent.py`
> **决策编号**: D-02（方案 A+B 并存）· D-04（旧请求 unverified）· D-05（多交付物）· C-01（模型收敛）· 问题一（intent 无结构化落地）
> **主控**: `JAGENT-2026-Completion-INDEX.md`

---

## 1. 前置依赖

- **S01** 已完成（决策可引用）。
- 交付物快照（上游）：
  - `harness/models/plan.py:21-38` 现有 `RequiredOperation`（`tool` + `input`，LLM 自报）。
  - `harness/models/events.py:54-60` `RunStartedPayload`（`intent` 自由文本）。
  - `harness/core/fold.py` `RunState`（无 intent 结构 / 契约字段）。
  - `harness/api/routes.py:264-286` `create_run` 写 `RunStarted`。

## 2. 问题背景

核心根因（JAGENT-2026-P1-13 Run `cee7d7f6`）：
> 用户："创建 blackbox.txt，写入 hello harness blackbox，然后重新读取"
> Planner 只生成 read；read 失败；Reviser 改 list；list 成功；`RunCompleted(all_normal=true)`。

即使现有 Layer 2 `required_operations` 契约存在，弱模型可以在 `required_operations` **和** plan steps **同时**丢 write（自洽地错误）。纯结构检查只能验证"LLM 自己声明的东西是否达成"，无法验证"用户要求的硬性交付物是否达成"。且现有 `RequiredOperation` 没有来源标记，无法区分"调用方声明"与"LLM 抽取"。

## 3. 为什么这么做

- 把"用户到底要求了什么"从自由文本升级为**受信契约**，且契约来源可审计（C-01：单一 `DeliveryContract{provenance}`）。
- 为 D-03（deliverable_met 操作+路径级）、D-04（unverified 标记）提供数据载体。
- 事件流必须能完整重放原始目标与交付契约（AGENTS.md 约束 + INDEX 验收项 10），所以契约要落事件。

## 4. 做之前先检查影响范围

- `RequiredOperation` 消费方：
  - `harness/core/planner.py:180-186`（正向覆盖检查）、`:671-685`（LLM 解析）。
  - `harness/core/scheduler/plan.py:918-928`（完成门 required_operations 检查）。
  - 测试：`tests/test_completion_gate.py`、`tests/test_planner.py`。
- `RunStartedPayload` 消费方：`create_run`（routes.py）、`fold.py:128-135`、`ScopedEventStore.append_event`（`client_request_claims`）。
- `PAYLOAD_MODEL_MAP`（events.py:373-408）：新增事件/字段必须注册，否则 `parsed_payload` 崩溃。
- **兼容策略（关键）**：
  - `RequiredOperation` **保留**（内部继续用），但 `DeliveryContract` 是其"带 provenance + after + 交付语义"的升级体；S06 完成后 `_completion_gate` 改读 `DeliveryContract`。本步不做 API 变更（S07 做）。
  - 旧请求（无契约）→ 空契约列表 + 全局 unverified 标记，不阻断运行（D-04）。

## 5. 期望达到的目标

- 新增 `harness/models/intent.py`：

```python
class DeliverySource(str, Enum):
    CALLER = "caller"       # 调用方 API 显式提交（方案 A）
    EXTRACTED = "extracted" # 系统从 intent 抽取（方案 B，过渡）

class DeliveryContract(BaseModel):
    contract_id: str = ""                 # 稳定 id，供确认/审计
    source: DeliverySource = DeliverySource.EXTRACTED  # C-01: 来源审计
    tool: str
    input: dict[str, Any]                 # 含 operation/path/content 等（D-03: content 存底但不参与匹配）
    after: list[str] = []                 # 预留时序依赖（L-01，本轮不校验）
    # 与 RequiredOperation 的关系：input 子集匹配规则复用 RequiredOperation.step_satisfies

class UserIntent(BaseModel):
    raw: str                              # 原始用户请求，Planner/Reviser 不可覆盖
    contracts: list[DeliveryContract] = []
    source_note: str = ""                 # 抽取说明/调用方说明，纯审计
```

- 事件类型：
  - 在 `RunStartedPayload` 扩展字段（`required_operations: list[dict]` + `intent_raw`），或新增 `INTENT_RECEIVED` / `DELIVERY_CONTRACT_REGISTERED`。**建议**：`RunStarted` 扩展 `intent_raw` 与 `contracts`（减少事件类型，INDEX §P3-H），若决策要独立事件则在 `PAYLOAD_MODEL_MAP` 注册。
  - fold 新增分支：`RunState.intent_raw`（原始请求，不可变）、`RunState.delivery_contracts`（列表）。
- `DagPlan` / `planner._parse_plan`：`required_operations` 继续由 LLM 产出，但 PlanGuardrail 覆盖检查逐步切换为对 `DeliveryContract` 判定（S06 完成切换）。

## 6. 实现要点

- `UserIntent.raw` 是**不可变受信数据**：`RunStartedPayload.intent_raw` 落事件；Planner/Reviser 的 `intent`/`user_intent` 字段是 LLM 重述，只能作为 Plan 描述，不得写回 `intent_raw`。
- 契约匹配复用 `RequiredOperation.step_satisfies`（结构性，不硬编码工具语义）。
- **禁止事项**：
  - 禁止让 LLM 写回/覆盖 `intent_raw`。
  - 禁止在本步让 Planner 生成 `DeliveryContract`（Planner 只生成 plan 与候选 required_operations）。
  - 禁止在本步做 API 透传（S07）。
  - 禁止删除 `RequiredOperation` 或 `DagPlan.required_operations`（S06 切换后再议）。

## 7. 验收标准

1. `pytest tests/test_fold.py tests/test_completion_gate.py tests/test_event_store.py` 全绿（现有回归）。
2. 新增测试 `tests/test_intent_contract.py`：
   - `UserIntent.raw` 保留原始文本，`_parse_plan` 不得修改它。
   - `DeliveryContract` 支持多条（D-05）。
   - `source` 区分 caller/extracted。
   - `RunStartedPayload` 扩展字段可序列化/反序列化（round-trip）。
   - fold：写入含契约的 RunStarted → `RunState.delivery_contracts` 正确折叠。
   - 空契约请求 → `contracts=[]` + 可标记 unverified（字段就位即可，判定逻辑在 S06）。
3. 事件 `PAYLOAD_MODEL_MAP` 覆盖新字段（若有新事件类型）。
4. 前端 OpenAPI 若已生成 `RunStarted`，需同步（`scripts/generate_openapi.py`）。

## 8. 这么做的后果

- **对 S06**：提供 `DeliveryContract` 作为完成门/覆盖校验的受信输入。
- **对 S07**：API 透传 caller 契约直接构造 `source=CALLER` 的契约。
- **对 S08**：Reviser 限权以 `intent_raw` + contracts 为不可变基准。
- **对 S09**：契约落事件后，终态判定可引用。
- **技术债**：`RunStarted` payload 扩展改变事件 schema → 旧 `.db` 不兼容时删除重建（本方案统一口径）。

## 9. 收尾自检清单

- [ ] intent_raw 不可变（Planner/Reviser 不能覆盖）
- [ ] 契约落事件 + fold 可折叠
- [ ] 多契约 + source 区分有测试
- [ ] PAYLOAD_MODEL_MAP 覆盖
- [ ] 现有回归全绿
- [ ] 在 INDEX 状态表更新 S05

## 10. 完成状态

| 项 | 值 |
|----|----|
| 状态 | 已完成 |
| 执行会话 | opencode（S05 会话） |
| 完成日期 | 2026-08-13 |
| 备注 | 新建 `models/intent.py`（DeliverySource/DeliveryContract/UserIntent，contract_id 稳定哈希）；`RunStartedPayload` 扩展 `intent_raw`+`contracts`；fold 折叠 `RunState.intent_raw`/`delivery_contracts`；`_ensure_run_started` 与 create_run 落 `intent_raw`。新增 `tests/test_intent_contract.py`（13 测试）。 |
