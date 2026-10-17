# 差异报告：引用契约收敛为「字段角色单一真相源」（A 根因的通用解）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-12 |
| **类型** | 差异报告（实现前）→ 随后 TDD |
| **来源** | 真实 E2E run `19538cb0` 失败分析（根因 A） |
| **关联决策** | ADR-008 / [S02 OperationContract](../plans/completion-semantics/S02_OperationContract契约细化.md) / [S04 步骤输出引用契约](../plans/completion-semantics/S04_步骤输出引用契约.md) 的 D-01 · C-04 |
| **状态** | 已实现（TDD，2026-09-12）；真实 E2E 已验证 |
| **范围** | 仅根因 A（引用契约）。根因 B（终态/预算解耦 + serial 完成门）单独并入 R1，不在本报告 |

---

## 1. 触发证据（真实运行）

Run `19538cb0`：Planner 将 `$s1.body` / `$s2.body` 写入 `file_op.content`，被 PlanGuardrail 连续 3 次拒绝
（`field 'content' does not allow $step.output references (ref_allowed=False)`）→ 降级 serial → 虽有真实执行，
但流程终态失败。核心不是 `file_op` 一个字段，而是**字段级契约信息未对 Agent 可见、且散落在并行结构中**。

---

## 2. 现状（代码事实）

**(a) 引用政策靠并行字典手工维护，且只被受信层消费**

- `OperationContract.ref_allowed_fields: dict[str, bool]`（`harness/models/tools.py:104`），缺省 `False`（`:108-109`）。
- 各工具在 `@operation(..., ref_allowed_fields={...})` 逐字段手写：`file_op.py:60,64,71,79,84`、`http_request.py:131,147,164-182,273-308`、`fetch_output.py:50`。
- 强制点：`harness/core/planner/output_refs.py:124-129`（PlanGuardrail 内），只认 `target_op.ref_allowed(top_field)`。

