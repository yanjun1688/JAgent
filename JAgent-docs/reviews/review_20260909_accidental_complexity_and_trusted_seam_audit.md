# Review — 偶然复杂度与受信接缝观察（v3.5 问题定义的代码实证）

| 属性 | 值 |
|---|---|
| **日期** | 2026-09-09 |
| **类型** | 架构 code review（只记录问题与证据，**不含修复方案、不排序、不拍板**，等 owner 裁决） |
| **范围** | `harness/` 全后端 + `frontend/src` 契约面 + 测试设施；基线 1433 passed / 2 skipped |
| **触发** | `PRD_v3.5_复杂度收敛与契约闭环_问题定义与参照.md` 列了问题 A–F，本 review 负责把它们落到**具体 file:line**，并补录 PRD 未覆盖的实证 |
| **相关** | [PRD v3.5](../prd/PRD_v3.5_复杂度收敛与契约闭环_问题定义与参照.md)、`reviews/review_20260908_*.md` 批次 |
| **状态** | 草案，待 owner 决策。本文不评估"要不要改/怎么改" |

---

## 0. 总结论

1. **护城河当前成立，无已知 P0**：append-only 物理触发器、`fold_events` 唯一投影、`step_evidence` 压缩不裁剪、工具注册期 fail-closed、完成门永不假绿——经逐项核对均在生效。生产装配只有 planning 一条调度器（`api/deps.py:160`），因此下文本应在 serial 轨道上的若干缺口目前不直达生产流量。
2. **PRD 的判断"复杂度长在接缝处"被代码证实，且比 PRD 记载的更具体**：同一条受信判定（最典型："工具是否存在/允许"）在代码中有 **8 份实现**；LLM 输出的消费外围有 **6+1 条独立封装**，重试/超时/解析失败语义各不相同；token 口径有 **3 套且互不相通**。
3. **另发现 7 项 PRD 未记载的现存缺陷/静默漂移**（R1–R6、R10–R11），其中两项属"受信强制在某条路径上实际缺位或被绕过"，一项属"受信拒绝本身不可观测"——与本项目"可观测、可追溯"的核心诉求直接相关。
4. 本文刻意不做功能裁剪建议。所有观察的裁、留、并、拆均留给 owner。

---

## 1. 分轨道问题（serial / planning 双轨接缝）

### R1 serial 完成路径绕过终态守卫，且该轨道完全没有完成门

**证据**：

- `harness/core/scheduler/loop.py:182-188`（direct_answer）与 `:193-198`（LLM 选 stop）均**直接** `self.store.append_event(run_id, EventType.RUN_COMPLETED, RunCompletedPayload(result_summary=...))`，不经基类 `_complete()`。
- 基类唯一受信完成口 `BaseScheduler._complete()`（`scheduler/base.py:644-684`）会做两件事：① `_resume_lock` 内的终态幂等守卫（`:653-658`）；② 在 `RunCompletedPayload` 上携带机械/交付双维证据 `all_normal / unmet_step_ids / deliverable_met / deliverable_status / deliverable_summary`（`:671-684`）。serial 直写路径这两者都没有——折叠出的 `completion_evidence` 为默认值。
- grep 全文件确认：`loop.py` **从不引用** `CompletionVerdict`，也不读 `state.delivery_contracts`（对比 planning 在 `plan.py:30,355-356,390,901-932` 的调用）。即 API 提交的 caller 显式契约 `required_operations`（`api/routes.py:275,333`）在 serial 轨道**不构成完成门**——LLM 说停即 `RUN_COMPLETED`。
- 同类绕过还有 serial 确认循环中直写 `RUN_PAUSED`：`scheduler/base.py:1005-1009`，未走带 `allow_after_terminal` 守卫的 `_append_run_event`（`base.py:589-623`）。

**缓解事实（为什么不是 P0）**：生产 `api/deps.py:160` 只装配 `PlanningExecutorScheduler`；serial 由 evaluation（`evaluation/run_eval.py:211,258`）、scripts 与 planner 失败 fallback（见 R2）使用。

**性质**：两条"完成"路径的受信强度不对等。这不是文档问题，是代码里真实存在第二条弱完成路径。

---

