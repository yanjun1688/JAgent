# Review — 契约抽取重试语义 / 压缩冷却解耦 / 高压缩档原文保留（review ③④⑤）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-09 |
| **类型** | code review 修正（reviewer 指认 3 点，全部 TDD 落地） |
| **分支** | `review/code-review-fixes` |
| **相关文档** | [DESIGN_v3.4 §11.9](../Dev/DESIGN_v3.4_执行循环韧性与上下文证据治理.md) |
| **决策** | ③/④/⑤ 均采纳 reviewer 建议方向（拍板后实现，见下） |
| **状态** | ✅ 实现 + 审计通过 |

---

## ③ ContractExtractor.extract：重试只覆盖 LLM 异常，不覆盖 JSON 解析失败

**核实**（属实）：`contract_extractor.py` 原 `extract` 仅在 `llm.chat` 异常时 `continue`；
`_parse` 返回（含空列表 / 解析失败 / 结构校验全丢）即 `return`，`max_retries` 对格式错误无效。
且 `_parse` 把"合法空（`required_operations: []`）"与"解析失败/无效"混为同一 `[]`，无法区分。
对照 `Planner.plan/revise`：解析失败与 guardrail 失败都会把 `last_error` 经 `retry_prompt` 塞回重试。

**裁决（A）**：对齐 Planner 语义——
- 解析失败（非法 JSON / 非对象 / 缺或非 list `required_operations`）、以及列表非空但全部被结构校验
  丢弃 → 把**具体原因**经 `_RETRY_HINT` 反馈给模型重试（镜像 parse+guardrail 双反馈）；
- `required_operations: []`（无硬性交付）= 合法决策 → 立即返回，不重试（镜像 revise 空 steps=完成）；
- 部分有效 → 直接返回有效子集；
- 预算用尽 → `[]` + D-04（unverified，不阻断 Run）；`asyncio.wait_for` 总超时上限不变。

**改动**：`_parse` 返回 `(contracts, error)` 区分三元结局；`_validate` 返回 `(contract, reason)`
携带丢弃原因；`extract` 按 attempt 构造 messages 并在失败时追加 `_RETRY_HINT`。工具名随反馈重发。

**测试（RED→GREEN）**：`tests/test_contract_extractor.py` 6 例 —— 解析失败重试带反馈、预算用尽
返回空不抛、合法空不重试、全丢带原因重试、全丢耗尽返回空、部分有效单次返回。既有
`test_api_contract_submission` / `test_replay_api` 无回归。

## ④ ContextManager.checkpoint_interval 身兼两职（书签节拍 vs 压缩冷却）

**核实**（属实）：`try_checkpoint`（`iteration % checkpoint_interval`）与 `maybe_compress`
（`cooldown = checkpoint_interval`）共用一值；且现役测试已"借用"该参数调冷却
（如 `checkpoint_interval=1` 只为让第二次压缩不被冷却挡），证明两语义本非一回事。

**裁决（A 彻底拆开）**：`checkpoint_interval` 只管书签节拍；新增 `compression_cooldown_iterations`
（默认 10）只管压缩冷却。默认值相同 → 未迁移调用点行为不变；迁移面：
- 语义确为"冷却"的测试 → 改传 `compression_cooldown_iterations`（cooldown pin、CW-C2、
  scheduler 两处集成、manager 两处集成）；
- 两语义都想要的调用点 → 镜像双参数（test 集成 + scripts/serve 镜像 11 处，逐位保持旧行为）；
- 纯书签语义的测试/默认路径 → 不动。

**测试（RED→GREEN）**：新增 `TestCompressionCooldownDecoupled`（checkpoint_interval=2 +
cooldown=10_000：iteration 2 写书签成功，而压缩在冷却内被挡）证明解耦成立。

## ⑤ Importance score 只在 lazy_clear 档真正被使用

**核实**（属实，含严重性修正）：`_score_event_importance` 只驱动 `_select_low_importance_events`
（lazy_clear，清 ≤0.2）；`_archive_episode`（keep=2）与 `_emergency_compact`（keep=3，docstring
明言 "ignore importance"）纯按时间位置折叠，高分失败项（0.8）只要不在最近窗口就与普通内容一起进摘要。
**注意**：压缩只修剪喂 LLM 的工作视图；原始事件仍在 Event Store，证据经 ADR-012 投影可重建，
故这是"摘要后工作上下文保真度"问题，非持久证据丢失。

**裁决（A 高重要性原文保留进 Episode，带上限）**：`Episode` 新增 `preserved_excerpts: list[str]`
（默认 []，旧事件反序列化兼容）；`_generate_episode`（archive/emergency 公共路径）对归档内容按
`(score ≥ importance_preserve_threshold(0.6), score↓, seq↑)` 选 ≤ `preserved_excerpt_max_items`(6)
条、每条 ≤ `preserved_excerpt_max_chars`(600) 字原文片段写入 Episode —— 系统侧确定性保留，**不依赖
LLM 摘要**（结构化路径亦然）。`agent_kernel._build_messages` 与 `planner/prompts` 渲染
"Preserved verbatim excerpts"。上限防压缩不收敛；raw 事件不受影响。

**测试（RED→GREEN）**：`TestPreservedExcerpts` —— 阈值/上限参数存在；直接驱动两档
（episode_archive keep=2 与 emergency_compact keep=3）后归档 Episode 携带旧失败 error 原文、近期
成功 output 不保留；`_generate_episode` 语义：决策 thought 保留 / 普通 thought 与成功 output 不保留；
kernel 工作视图渲染 pin（"Preserved verbatim excerpts" + 原文出现在 think 消息）。

---

## 审计

- 全量 `pytest`：**1433 passed / 2 skipped**（基线 1422 + 净增 11）。
- `ruff check harness tests scripts evaluation`：**0 error**。
- 默认行为零漂移核销：
  - ④ 未迁移路径 cooldown 默认值=10=旧 checkpoint_interval 默认；scripts/serve 镜像保证一致；
  - ⑤ Episode 旧事件缺 `preserved_excerpts` → `default_factory=list` 兼容；无 ≥ 阈值归档项时列表恒空，
    不影响既有 compression token/keep_recent pin；
  - ③ 失败兜底路径（预算用尽 → []）与超时上限不变，`classify.py` 调用方无改动。
- 工具注册路径与 eval 语义未触碰（本批修复均不涉及 tool 注册）。
