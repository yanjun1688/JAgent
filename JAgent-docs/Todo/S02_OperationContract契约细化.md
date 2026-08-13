# S02 — OperationContract 契约细化（per-operation 工具契约）

> **所属层**: L2（Tool Layer 核心）
> **关联**: `models/tools.py` · `tools/file_op.py` · `tools/http_request.py` · `tools/browser_tool.py` · `tools/mcp_call.py`
> **决策编号**: C-04（引用校验粒度）· D-01（引用受信化，依赖 output_schema 与 `ref_allowed`）· 问题五（Tool Contract 粒度）
> **主控**: `JAGENT-2026-Completion-INDEX.md`

---

## 1. 前置依赖

- **S01** 已完成：决策编号 D-01/D-02/C-04 可引用。
- 交付物快照（上游）：
  - `harness/models/tools.py` 现有 `ToolDefinition`（`side_effects: list[SideEffect]` 按工具整体声明）、`RetryPolicy`、`SuccessIndicator`、`DependencyConstraint`、`Guardrail`。
  - `harness/tools/file_op.py:38-80` `FILE_OP_DEF`（`side_effects=[WRITE, DELETE]`）。
  - `harness/tools/http_request.py:48-98` `HTTP_REQUEST_DEF`（`side_effects=[EXTERNAL]`）。
  - `harness/tools/browser_tool.py` / `harness/tools/mcp_call.py` 同构声明。

## 2. 问题背景

日志证据（`harness.log:167`、`watchdog_verify_stderr.log`）：
- `file_op(read)` 被拒 `probe`，因为工具整体声明 `side_effects=['write','delete']`——**只读探测继承了写/删副作用**。
- `http_request(GET)` 被拒 `probe`，因为 `side_effects=['external']`——**只读 GET 被当成有外部副作用**。
- 根因：副作用、确认要求、幂等字段、probe 允许性全部按**工具**声明，无法区分 `file_op.read / write / delete`、`http_request.GET / POST`。

这不是 Probe 语义 bug，而是**契约粒度缺陷**：系统把工具视为原子单元，无法对 operation/参数做安全判定。必须把契约下沉到 operation 级别（问题五），并为 D-01 的引用校验提供 per-field `ref_allowed`（C-04）。

## 3. 为什么这么做

- 让 `file_op(read)` 的合法只读探测不触发误判；让 `http_request(GET)` 不被当成有副作用请求。
- 为 S04 的 `$step.output` 引用校验提供：每个 operation 的 `output_schema` + 每个 input 字段的 `ref_allowed`。
- 为将来 confirmation / idempotency 按 operation 差异化铺路（决策 D-02/D-03 的受信判定基础）。

## 4. 做之前先检查影响范围

- `ToolDefinition` 当前字段被以下位置消费，改动前必须 grep 全量调用点：
  - `harness/core/planner.py:159-164`（probe 校验读 `tool_def.side_effects`）→ 之后改按 operation。
  - `harness/tools/executor.py:156`（幂等键）、`:244`（confirmation）、`:352`（semantic）、`:459`（side_effects 日志）。
  - `harness/tools/guardrails.py`（SchemaGuardrail 读 `input_schema`）。
  - `harness/api/serve.py:132-138`（装配 `tool_defs`）。
  - 测试：`tests/test_tool_layer.py`、`tests/test_guardrails_v04.py`、`tests/test_probe_and_convergence.py`、`tests/test_scheduler.py`、`tests/test_completion_gate.py`。
- **兼容策略**：`ToolDefinition.side_effects` 字段**保留**（作为该工具任意 operation 的并集 / 兜底），新增 `operations` 映射**叠加**生效；找不到匹配 operation 时回退工具级声明。这样不破坏现有测试与外部引用。

## 5. 期望达到的目标

- 存在统一的 per-operation 契约模型，四个工具（file_op / http_request / browser / mcp_call）均声明。
- `file_op.read` 的 `side_effects=[]`、`probe_allowed=True`、`ref_allowed` 对 `path/content` 为 False。
- `file_op.write/delete` 的 `side_effects=[WRITE]`/`[DELETE]`、`probe_allowed=False`。
- `http_request.GET/HEAD` 的 `side_effects=[]`（仅只读）、`probe_allowed=True`；`POST/PUT/PATCH/DELETE` 的 `side_effects=[EXTERNAL]`、`probe_allowed=False`。
- 所有字段由 Pydantic 模型产出，无裸字典。