### R2 planner 失败回退 serial 是"嵌套新调度器实例"，存在三个接缝缺陷

**证据**：`scheduler/plan.py:1144-1161`，planner 重试耗尽后在当前 asyncio.Task 内 `new` 一个独立 `AgentLoopScheduler`（独立对象、独立控制字典），复用同一 store/executor/config，但 `await serial.run(run_id, intent)`。

由此产生：

1. **双重 watchdog / run deadline 预算翻倍**：内层 `serial.run()` 再次执行 `base.py:243-245` 的 deadline 注册与 `asyncio.wait_for`（`base.py:232-331`），外层 planning task 的那份仍挂着。
2. **fallback 期间外部控制信号断裂**：API 层按 run_id 持有的是**外层** planning 实例（`deps.py:160-176,198-199`；`routes.py:443,475,557,643` 全部 `api._schedulers.get(run_id)`）。pause/resume/confirm 解析的是外层 `_pause_events/_confirm_events`（`base.py:140-141`），内层 serial `_wait_for_resume()`（`base.py:1098-1128`）等的是**内层自己的 Event**，外层 resume 唤醒不了内层，只能等确认超时（`base.py:1117-1122`）失败收敛。注意 RUN_RESUMED 事件虽写入了事件流，但内层阻塞等待不消费事件流。
3. **cleanup / reaper 双层执行**：内层 `run()` 的 finally（`base.py:288-331`）先 pop monitor 与 run 缓存，外层 finally 再跑一遍；内层未传 `run_end_cb`（`plan.py:1148-1160` 用默认 no-op），`_cancel_and_reap`（`base.py:555-587`）两层各调一次。

**性质**：这是"同 run_id 上两个调度器实例"的嵌套 hack，不是状态机层的统一。R1 的弱完成路径也会经 fallback 在生产 run 中被实际走到。

---

### R3 压缩三档阈值是"算了不用"的死配置

**证据**：

- `harness/core/context_manager.py:98-100` 根据构造参数算出 `compression_threshold / emergency_threshold / lazy_clear_threshold`（token 绝对值）。
- 实际决策 `maybe_compress` 完全不读这三个属性，而用硬编码字面量：`<= 0.5` 跳过（`:148`）、`> 0.9` emergency（`:162`）、`> 0.7` archive（`:167`）、其余 lazy（`:172-176`）。构造参数只出现在日志（`:142-144`）。
- 阈值参数 `compression_threshold_ratio / emergency_threshold_ratio / lazy_clear_ratio` 的外部传入因此**不改变行为**。`tests/test_context_window.py:95,111` 传 `compression_threshold_ratio=0.8` 等数值以为可配，实际无效。
- 同类硬编码：lazy 裁剪重要性阈值 0.2 出现在 `:458` 与 `:467`，importance 分值 0.7/0.5/0.8/0.6/0.2 在 `:396-405`，均不引用单一配置。

**性质**：参数化是假象；调参者无法经公开构造参数改变分档行为。

---

### R4 token 计量三套口径互不相通，且部分 LLM 调用在 run deadline 预算之外

**证据**：

| 口径 | 位置 | 算法 / 覆盖 |
|---|---|---|
| monitor 异常检测 | `monitoring/run_monitor.py:313` | `int(len(thought_text) * 0.25)`，**只数 AGENT_THOUGHT.thought 文本**，不数 tool_results、不数 episode summary/preserved excerpts；阈值 `max_tokens(5000)*0.8`（`:49-50,315`） |
| 压缩触发 | `context_manager.py:103-113` | 可插拔 `TokenCounter`（tiktoken→启发式回退，`token_counter.py`），数 thought_history + tool_results，**同样不数 summary/excerpts**；阈值 128000×0.7（`:35-37`） |
| 面板/查询记账 | `analysis/service.py:71,238`、`api/query.py:212` | 对事件 payload 的 `token_count` 字段求和 |

