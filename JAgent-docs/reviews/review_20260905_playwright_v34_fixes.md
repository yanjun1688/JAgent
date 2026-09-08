# Architecture Review & Fix Roadmap — playwright-mcp 收敛 + v3.4 定稿

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-05 |
| **审查人** | Agent 导师（架构审查 + code review） |
| **范围** | ADR-011（浏览器后端收敛到 @playwright/mcp）+ v3.4（执行循环鲁棒性与上下文证据治理）未提交工作区 |
| **快照** | branch `feat/playwright-mcp-convergence` @ `a40e057` + 67 项未提交改动（32 已修改未暂存 + 28 未跟踪，0 staged / 0 stash） |
| **相关文档** | [ADR-011](../architecture/ADR-011_浏览器后端收敛与浏览器池.md) · [ADR-012](../architecture/ADR-012_受信执行态证据投影与输出引用.md) · [PRD_v3.4](../prd/PRD_v3.4_执行循环鲁棒性与上下文证据治理.md) · [DESIGN_v3.4](../Dev/DESIGN_v3.4_执行循环鲁棒性与上下文证据治理.md) |
| **状态** | ☐ 已记录；待后续实现 |
| **验收基线** | `ruff check harness tests scripts evaluation` 0 error；`pytest` 1395 passed / 2 skipped（快照期） |

