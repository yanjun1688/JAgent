# Review — 全项目死代码扫描（`_is_stop_signal` 等残留清扫）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-08 |
| **类型** | code review 清扫（"定义了但未使用"重构残留，第三次同类） |
| **分支** | `review/code-review-fixes`（自 `main` c7c6230） |
| **相关文档** | [DESIGN_v3.4 §11.8](../Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md) |
| **状态** | ✅ 已清扫 + 全量验证通过 |

---

## 1. 触发与范围

Review 指认 `harness/core/agent_kernel.py::_is_stop_signal`（:25）定义但从未被调用，`_consume_response`
内联同谓词（`_STOP_MARKER in content or "ANSWER:" in content`）。这是同批审查中第三次"定义即死"辅助残留
（前两次：`recovery._TOOL_UNAVAILABLE_PATTERNS`、`EventStore._seq_locks`）。应要求做**全项目**私有
符号死代码扫描（AST 提取 `harness/` 单下划线 def/常量，全仓含注释/docstring 的词计数 =1 为候选，再逐个人工复核）。

## 2. 已删除（均有"零引用 + 人工复核"证据）

| 项 | 位置 | 证据/说明 |
|---|---|---|
| `_is_stop_signal` | `agent_kernel.py` | review 指认；内联判定仍在 `_consume_response`，行为不变 |
| `_WEAK_TOOL_SIGNALS` | `scheduler/classify.py` | 常量注释仍留（描述弱词语义）；`intent_requires_tools` 只用 `_TOOL_SIGNAL_PATTERNS`，无消费者 |
| `BrowserLease._process` | `tools/browser_pool.py` | dataclass 遗留状态槽：从不写入/读取（spawn 走 mcp `stdio_client` 自身管理进程）；构造均为关键字传参，删字段安全 |
| `_parse_layer_info` | `scripts/test_llm_dag.py` | 惰性/断头辅助（体为丢弃计算），无引用 |
| `_EXEC_CONFIRM_DEF` / `_HTTP_RATE_LIMIT_DEF` | `evaluation/run_eval.py` | 旧 ToolDefinition 风格残留；工具实际以 `_ExecConfirmTool()` / `_HttpRateLimitTool()` 类注册（run_eval.py L224-225），常量逐字段重复类定义 |

## 3. 清扫中发现的既有缺陷（非本轮引入，已一并修复）

`evaluation/run_eval.py` 顶层 harness import 块（`EventStore`/`BaseTool`/`ToolExecutor` 等）位于
`_ExecConfirmTool` / `_HttpRateLimitTool` 两个**引用 `BaseTool` 的类定义之后** → 模块 import 即
`NameError`，eval 入口点实际不可运行（HEAD 亦如此，仅被 ruff F821 暴露）。修复：eval-only 工具块整体移到
import 块之后；现在 `python -c "import evaluation.run_eval"` 成功。

## 4. 误报甄别（未删除）

- `models/intent.py::_ensure_contract_id` —— 是 `@model_validator(mode="after")` 装饰的**活校验器**；
  装饰器应用不二次出现函数名，朴素词计数误报为死代码。**保留**（提醒：此类扫描必须人工复核，禁机械删除）。

## 5. 验证

- 死代码扫描重跑：true positive 0（唯一候选为 §4 误报）。
- `pytest` 全量：**1422 passed / 2 skipped**（删除前后一致 → 无行为漂移）。
- `ruff check harness tests scripts evaluation`：**0 error**（此前 evaluation 不在门禁内）。