- planning 轨道写 `AGENT_THOUGHT` 时 `token_count=0` 共 **4 处**：`scheduler/plan.py:133,349,382,919`（占位 thought 与 answer thought）。因此 analysis 面板的 token 汇总在生产主路径上恒为 0。
- 0.25 字符系数在 `token_counter.py:50,98` 与 `run_monitor.py:313` 各自独立写了一份。
- 另一类问题——**预算覆盖不统一**：只有走 `_phase_call` 的调用受 Q-07 run 总 deadline 约束（`base.py:523-553`）。serial 的 `kernel.think`（`loop.py:158` 直连，未包 `_phase_call`）与 episode 摘要 LLM 调用（`context_manager.py:565` 直连 `llm_client.chat`，无超时、无 phase budget）都在 run deadline 之外，仅靠 `llm_client.py:116` 的 httpx 120s 硬超时兜底。

**性质**：PRD C1（启发式估算、漏算 summary）属实；本 review 追加"三个口径互相不通 + 主路径记账为 0 + watchdog 对部分 LLM 调用不可见"两点。

---

### R5 `DagExecutor.execute()` 的 PlanGuardrail 复检在生产路径不执行

**证据**：

- `core/dag_executor.py:107-121` 的公共 `execute()` 入口在执行前跑 `self._guardrail.validate(plan)`，失败写 `PLAN_FAILED`。
- grep 全仓（排除定义）确认：生产调度器调的是 `execute_layer()`（`scheduler/plan.py:516`；`dag_executor.py:191-210` 直接进单层执行，**无整计划复检**）。`execute()` 的调用方只有测试：`tests/test_dag_executor.py`（9 处）、`test_dag_per_tool_semaphore.py`（2）、`test_semantic.py`（2）、`test_step_normal_gate.py:97`、`test_traceability_hooks.py:36`。
- 生产路径的执行时防线实际只剩单步注册表双查 `dag_executor.py:501-505`（`get_tool_def`/`get_tool_fn`，未知工具 → StepResult FAILED）。计划级规则（probe 合法性、`$ref`、dangerous_with、DAG 结构等）在执行时没有再跑一次。

**缓解事实**：计划/修订生成时 PlanGuardrail 已在 `planner/agent.py:130,204` 与合并后复检 `plan.py:631-637,830-837` 跑过；`revision_guard.py:310-313` 注释也声明"新调用点不得只跑 invariants 跳过 PlanGuardrail"。

**性质**：PRD B3 所述"revision invariants → PlanGuardrail → dag_executor 三层兜底"中的**第三层在真实流量路径上不存在**，只在测试里被覆盖。纵深防御的层数比文档宣称的少一层。

---

### R6 PlanGuardrail 违规不落任何事件——受信拒绝不可观测

**证据**：

- 计划生成/修订时的受信违规处理：错误字符串拼进 `retry_prompt` 塞回 LLM（`planner/agent.py:130-134,204-212`）；合并后复检失败直接 `_fail()`，原因进 `RUN_FAILED.final_error` 文本（`scheduler/plan.py:631-637,830-836`）；修订不变量/退化守卫的拒绝也只是反馈文本（`revision_guard.py:324-346`、`revision_invariants.py:86-95`）。
- grep 确认这些路径**不写任何事件**。`GUARDRAIL_TRIGGERED` 仅由工具层 `GuardrailRunner` 拦截路径写出（`tools/executor.py:281-292`）。
- 后果：计划被受信组件拒绝了几次、拒绝原因是什么、占修订轮次多少，在事件流 / Replay / analysis 面板中**无结构化记录**，只能人工读日志或从最终 RUN_FAILED 文本推断。工具层护栏有完整的"拦截→事件→fold→统计→前端"闭环（`fold.py:297-307`、`analysis/service.py:196-225`），计划层护栏没有对等物。

**性质**：这是两套护栏到可观测性的通道不对等，直接影响"受信决策可追溯"这一核心设计目标的覆盖面。

---

## 2. 平行实现清单（PRD 问题 A 的代码实证）

### R7 "工具是否存在/允许"共有 8 份判定

全部持有同一个 `ToolRegistry`，但默认方向与触发时机不同：