> ⚠️ 行号均为快照期（2026-09-05）实测。后续实现前须重新核对当前工作区。
> ⚠️ 工作区**无任何提交**，playwright-mcp 收敛与 v3.4 两条线混在同一堆未提交改动中——见 [git 状态说明](#1-快照与背景)。

---

## 1. 快照与背景

- 全部实现以**未提交改动**形式存在（4 个本地分支同指 `a40e057`，分支间无差异；切分支不隔离脏工作区）。**任何 `reset --hard` / `clean -f` / stash 前必须确认已留存**。
- 审查期间实测健康度：`ruff check harness tests` ✅；`pytest` 1395 passed / 2 skipped ✅（运行前需 `pip install langfuse tiktoken`）。
- 审查结论：**受信边界设计成立、实现质量高，无"受信组件可被 Agent 绕过"的越权路径**；需修复的是少量确定性缺陷与若干待定稿的架构点。

---

## 2. 已验证成立的安全不变量（无需返工，勿改坏）

| 不变量 | 证据（快照期） | 结论 |
|---|---|---|
| 人工确认先于副作用 | `harness/tools/executor.py:349-426`（Step 5）先于 `:443+`（Step 7 invoke）；未确认只写 `CONFIRMATION_REQUESTED` 并返回 | ✅ `browser_file_upload` 等不会"先执行后确认" |
| 浏览器动作不被自动重放 | `BrowserMcpTool` 继承 `RetryPolicy(retryable_errors=[])`；`run()` 吞异常返回结构化失败；F-5 语义重试门 `executor.py:463-474` 中 `is_read_only_action(browser_*)=False` fail-closed | ✅ 双击/重复副作用不可达 |
| 同 Run 浏览器调用串行 | `harness/tools/browser_pool.py:64-66` `BrowserLease._call_lock`；`acquire` 同 Run 复用 lease | ✅ ReAct 与 DAG 路径均收敛到该锁 |
| per-tool 并发上限受信强制 | `harness/core/dag_executor.py:~90/:507` 全局 + per-tool 信号量；`max_parallel=1` 经 `base.py:142` 入 ToolDefinition | ✅ |
| RCE 级工具不可见/不可调 | `harness/tools/browser_policy.py:33` `browser_run_code_unsafe` 硬封；`browser_evaluate` env 门控（`HARNESS_BROWSER_ALLOW_EVALUATE`），注册期剔除 | ✅ |
| Agent 不可指定 profile/模式/路径 | `harness/models/browser.py` 全 env 配置，工具 schema 无路径参数 | ✅ |
| recovery 判定无 LLM | `harness/core/recovery.py` 纯函数；预算/白名单/分级机械判定，`validate_local_repair` fail-closed（:214-222） | ✅ |

---

## 3. 确认缺陷（待修，B 类）

| # | 严重度 | 位置（快照期） | 问题 | 修法 |
|---|---|---|---|---|
| B1 | High | `scripts/test_real_llm_flow.py:259/280/311` | `register_browser_tools` 仅在 `_browser_registry()` 函数内 import，`test_browser`/`test_browser_error` 直接调用 → playwright-mcp 已安装时运行即 NameError（ruff F401/F821） | import 上移到模块顶部 |
| B2 | High | `evaluation/run_eval.py:80/135`（import 在 `:178-181`） | `_ExecConfirmTool(BaseTool)`/`_HttpRateLimitTool(BaseTool)` 类语句先于 `BaseTool` 等 import 执行 → `import run_eval` 即 NameError（ruff F821×2 + F401） | import 上提至模块顶部 |
| B3 | Med | `scripts` + `evaluation` | `ruff check harness tests` 干净，但含 scripts/evaluation 时 6 error（即 B1+B2） | 修完 B1/B2 后全仓 0 error |

---

## 4. 架构决策与修复路线（C 类，后续实施依此执行）

### C1【核心】输出 offload 正确性 — inline-if-referenced

**缺陷**：事件只存占位符（`executor.py` `_offload_if_large` :109-178，写入 :598-614）。三条消费者中：
1. LLM 上下文 / Replay / 审计 → 摘要+ref：**offload 设计意图，保持**；
2. 幂等缓存命中 `executor.py:316-347` 返回 `payload.output`：被 offload 时返回占位符 ❌；
3. F-4 崩溃续跑 `recovery.rebuild_results_from_evidence` → `dag_executor.py:482-495` + `dag_vars.resolve_variables_in_input`：占位符被当真实值解析进下游工具参数 ❌。

**裁决**：占位符仅合法进入"观测路径"；**禁止进入"程序化消费路径"**（幂等命中回填、续跑证据重建、`$step.x` 依赖解析）。

**实施规格**：
1. dag 层加**纯受信函数** `is_step_output_referenced(plan, step_id) -> bool`：扫描全部 step 的 `.input`（嵌套 dict/list/字符串内联）中 `$sid` / `$sid_*` / `$sid.field` 引用。
2. `ToolExecutor.execute(..., persist_inline: bool | None = None)`；`None` 缺省 = **inline（fail-safe）**。`True` → 跳过 offload（事件内联全量）；`False` → 维持现 offload（超阈值落 blob）。用 grep 逐个裁决 `executor.execute(` 调用点：DAG 传 `is_step_output_referenced(...)`；ReAct/loop（纯观测）传 `False`；其它不传（缺省 inline）。
3. **占位符纵深防御**：`build_ref_placeholder` 返回**唯一命名空间形态**（顶层 `"blob_ref"` 键）；`dag_vars._resolve_ref` / `resolve_variables_in_input` 检测到该形态即抛结构化"requires hydration"错误，禁止静默替换（占位符自身 summary/ref 字段被显式引用除外）。
4. 一致性场景测试：被引用大输出不被 offload（续跑后下游拿真实值）；未引用大输出被 offload；幂等命中被引用步骤返回真实全量；dag_vars 对 blob_ref 占位符抛错不静默。
5. 文档：ADR-012 / DESIGN_v3.4 补"输出引用语义"一节（inline-if-referenced + 占位符仅限观测路径 + blob_ref 形态）。

### C2 profile 锁跨进程 TOCTOU（`harness/tools/browser_pool.py`）

- 现状：`_wait_profile_lock` 存在性轮询 + `_spawn_lease` `write_text` **非原子**写锁；同进程靠 `_guard`，跨进程（uvicorn workers/双 serve）会竞态双开同一 profile。
- 改：`os.open(lock_path, O_CREAT|O_EXCL|O_WRONLY)` **原子创建**；`FileExistsError` → 在 `lease_wait_ms` 内重试排队，超时抛既有 `BrowserLeaseUnavailableError`；成功写 holder 元数据（pid/run_id/ts）；释放 `unlink`；spawn 失败分支清理锁。
- 孤儿回收：`reap_stale_profile_locks` 以 **holder pid 存活探活**（`os.kill(pid,0)` 捕获 `ProcessLookupError`）为主判据 + 宽限期，原 24h 兜底保留。
- 测试：并发 acquire 同一 persistent profile 恰一成功；假 dead pid 锁可回收；live pid 锁不可回收。
- 文档：ADR-011 §3.2 注明原子锁 + pid 存活回收。

### C3 启动探测与受信总开关

- `BrowserConfig` 增 env `HARNESS_BROWSER_ENABLED`（默认 `1`）；`0` = 不建池、不 spawn、不注册浏览器工具（无浏览器部署退路）。
- discovery 结果**缓存到文件**（含 @playwright/mcp 版本指纹 + 工具清单）；版本指纹（根 `package.json`/package-lock 锁定版本）不变则直接加载缓存不 spawn；变了/缺失才一次性 `--isolated --headless` 探测；探测失败维持软降级 + install hint。
- 文档：ADR-011 §5 env 表补全（`HARNESS_BROWSER_OUTPUT_ROOT`、`connect_timeout_ms`、新增 `HARNESS_BROWSER_ENABLED`）；README 同步。

### C4 `browser_tabs` 分类 — 文档向代码收敛

- 保留代码（`READONLY_TOOLS` 不含 `browser_tabs` → 判 mutating/EXTERNAL，正确：该 MCP 工具可新建 tab）。改 ADR-011 §3.3：把 `browser_tabs` 移入"外部副作用"列并注明理由（read-only/probe 步骤不得触发建 tab）。

### C5 删除死代码 `browser_policy.whitelist_allows`

- `harness/tools/browser_policy.py:80-92` 全仓零调用者。删除；workspace 白名单唯一执行点收敛到 `guardrails.ToolWhitelistGuardrail.is_allowed`（若 browser 模块需复用则从此导入）。

---

## 5. 收尾小项

- `.gitignore` 增加 `.harness_outputs/`（仓库根运行产物，防误提交）。
- 可选：`scripts/integration_test.py` / `scripts/test_llm_dag.py` / `scripts/test_v07_integration.py` 三处重复 `_ScriptTool` 适配器抽公共模块（低优先）。

---

## 6. 实施顺序建议

1. B 类（A1/A2 纯 import 修复 + ruff 清零）——先行，低成本。
2. C1 文档先行（ADR-012/DESIGN）→ 纯函数 + executor 参数 + dag_vars 防御 + 测试。
3. C2 原子锁 + 并发/回收测试。
4. C3 开关 + 版本化缓存。
5. C4/C5 + 收尾小项。

## 7. 验收标准（实现完成后）

- `python -m ruff check harness tests scripts evaluation` → 0 error。
- `python -m pytest -q -p no:cacheprovider` → 全绿（先 `pip install langfuse tiktoken`）。
- C1 实现后逐条复核三消费者：占位符**不再进入任何程序化路径**（幂等命中、F-4 重建、`dag_vars`），仅存于观测路径。
- 不破坏既有断言：`tests/test_event_loop_compat.py` 对 `serve.py` `loop=` 的静态断言必须保留。