**(b）契约信息未投影给 Planner**

- `build_tool_schemas`（`system_prompt.py:366-378`）与 `build_tool_descriptions`（`planner/prompts.py:33-61`）
  都只读 `input_schema` 的 `properties/required/enum`，**从不读 `ref_allowed_fields`**。
- Planner 因此不知道哪些字段可放 `$ref`，只能靠「被拒→重试」盲试 → 必然撞墙（本次 3 次全撞）。

**(c）政策自相矛盾（缺一致性轴）**

| 工具.字段 | 现 `ref_allowed` |
|---|---|
| `http_request.body` / `headers` | `True` |
| `file_op.content` | `False` |

同为「数据承载字段」却取值相反，说明 C-04 的取值缺乏统一判据。

**(d）结论**：`input_schema`（类型/必填）、`ref_allowed_fields`（引用）、`idempotency_key_fields`（幂等）、
`scope_targets`（作用域）是**四套并行字段引用**；每加一个工具/字段就要同步多处，漏一处即出 bug。这是臃肿来源。

---

## 3. 目标设计（单一真相源 + 派生）

### 3.1 最小契约单元 = 字段，角色内联在 `input_schema`

在工具级 `input_schema.properties.<field>` 增加厂商扩展键 `x-role`：

```python
class FieldRole(str, Enum):
    DATA = "data"          # 数据汇：允许 $ref（值由受信执行器解析）
    AUTHORITY = "authority"  # 授权/寻址汇：禁止 $ref（path/url/tool_name/命令/凭据）

FIELD_ROLE_KEY = "x-role"
DEFAULT_FIELD_ROLE = FieldRole.AUTHORITY   # fail-closed
```

缺失即 `AUTHORITY`（安全默认）。

### 3.2 一条受信策略（污点/数据流）

> `$ref` 的值源自非受信工具输出（可能被外部内容污染），**只能流入 DATA 字段**；**不得流入 AUTHORITY 字段**。

`ref_allowed(field) := field_role == DATA`。guardrail / executor / planner 视图全部由此派生。

### 3.3 三个消费方，零逐工具代码

| 消费方 | 变化 |
|---|---|
| `parse_output_refs`（受信强制） | 从字段角色派生允许性，替换读 `ref_allowed_fields` |
| `build_tool_descriptions`（非受信视图） | 每个参数自动标注 `[ref: allowed]` / `[ref: forbidden]` → 补上对 Planner 的契约可见性 |
| `build_tool_schemas`（发往 provider） | 递归剥离 `x-*` 厂商键，避免污染 function-calling |

### 3.4 角色映射（现有工具，统一判据）

| 工具.字段 | 角色 | ref |
|---|---|---|
| `file_op.content` | DATA | ✅（本次变更：由 False→True） |
| `file_op.path` | AUTHORITY | ❌ |
| `http_request.body/headers` | DATA | ✅（不变） |
| `http_request.url` / `method` | AUTHORITY | ❌ |
| `mcp_call.arguments` | DATA | ✅ |
| `mcp_call.tool_name` | AUTHORITY | ❌ |
| `fetch_output.ref` | AUTHORITY | ❌ |

### 3.5 Planner 提示词

`_PLAN_PROMPT` 增加一句：工具参数中 `[ref: allowed]` 字段可写 `$step.field`，`[ref: forbidden]` 必须字面量。

---

## 4. 逐文件差异

| 文件 | 现状 | 目标 |
|---|---|---|
| `harness/models/tools.py` | `OperationContract.ref_allowed_fields` + `ref_allowed()` | 新增 `FieldRole` / `FIELD_ROLE_KEY` / `field_role()`；删除 `ref_allowed_fields` 与其入参；`ref_allowed` 由角色派生（或直接由 `field_role`） |
| `harness/tools/base.py` | `@operation(..., ref_allowed_fields=...)` | 移除该参数（角色改在 schema 声明） |
| `harness/tools/file_op.py` | `ref_allowed_fields={"path":False,"content":False}` | schema 属性加 `x-role`：`path=authority`、`content=data` |
| `harness/tools/http_request.py` | 多条 `ref_allowed_fields` | schema 属性加 `x-role`：`url/method=authority`、`body/headers=data` |
| `harness/tools/mcp_call.py` | 见代码 | `tool_name=authority`、`arguments=data` |
| `harness/tools/fetch_output.py` | `ref_allowed_fields={"ref":False}` | `ref=authority` |
| `harness/core/planner/output_refs.py` | 读 `ref_allowed_fields` | 读 `field_role(target_def.input_schema, top_field)` |
| `harness/core/system_prompt.py` | `build_tool_schemas` 原样透传 schema | 递归剥离 `x-*` 键 |
| `harness/core/planner/prompts.py` | 工具描述不含 ref 能力 | 参数行追加 `[ref: allowed/forbidden]` |
| `tests/test_output_ref_validation.py` | 断言 `file_op.content` 禁引用 | 改为断言 `content` 允许、`path` 拒绝 |
| `tests/test_operation_contract.py` | 断言 `op.ref_allowed("content") is False` | 改为角色派生断言 |
| 新增 `tests/test_field_role_ref_contract.py` | — | TDD 主测试：角色默认/派生、描述投影、schema 剥离、guardrail 行为 |

---

## 5. 行为变更（显式标注）

- **BEHAVIOR CHANGE**：`file_op.content` 由「禁止 `$ref`」改为「允许」——这是修复本次死结的核心；`path` 仍禁止。
- 其余工具角色与现值等价（仅声明方式改变），不产生行为变化。
- 引用校验的**强制强度不变**，只是取值来源从并行字典改为字段角色。

---

## 6. 明确不做（防膨胀 / 防跑偏）

- **不做** `idempotency_key_fields` / `scope_targets` 的收敛（列为后续独立项；避免一次抽象过大）。
- **不做** 根因 B（终态/预算解耦、serial 完成门、无进展守卫）——并入文档 R1，另立任务。
- **不做** 迁移兼容层（owner 明确：不考虑迁移）。
- **不引入** 新流程/编排；角色派生是受信契约层的单点，不是 Workflow Engine。

---

## 7. TDD 计划（顺序）

1. `test_field_role_ref_contract.py`：
   - 缺失 `x-role` → `AUTHORITY`（fail-closed）；`x-role=data` → DATA。
   - `field_role` 对不存在字段/非 dict schema 的行为。
2. 更新 `test_output_ref_validation.py` / `test_operation_contract.py` 到新判据（红）。
3. 实现 `models/tools.py` + `base.py` + 4 工具 schema。
4. 实现 `output_refs.py` 派生 + `build_tool_schemas` 剥离 + `build_tool_descriptions` 投影。
5. 全绿后跑 `ruff` / `mypy` / `pytest` 全量。

---

## 8. 风险

- 若某工具作者忘记给 DATA 字段标注 `x-role`，该字段默认 AUTHORITY → `$ref` 被拒（fail-closed，安全但可能再次出现本次症状）。**缓解**：`build_tool_descriptions` 投影使能力对 Planner 可见；契约测试覆盖四工具。
- `x-role` 是 JSON Schema 合法厂商扩展，但需在 `build_tool_schemas` 剥离后才发往 provider（已在计划内）。

---

## 9. 实施证据（2026-09-12）

- **代码落地**：`harness/models/tools.py` 的 `FieldRole` / `FIELD_ROLE_KEY="x-role"` / `field_role()` + `ToolDefinition.ref_allowed()` 派生；删除并行 `ref_allowed_fields`；`@operation` 移除该入参；4 工具 schema 加 `x-role`；`output_refs.py` 改读字段角色；`build_tool_schemas` 剥离 `x-*`；`build_tool_descriptions` 投影 `[ref: allowed/forbidden]`。
- **测试**：新增 `tests/test_field_role_ref_contract.py`（角色默认/派生、schema 剥离、描述投影、guardrail 行为）；更新 `test_output_ref_validation.py` / `test_operation_contract.py` / 夹具。
- **全量回归**：`pytest -q` → **1521 passed, 2 skipped**；`ruff check harness tests scripts` / `mypy harness` 全绿。
- **真实 E2E**：Run `f83e98ee`（httpbin 8 步采集 → 写 `collect.txt` → 读校验 → append → 再读 → 汇报）`status=completed`（40 事件，Planning 模式，无 serial 降级）。对比修复前 Run `19538cb0` 因 `file_op.content` 撞 `ref_allowed=False` 降级 serial 并失败。
- **BEHAVIOR CHANGE 已生效**：`file_op.content` 由禁止 `$ref` 改为允许。

---

## 10. 文档同步（2026-09-12）

本报告方向已回写至契约文档：`ADR-008`（D-01/C-04）、`S02`（§5/§6/§7/§8/§10）、`S04`（§1/§3/§5/§6/§7/§9/§10）、`ADR-010`（§6.1/§6.2/§13）。