| # | 位置 | 判定 | 默认方向 | 时机 |
|---|---|---|---|---|
| 1 | `tools/registry.py:44-49` + `tools/guardrails.py:383-431` | 注册契约自洽（DELETE 必须挂 destructive 等） | fail-closed，拒绝启动 | 注册时 |
| 2 | `core/planner/guardrail.py:50-53` | step.tool 是否注册 | fail-closed（errors → LLM 重试） | plan / revise / 合并复检 |
| 3 | `core/dag_executor.py:109-121` | 整计划再跑一次 #2 的 validate | fail-closed（PLAN_FAILED） | `execute()` 入口——**生产不走，见 R5** |
| 4 | `core/dag_executor.py:501-505` | `get_tool_def` + `get_tool_fn` 双查 | fail-closed（StepResult FAILED） | 每个 step 执行前（生产实际防线） |
| 5 | `tools/guardrails.py:186-202` `ToolWhitelistGuardrail` | 是否在 workspace `allowed_tools`（含 `prefix*` glob） | **fail-open**：scope 未声明白名单即放行；声明后 fail-closed | 每次工具调用 |
| 6 | `core/planner/revision_invariants.py:14-27` `step_is_mutating` | 借存在性判 mutating | fail-closed（未知=mutating，Q-06 对齐后） | 修订不变量校验 |
| 7 | `core/contract_extractor.py:86-89` + `models/intent.py:52-63` | 抽取契约的工具/操作是否合法 | fail-closed 单项丢弃（不阻断 run，耗尽→[]→unverified） | 首轮 plan 前；同一校验也服务 API caller 契约（`routes.py:286-293` → 400） |
| 8 | `core/scheduler/base.py:927-937` | serial 降级路径 `_find_tool_def` + `tool_fns` 双查 | fail-closed（`_fail` 整个 run） | serial act 时 |

- 三份规则集还存在语义重叠：PlanGuardrail G2（`:50-53`）与 invariants R3（`revision_invariants.py:72-81`）对同一个未知工具步骤可能产出两条互不引用的拒绝文案（经 `revision_guard.py:332-340` 顺序拼接同时喂给 LLM）。
- 同一故障的用户可见文案至少四种：`unknown tool 'x'`（`planner/guardrail.py:52`；`contract_extractor.py:87`；`models/intent.py:62`）、`Tool 'x' not registered`（`dag_executor.py:505`）、`Unknown tool: 'x'`（`scheduler/base.py:930`）。按 error 文本做聚类会裂成多类。
- PlanGuardrail 内部重复：input 必须是 object 查了两次（`guardrail.py:76-78` 与委托的 DAG 结构校验 `models/plan.py:225-227`）；`max_parallel` 超限规则（`:143-168`）只 warning、返回 []，永不报错（真正强制在 `dag_executor.py:511-512` 的 per-tool 信号量）。
- **防线不可合并的部分（记录在案）**：执行时独立检查（#4）是 AGENTS.md 约束 4 要求的纵深防御，不依赖计划期正确性；注册期 #1 必须先于一切可见性；#5 的 fail-open 是当前 workspace 存量语义。这些默认方向在任何整理中都不得被顺手反转。

### R8 LLM 调用 6+1 条独立封装，失败语义系统性不一致

所有路径最终汇聚 `LLMClient.chat`（`core/llm_client.py:54`），但外层语义各写一份：

| 路径 | 位置 | 重试 | run deadline | 解析失败处理 | run_id/trace |
|---|---|---|---|---|---|
| planner plan/revise | `planner/agent.py:86-218`（`_chat_structured:56`） | 3 次，错误**喂回模型** | 经 `_phase_call` | retry_prompt 回喂 | 有 |
| planner local_repair | `planner/agent.py:220-276` | 3 次，但首个 LLM 异常即 `return None`（`:257-259`） | 经 `_phase_call` | 同 prompt 空转重试，不回喂错误（`:262-270`） | 有 |
| classify | `scheduler/classify.py:132-161` | 1 次 | 经 `_phase_call` | 异常→保守判定"需要工具"放行（`:156-161`） | 有 |
| contract extractor | `core/contract_extractor.py:170-179` | 2 次 | 外层 `wait_for` 15s（`classify.py:83`；常量 `contract_extractor.py:25`） | 有 `_RETRY_HINT` 回喂；耗尽→[]（D-04） | **chat 未传 run_id** |
| episode 摘要 | `context_manager.py:565-572` | 0 次 | **无**（见 R4） | 裸 `json.loads`，失败降级 legacy（`:583-591`） | 无 |
| serial kernel think | `agent_kernel.py:178-227` | 0 次 | **无**（见 R4） | 文本 `ANSWER:`/`<STOP>` 解析（`:229-276`） | 经 client 上报 |
| llm_judge（评测） | `evaluation/scorers/llm_judge.py:43-52` | 0 次 | 无 | None | 无（可接受） |