## 6. 实现要点

在 `harness/models/tools.py` 新增：

```python
class OperationContract(BaseModel):
    operation: str                       # 唯一标识，如 "read" / "GET"（大小写约定与工具 input 对齐）
    input_schema: JSONSchema = {}
    output_schema: JSONSchema = {}       # D-01: 引用校验的字段来源
    side_effects: list[SideEffect] = []  # 该 operation 实际副作用
    requires_confirmation: bool = False  # 该 operation 是否需要人工确认
    idempotency_key_fields: list[str] | None = None  # 覆盖工具级；None = 继承工具级
    probe_allowed: bool = False          # 该 operation 是否允许 probe 声明
    retry_policy: RetryPolicy | None = None  # 覆盖工具级
    ref_allowed_fields: dict[str, bool] = {}   # C-04: input 字段名 → 是否允许 $step.output 引用
    # 未列出的字段名默认 ref_allowed=False
```

在 `ToolDefinition` 增加：

```python
operations: list[OperationContract] = []   # 空 = 保持工具级行为（向后兼容）
```

配套辅助函数（放在 `models/tools.py` 或 `tools/registry.py`）：

```python
def resolve_operation_contract(tool_def: ToolDefinition, input: dict) -> OperationContract | None:
    """按 input 中的 operation/method/action 键解析出匹配的 OperationContract。
    找不到返回 None，调用方回退工具级声明。键名约定：file_op→operation，
    http_request→method，browser→action，mcp_call→operation。"""
```

**禁止事项**：
- 禁止删掉 `ToolDefinition.side_effects` 字段（破坏兼容）。
- 禁止在 `OperationContract` 里引入枚举之外的自由字符串副作用。
- 禁止让 LLM 决定 side_effects（受信声明，只由工具定义文件写死）。

## 7. 验收标准

1. `pytest tests/test_tool_layer.py tests/test_guardrails_v04.py tests/test_probe_and_convergence.py` 全绿（现有行为不回归）。
2. 新增单元测试（建议 `tests/test_operation_contract.py`）：
   - `file_op.read` → `side_effects=[]`、`probe_allowed=True`。
   - `file_op.write` → `side_effects=[WRITE]`、`probe_allowed=False`。
   - `file_op.delete` → `side_effects=[DELETE]`、`probe_allowed=False`。
   - `http_request.GET/HEAD` → `side_effects=[]`、`probe_allowed=True`。
   - `http_request.POST/PUT/PATCH/DELETE` → `side_effects=[EXTERNAL]`、`probe_allowed=False`。
   - `ref_allowed_fields`：`file_op.path`=False、`file_op.content`=False；至少一个工具存在 `ref_allowed=True` 的字段示例。
   - 解析函数对未知 operation 返回 None 且回退工具级声明。
3. OpenAPI/前端：若 `ToolDefinition` 出现在 API response 中，需重新生成前端类型（本步如无 API 变更可跳过，但需在文档记录）。

## 8. 这么做的后果

- **对 S03**：无直接影响。
- **对 S04**：提供 `output_schema` 与 `ref_allowed_fields` 作为引用校验依据。
- **对 S06**：完成门"操作+路径级"的匹配可精确到 operation。
- **技术债**：`browser`/`mcp_call` 的 operation 枚举若与输入 schema enum 不一致，解析函数需容错（记录在案）。

## 9. 收尾自检清单

- [ ] 全量受影响测试回归
- [ ] 四个工具都声明了 `operations`
- [ ] 兼容回退路径（无匹配 → 工具级）有测试
- [ ] Pydantic 模型无裸字典
- [ ] 在 INDEX 状态表更新 S02

## 10. 完成状态

| 项 | 值 |
|----|----|
| 状态 | 已完成 |
| 执行会话 | opencode（S02 会话） |
| 完成日期 | 2026-08-13 |
| 备注 | `models/tools.py` 新增 `OperationContract` + `resolve_operation_contract`；file_op/http_request/browser 声明 operations（read-only 探测放行、write/delete/外部副作用保留）；mcp_call 无静态 operation 判别键 → 空列表走工具级回退（技术债记录）；planner probe 校验按 operation；executor 幂等/确认按 operation 覆盖。新增 `tests/test_operation_contract.py`（15 测试）。 |
