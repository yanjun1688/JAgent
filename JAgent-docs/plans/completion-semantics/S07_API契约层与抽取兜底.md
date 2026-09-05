# S07 — API 契约层（caller 契约透传 + 抽取兜底）

> **所属层**: L6（接口层）+ L4（Kernel 辅助抽取）
> **关联**: `api/schemas.py`（`CreateRunRequest`）· `api/routes.py`（`create_run`）· `api/deps.py`（`start_run`）
> **决策编号**: D-02（方案 A：caller 显式契约）· C-01（provenance）· C-02（抽取为缓解）
> **主控**: `JAGENT-2026-Completion-INDEX.md`

---

## 1. 前置依赖

- **S05** 已完成：`DeliveryContract{source}` 模型与事件、fold。
- 交付物快照（上游）：
  - `harness/api/schemas.py:14-18` `CreateRunRequest`（intent/conversation_id/client_request_id/workspace_id）。
  - `harness/api/routes.py:239-307` `create_run`。
  - `harness/api/deps.py:111-177` `start_run`。

## 2. 问题背景

用户/调用方要求"创建 blackbox.txt，写入 hello harness blackbox，然后重新读取"。当前 API 只接受自由文本 `intent`，硬性交付要求只能靠弱 LLM 从文本里自己猜（会自洽地漏 write）。Review 方案 A（structured_input_requirements_review_20260812.md §3）提出让调用方以结构化方式声明硬性交付要求。D-02 冻结：API 增加可选字段；未提供时走抽取兜底。

## 3. 为什么这么做

- 硬性交付要求来自**调用方显式声明**（D1 声明式契约），不再依赖 LLM 理解用户意图——这是真正闭合"自洽漏报"的路径（C-02）。
- `provenance` 让审计能区分"调用方声明"与"LLM 抽取"，完成门对两者同样生效但语义强度不同。
- 前端聊天框暂不传（保持现状），不破坏现有调用方。

## 4. 做之前先检查影响范围

- `CreateRunRequest` 消费方：`create_run`、前端 `api/client.ts` 生成类型、`frontend/public/openapi.json`。
- `start_run` 签名：需要把契约从 `create_run` 传递到 Scheduler 上下文。
- `claim_client_request`（routes.py:270-279）：幂等请求重放时契约也要一致（payload 已含 RunStarted）。
- 抽取兜底（方案 B）：新增加独立抽取调用——`intent → list[DeliveryContract]`（`source=extracted`）。该调用是**非受信** LLM 输出，必须经受信校验（结构校验 + 与 plan 的双向覆盖，S06 已建）。
- 测试：`tests/test_api.py`、`tests/test_api_contract_robustness.py`、前端 conversation 测试。

## 5. 期望达到的目标

- `CreateRunRequest` 增加可选字段：

```python
class CreateRunRequest(BaseModel):
    intent: str
    required_operations: list[RequiredOperationInput] | None = None  # 调用方显式契约（方案 A）
    # ... 现有字段不变

class RequiredOperationInput(BaseModel):
    tool: str
    input: dict[str, Any]
```

- `create_run`：若提供 `required_operations` → 构造 `source=CALLER` 契约并随 RunStarted 落事件；未提供 → 触发抽取兜底（`source=EXTRACTED`），抽取失败则 `contracts=[]` + 全局 unverified（D-04）。
- 契约进入 Scheduler/完成门判定路径（S06 已实现消费侧）。
- OpenAPI / 前端类型同步生成。

## 6. 实现要点

- 抽取兜底实现（非受信，独立 prompt，与规划解耦——Review 方案 B）：
  - 新 `intent → contracts` 抽取函数（`core/planner.py` 或 `core/contract_extractor.py`），固定 schema，独立 prompt。
  - 抽取结果经受信结构校验：每条契约 `tool` 存在、`input` 含 `operation` 等必填键。
  - **禁止**：把抽取结果直接视为绝对可信输入；保留原始 `intent_raw`，记录抽取结果与来源（INDEX §验收项 1/2/3）。
- `RunStartedPayload` 已含 contracts 字段（S05），此处只接线。
- `claim_client_request` 幂等重放：同一 `client_request_id` 必须复用首次提交的契约，二次提交契约不一致 → 拒绝或忽略（用首次）。
- **禁止事项**：
  - 禁止在未提供 `required_operations` 时因抽取失败而拒绝 Run（D-04 兜底）。
  - 禁止让 Scheduler 伪造 caller 契约。
  - 禁止破坏旧请求（无新字段 → 默认 None → 抽取/空契约）。

## 7. 验收标准

1. 新增测试 `tests/test_api_contract_submission.py`：
   - POST 带 `required_operations=[{file_op write blackbox.txt}, {file_op read blackbox.txt}]` → RunStarted 事件含 `source=caller` 契约。
   - POST 不带 → 走抽取兜底（fake LLM 返回契约）→ `source=extracted`。
   - 抽取返回空 → `contracts=[]`、Run 可运行、unverified 标记。
   - 幂等 `client_request_id` 重放：二次提交不同契约 → 以首次为准。
   - 非法契约（未知 tool / 缺 operation）→ 400 或结构化拒绝。
2. 现有 `tests/test_api.py`、`tests/test_api_contract_robustness.py` 回归全绿。
3. `scripts/generate_openapi.py` 可重新生成前端类型，前端 `client.ts` 含新字段。

## 8. 这么做的后果

- **对 S08**：Reviser 不得修改 caller 契约（限权基准含 `source=caller` 的强约束）。
- **对 S12**：验收项 2（契约不依赖弱模型）由 caller 路径达成。
- **前端**：聊天框仍不传，后续若要让用户便捷声明，需前端改造（本轮不做，记录）。
- **技术债**：抽取调用增加一次 LLM 调用成本；抽取质量影响契约完整性（C-02 承认的缓解定位）。

## 9. 收尾自检清单

- [ ] caller 契约透传 + 落事件
- [ ] 抽取兜底 + 结构校验
- [ ] 幂等重放一致
- [ ] OpenAPI 前端类型同步
- [ ] 现有 API 回归全绿
- [ ] 在 INDEX 状态表更新 S07

## 10. 完成状态

| 项 | 值 |
|----|----|
| 状态 | 已完成 |
| 执行会话 | opencode（S07 会话） |
| 完成日期 | 2026-08-13 |
| 备注 | `CreateRunRequest.required_operations`（RequiredOperationInput）+ `core/contract_extractor.py`（ContractExtractor：固定 prompt + 与 caller 共用工具级受信校验，无效丢弃）；非法 caller 契约在 RunStarted 前返回 400；无 caller 时先写 RunStarted，再以有界抽取追加 `DeliveryContractsResolved`；claim_client_request 幂等重放复用首次契约；OpenAPI + frontend schema.ts/client.ts 同步。 |