- 阶段计时日志块也有两份手抄：`planner/agent.py:64-84` 与 `scheduler/classify.py:139-155`。
- 宽容 JSON 解析（去 markdown fence → 抠最外层 `{...}` → `json.loads`）有 4 个变体：`planner/parsing.py:27-41`、`:61-77`、`contract_extractor.py:111-125`、`evaluation/scorers/llm_judge.py:75-84`；`context_manager.py:583-591` 是第 5 处但**不剥 fence**——模型把 JSON 放进代码块即静默降级 legacy，其余四处则能恢复。
- prompt 注册表（`system_prompt.py:18-352`，9 phase）本身组织良好，但有注册表之外的 4 份 prompt 住户：契约抽取（`contract_extractor.py:27-62`）、judge、stop 摘要内联文本（`agent_kernel.py:88-101`）、planner 二次组装（`planner/prompts.py:77-103`，其中 `.replace("## User Intent\n", ...)` 靠匹配模板字面量注入，模板改名即静默失效）。另外 `AgentPhase.SERIAL_THINK`（`system_prompt.py:23,207-239`）注册后无任何消费方，实际使用的 `_FN`/`_TEXT` 两份 prompt 文本约 90% 重复。

### R9 读模型 / 序列化 / 存储的多份实现与已发生漂移

- **run 列表两套序列化且字段已漂移**：REST `GET /runs`（`api/routes.py:243-271`，8 字段，含 `orphaned`/`workspace_id`）vs unified query `type=runs`（`api/query.py:109-163`，9 字段，含 4 个 tool 计数但**缺** `orphaned`/`workspace_id`）；两段 list/fold 批处理也各抄一遍。
- **run 详情**：REST `routes.py:386-408`（11 字段）与 query `query.py:222-263`（约 25 字段）各写一份；`pending_confirmations` 的 5 字段结构写了两遍（`routes.py:398-407` 的 `PendingConfirmationItem` vs `query.py:248-257` 内联 dict）。
- **工具 6 桶状态聚合同一文件内两份逐字同构**：`query.py:182-205` 与 `:421-447`。
- **analysis 默认时间窗口两条路径语义不同**：REST analysis 强制最近 24h（`api/analysis_routes.py:39,54,69` 均 `time.time()-86400`）；unified query 传 None，而 service 把 None 当"全部历史"（`analysis/service.py:55-56`）。同一 dashboard 两个入口默认数据口径不同。
- **`_row_to_event` 两份且已分叉**：`storage/event_store.py:1004-1034`（含 tenant_id/workspace_id/is_audit）vs `api/query.py:669-693`（缺这三列）。
- **DTO 重复定义**：`api/schemas.py:42-51` `WorkspaceResponse` 与 `models/workspace.py:45-53` 8 个字段全重复，3 处手工拼（`routes.py:163,172,198`），而 create 路径（`:148`）干脆直接返回未校验的 `workspace.model_dump()`；`EventResponse`（`schemas.py:125-133`）与 `models/events.py` 的 `Event` 9 字段重复。
- 写路径是干净的：query.py 全为 GET，无 append/pause/confirm。
- **`event_store.py` 一个类服务 5 种聚合**（1035 行）：事件流方法仅约 1/3（`:357-567`），其余是 client_request_claims（`:268-347`）、tenants（`:708-721`）、workspaces CRUD（`:723-822`）、conversations CRUD（`:844-977`）与派生读。CRUD 方法互不调用，但共享单连接、全局写锁（`:147`）与 run 缓存（`:151-152`）；`claim_client_request`（`:268-347`）是跨 claims+events 的原子事务。`storage/scoped.py` 29 个方法几乎全量透传，其中 `:133-141`（同源复核）、`:160-189`（SQL 白名单）等含真实安全逻辑，非纯转发。
- 前端同一资源两个取数路径并存：`listRuns()`（REST，History/Replay 页面消费）与 `queryRuns()`（仅遗留 OpsDashboard 消费），TS 类型两套且字段不一致；ops-client 的 15 个 query 函数中 8 个（run/events/run-analysis/timeline/tool-traces/tool-defs/plans/ws-clients）在 `pages/components/hooks` 中零调用。

---

## 3. 契约同源与"边际同步成本"（PRD 问题 D，实证比 PRD 更严重）

### R10 事件枚举/payload 实际上没有进入 OpenAPI 同源链

**证据**：

- 前端不存在 EventType 的 TS 枚举/判别联合。事件字段在生成/手写类型中全部是 `event_type: string`、`payload: Record<string, unknown>`：`frontend/src/api/schema.ts:114,176,239`（OpenAPI 生成产物）、`api/types.ts:6`、`api/ops-client.ts:113,191,292`。
- 原因：`scripts/generate_openapi.py:70-96` 只从 FastAPI 的 response_model 生成 schema，而 Event/Payload 模型从未作为 response_model 暴露。AGENTS.md §4.1 声称的"事件类型以后端 Pydantic 为唯一来源、前端自动生成"在事件这一层**链路不存在**。
- 前端事件表现靠 7 处硬编码表人肉同步：`components/ThinkingPanel.tsx:112-170`（label switch + 颜色表）、`components/OpsRealTimePanel.tsx:62-67`（`KEY_EVENT_TYPES` 功能性过滤，不在集合内不进实时流）与 `:219-227`（摘要文案）、`hooks/useRunWebSocket.ts:13-26`、`pages/RunDetail.tsx:15-39,163-182`、`api/analysis-styles.ts:97-113`。
- 后端新增一个"有折叠语义+需前端展示"的事件，实测触点约 **18–22 个手改点**：枚举（`models/events.py:11-58`）、payload model、`PAYLOAD_MODEL_MAP`（`:519-561`，手写 dict）、两层 `__init__.py` re-export（`models/__init__.py:16-50,68-128`、`harness/__init__.py:24-51,84-111`）、`fold.py:208-565` case（match 无 default，未知事件被静默忽略）、N 个语义消费点（analysis/query/replay/browser_pool 等）、前端 7 表、README 计数、测试。
- **无任何完整性强制**：grep 全仓无 `len(EventType)` / `set(PAYLOAD_MODEL_MAP)` / fold case 完备性断言（运行时核对当前 41=41 一致，纯靠纪律）。漏注册 map 的实际后果是写入时才在 `event_store.py:999-1000` 报 "Unknown event type"。
- **文档已漂移的活例**：`README.md:97` 标题"事件类型 (38 种)"、`:167` 注释"38 种 EventType"，实际枚举 **41 种**（运行时实测 `len(EventType)==41`）；README 环境变量表（`:302-319`）漏记已生效的 `HARNESS_LOCAL_REPAIR`、`HARNESS_LOCAL_REPAIR_TOOLS`（`serve.py:176-179`）、`HARNESS_CONTEXT_TOKEN_LIMIT`（`context_manager.py:50`）。
- 配置面同源问题同构：`SchedulerConfig` 10 个字段（`scheduler/base.py:76-109`）仅 3 个有 env 钩子（`serve.py:172-179`）；`/query?type=schedulers` 白名单只投影 5 个字段（`query.py:399-405`），前端 `ops-client.ts:260-266` 又手写同 5 字段。全仓约 40 个魔法常量（超时/阈值/截断长度/keep 窗口等）散落 8+ 文件、31 处 env 读取，无配置总表。

**对照（同源做得好的部分，记录以免被误改）**：operation 契约（side_effects/idempotency/probe/ref 白名单）在 `models/tools.py:65-118` 单点声明，executor/guardrail/幂等/`$ref` 全部派生消费，新 operation 无需改消费方；工具注册期安全校验自动 fail-closed；Planner 工具清单与 LLM schema 从注册表自动派生（`planner/prompts.py:33-61`、`tools/registry.py:86-87`）；工具层 GUARDRAIL_TRIGGERED → fold → analysis → 前端链路全自动。

---

## 4. 工程护栏与测试设施（PRD 问题 E/F 旁证）

- **无任何 CI 配置**（仓库无 `.github/workflows/`）。`.pre-commit-config.yaml` 仅 ruff、bug-summary 测试、全量 pytest 三个 hook；无 mypy、无前端 tsc/build/vitest。
- mypy 对 11 个模块整体关闭了多类错误（`pyproject.toml` disable 列表含 dag_executor、tools.executor、run_monitor、scheduler.base、mcp_*、api 多个文件）。
- 测试构造端重复显著：`Event(` 构造 561 处、`append_event(` 292 处、裸 dict 形式 RunStarted intent 182 处（与 75 处 `RunStartedPayload(` 强类型构造两种风格并存）；同一个 `_event(run_id, seq, type, payload, created_at=0.0)` 工厂至少复制 6 份（`test_fold.py:9-23`、`test_lifecycle.py:12-24`、`test_step_evidence_projection.py:15-16`、`test_replay_projection.py:30`、`test_intent_contract.py:115`、`test_context_manager.py:1178`）；scheduler 装配副本 6 份、mock 工具工厂多份；`store` fixture 在 `test_lifecycle.py:27-32` 与根 `conftest.py:8-13` 重复。断言端以字段级为主（裸 dict 全等仅 3 处），演进成本主要在构造端：给 payload 新增强制字段需逐文件手工补。

---

## 5. 观察汇总（仅分级，不含处置意见）

| # | 观察 | 性质 | 当前是否可达生产 | 证据 |
|---|---|---|---|---|
| R1 | serial 直写 RUN_COMPLETED 绕过终态守卫、无 CompletionVerdict | 受信强度缺口 | 间接（fallback / eval / scripts） | loop.py:182-198；base.py:644-684 |
| R2 | fallback 嵌套调度器：双 deadline / 控制信号断裂 / 双层 cleanup | 接缝缺陷 | 是（planner 重试耗尽时） | plan.py:1144-1161 |
| R3 | 压缩三档阈值算了不用，参数化无效 | 静默漂移/死配置 | 是（行为本身正常，配置者被误导） | context_manager.py:98-100 vs 148-176 |
| R4 | token 三口径；主路径记账为 0；两类 LLM 调用在 watchdog 之外 | 度量失真 + 强制覆盖缺口 | 是 | run_monitor.py:313；plan.py:133,349,382,919；loop.py:158；context_manager.py:565 |
| R5 | 执行入口计划级复检生产不执行 | 纵深防御层数少于文档 | 是（但生成/合并期防线仍在） | dag_executor.py:107-121；plan.py:516 |
| R6 | 计划层受信拒绝无事件、不可观测 | 可追溯性覆盖缺口 | 是 | planner/agent.py:130-134；plan.py:631-637 |
| R7 | "工具存在性"8 份判定 + 4 种文案 + 规则重叠 | 同语义多实现（PRD-A 模式） | — | 见表 |
| R8 | LLM 外围 6+1 封装语义不一致；lenient JSON 5 处 | 同语义多实现 | 是（R4 即其后果之一） | 见表 |
| R9 | run 读模型双份漂移、analysis 窗口漂移、store 上帝类 | 重复 + 已漂移 | 是（前端两入口数字不同） | routes.py/query.py；analysis_routes.py:39 |
| R10 | 事件未进 OpenAPI；18–22 触点靠人肉；无完整性断言；README 38/41 | 契约同源断链（比 PRD-D1 更严重） | — | schema.ts:114 等；events.py |
| R11 | episode JSON 不剥 fence，静默降级 | 解析口径不一致 | 是 | context_manager.py:583-591 |
| R12 | 无 CI、mypy 大面积豁免、测试工厂重复 | 护栏缺位（PRD-E/F） | — | 见 §4 |

---

## 6. 明确不在本文范围

- 不给修复方案、不建议裁/并/拆任何功能或事件、不排优先级。
- 不质疑受信边界设计本身：本次核对未发现证据丢失、安全绕过或完成门假绿；planning 主路径的 R5/R6 属"防线层数与可观测覆盖面"问题，不是已知实洞。
- R1–R12 的处置（修、并、留作已知、补文档）待 owner 逐项裁决；裁决后按 AGENTS.md §3.4（差异审查→文档修正→开发）与 §3.5（根治+回归测试）执行。
