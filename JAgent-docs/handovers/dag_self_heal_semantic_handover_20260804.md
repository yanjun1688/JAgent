# DAG 自愈重执行 Bug 修复 + 任务完成语义设计矛盾 — 交接文档

## 【项目背景】

- **项目**: Harness v2.1 Agent-First 任务执行引擎
- **路径**: `D:\Project\JAgent`
- **分支**: `review/alignment-check`（大量未提交 WIP，见 §6）
- **技术栈**: Python 3.11、FastAPI、Pydantic v2、aiosqlite/SQLite、pytest（`asyncio_mode=auto`）、ruff
- **核心约束**: 事件溯源 + 受信/非受信边界（AGENTS.md）；决策权归 Agent，强制权归系统
- **会话触发点**: 通过 Langfuse 追踪发现 DAG 自愈时已完成步骤被重复执行，token 无谓消耗；逐层排查出 3 个 Bug 并修复

## 【本文档定位】

本篇是给**新会话**的交接文档。读者需要快速恢复三类上下文：
1. 本次会话修复的 3 个 DAG 自愈 Bug（根因 → 改动 → 回归测试）
2. 修复过程中**新暴露的架构设计矛盾**：工具完成 ≠ 步骤任务完成（`should_not_rerun` 受信边界被打破）
3. 后续新会话需要继续的关键技术脉络（幂等键计算、planner.revise 契约）

---

## 【一】本次修复的 3 个 Bug

### Bug 1: revise 复用 step id 时，已完成步骤被重复执行

**现象**: 5 步计划中 s3 soft_error → revise 后，`DagStepStarted` 事件计数为
`{'s1': 2, 's2': 1, 's3': 1, ...}` —— 已完成的 s1 被重新调度执行。

**根因**: `harness/core/scheduler/plan.py` 的 `_execute_plan` 计算
`completed_ids` 时带了过滤条件：

```python
# 旧代码（错误）
completed_ids = {
    sid for sid, r in results.items()
    if isinstance(r, StepResult) and r.should_not_rerun
    and sid not in {s.id for s in plan.steps}   # ← 错误假设
}
```

该过滤隐含假设 **revise 会为剩余步骤铸造全新的 step id**（新 id 不在原
plan 中 → 全部视为"待执行"）。但实测 LLM 的 revise 通常**复用原有 step
id**（s1 仍在 revised plan 里，只是 s3 的 input 被修正）。于是已完成步骤
的 id 全部被过滤掉 → `completed_ids` 为空 → 自愈后重跑整个 DAG。

**修复**: 删除 `and sid not in {s.id for s in plan.steps}` 过滤。

```python
completed_ids = {
    sid for sid, r in results.items()
    if isinstance(r, StepResult) and r.should_not_rerun
}
```

---

### Bug 2: `topological_sort(completed_step_ids=...)` 参数名承诺与实现不符

**现象**: 修复 Bug 1 后，revise 复用 id 时出现误报 `Cycle detected`，或
completed 步骤仍然被调度（`should_not_rerun` 步骤从拓扑层泄漏）。

**根因**: `harness/models/plan.py` 的 `topological_sort`：

- 参数名 `completed_step_ids` 暗示"跳过这些已完成步骤"，但旧实现只在
  **依赖合法性校验**（`all_valid`）里用到它；
- **Kahn 队列初始化** `queue = deque([sid for sid, deg in in_degree.items() if deg == 0])`
  完全没过滤 completed → completed 步骤照样入队；
- `visited` 从 0 计数，但 completed 步骤不经过出队 → 造成 `visited != len(steps)`
  误报环检测失败。

**本质问题**: "参数说跳过，代码却照样入队" —— 这是典型的**受信组件内部
约束与契约不一致**，属于架构偏离（见 §三）。

**修复**（4 处）:

```python
completed = set(completed_step_ids or ())

# 1) 依赖合法性: completed 依赖视为已满足（原有，保留）
if dep not in steps or dep in completed:
    continue

# 2) 队列初始化过滤 completed
queue = deque([
    sid for sid, deg in in_degree.items()
    if deg == 0 and sid not in completed
])

# 3) visited 初始化为"已完成的 in-plan 步骤数"
visited = len(completed & set(steps.keys()))

# 4) 出队 & 解锁邻居均过滤 completed
if sid in completed:
    continue
...
if in_degree[neighbor] == 0 and neighbor not in completed:
    queue.append(neighbor)
```

**注意**: 修复后中间层 completed 的语义是——其上游/下游正常调度，但依赖它的
下游步骤 in_degree 不把该 completed 依赖计入（如 s2 completed，s3 依赖 s2，
则 s1、s3 同层并行）。

---

### Bug 3: soft_error 自愈分支丢失 LLM 的 `step_tasks` 判定

**现象**: 5 步计划中 s3 产生 SOFT_ERROR，revise 里 LLM 标注
`step_tasks: {"s3": "not_achieved"}`，但修复后 s3 依然**不会**被重新执行
（`DagStepStarted: {'s3': 1}`，应为 2）。

**根因**: `harness/core/scheduler/plan.py` 的 `_execute_plan` 有两个 revise
分支：
- **layer_failure 分支**（`if not ok:`）已有 `revised.step_tasks →
  results[sid].task_state` 的合并逻辑；
- **soft_error 分支**（`all_layers_ok` 后检测到 soft_error）**缺失**这段合并。

由于 `StepResult.should_not_rerun` 的语义是 `exec_state 属于(COMPLETED,
SOFT_ERROR, IDEMPOTENT, SKIPPED, CANCELLED) 且 task_state != NOT_ACHIEVED`
（见 §三），LLM 的 `not_achieved` 判定不 merge 进 `results[sid].task_state`，
`should_not_rerun` 就保持 True → 修正后的 s3 永远不会重跑。

**修复**: soft_error 分支补上与 layer_failure 分支**完全一致**的合并逻辑：

```python
if revised.step_tasks:
    for sid, ts_str in revised.step_tasks.items():
        if sid in results:
            try:
                results[sid].task_state = TaskState(ts_str)
            except ValueError:
                pass
```

**设计教训**: 两条 self-heal 路径（layer_failure / soft_error）必须共享
step_tasks 合并逻辑，禁止复制差异。后续应提取为单一辅助函数。

---

## 【二】回归测试（全部通过）

### 新增 `tests/test_dag_self_heal.py`（7 个测试）

| 测试 | 锁定内容 |
|------|----------|
| `test_self_heal_does_not_re_execute_completed_step` | 端到端：s2 失败 revise 后 s1 仅执行 1 次 |
| `test_self_heal_reuses_ids_skips_in_plan_completed` | 复用 id 时 completed 从拓扑移除 |
| `test_soft_error_self_heal_reruns_only_failed_step` | 场景3：5 步中仅 s3 重跑 1 次（`{'s3': 2}`） |
| `test_topological_sort_no_false_cycle_with_completed` | completed 依赖不再误报环 |
| `test_topological_sort_true_cycle_still_detected` | 真环仍抛出 Cycle detected（不因修复放松） |
| `test_completed_step_never_enters_schedule_queue` | **锁死 Bug 2**：completed 绝不入队 |
| `test_completed_step_in_middle_layer_skipped` | 中间层 completed 正确跳过，上下游正常调度 |

### 扩展 `tests/test_planner_revise_rerun.py`（+4 测试）

`test_topological_sort_skips_completed_in_plan_step`、
`test_topological_sort_completed_in_first_layer`、
`test_topological_sort_dep_on_completed_skips_dependency_count`、
`test_topological_sort_completed_in_plan_cycle_detection_still_works`。

### 验证方式（关键模式）

用 `DagStepStarted` 事件计数（`Counter`）证明"每个 step 只执行应有的次数"：

```python
counts = {e.payload["step_id"]: ... for e in events if e.event_type == DagStepStarted}
assert counts["s3"] == 2   # 唯一允许重跑的就是失败的那个 step
```

### 全量结果

- `pytest -q` → **772 passed, 2 skipped, 1 warning**
- ruff：本次涉及文件全干净（`harness/models/plan.py`,
  `harness/core/scheduler/plan.py`, `harness/tools/file_op.py`,
  `tests/test_dag_self_heal.py`, `tests/test_planner_revise_rerun.py`）
- 注意：仓库存在 ~75 个**既有** ruff 错误（`tests/test_dag_executor.py` 等），
  非本次引入，未处理。

---

## 【三】★ 新暴露的架构设计矛盾（新会话重点）

### 矛盾点

`JAgent-docs/plans/completion-semantics/TDD_S1_任务完成语义分层.md` 声称：

> `should_not_rerun` 判定为纯函数 `ExecState → bool`，不依赖 LLM 输出
> （§7 受信边界检查清单第 3 条，勾选 ✅）
>
> `should_not_rerun` 对 COMPLETED / SOFT_ERROR / IDEMPOTENT / SKIPPED /
> CANCELLED → True（§8 验收标准）

但**实际代码** `harness/core/dag_types.py:74-77`：

```python
@property
def should_not_rerun(self) -> bool:
    return self.exec_state in (
        ExecState.COMPLETED, ExecState.SOFT_ERROR, ExecState.IDEMPOTENT,
        ExecState.SKIPPED, ExecState.CANCELLED,
    ) and self.task_state != TaskState.NOT_ACHIEVED   # ← 依赖 LLM 输出
```

**文档与实现不一致**：实现已经让"系统强制属性"依赖了 Agent（LLM）的
`task_state` 判定。这不是本次会话改的（`dag_types.py` 未被本会话触碰，
`git log` 显示是先前提交引入），但本次修复 Bug 3 恰恰**依赖**这个耦合才能
工作——即"工具完成（exec_state=SOFT_ERROR）但任务未达成（task_state=
not_achieved）时允许重跑"。

### 为什么这是个设计问题

按 AGENTS.md 约束 4：

> 危险操作的拦截由 Tool Layer Guardrails 负责，与 System Prompt 是否提醒
> Agent 无关。**系统强制不依赖 Agent 配合。**

`should_not_rerun` 是系统强制属性（决定一个 step 是否被再次调度）。如果它
依赖 LLM 输出 `task_state`，那么"LLM 忘标 not_achieved" → 系统就认为已完成 →
soft_error 步骤永远不重跑。**强制权又回到了 Agent 手里**，违背受信边界。

### 两种可能的方向（新会话需用户拍板）

| 方向 | 方案 | 影响 |
|------|------|------|
| **A. 严格执行纯函数** | `should_not_rerun` 只由 `exec_state` 决定；SOFT_ERROR 默认**允许重跑**（回归到 `must_rerun` 语义）；`task_state` 仅作为给 LLM 的参考信息 | 受信边界干净；但 SOFT_ERROR 的"小问题"可能被无条件重跑，浪费 token |
| **B. 承认双因子** | 更新 TDD_S1 文档，明确 `should_not_rerun = f(exec_state, task_state)`，把 task_state 纳入受信模型（如：revise 时由系统强制要求 LLM 为每个 SOFT_ERROR step 输出 task_state，缺省视为 not_achieved） | 语义贴近真实需求；需同步修改文档的受信边界声明，不能假装是纯函数 |

**本次 Bug 3 的修复按方向 B 的隐含假设实现**（merge 后重跑），但文档还没
更新。新会话**第一步**应回到 §3.4 三步流程：报告差异 → 修正文档 → 再开发。

---

## 【四】关键机制记录（新会话快速上手）

### 4.1 executor 幂等键计算

文件: `harness/tools/idempotency.py`（`IdempotencyKeyGenerator.compute`）

```python
@staticmethod
def compute(tool_def: ToolDefinition, input: dict) -> str | None:
    if tool_def.idempotency_key_fields is None:
        return None                                     # 工具声明关闭幂等
    fields = tool_def.idempotency_key_fields
    subset = {k: input[k] for k in fields if k in input}
    payload = json.dumps(subset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    seed = f"{tool_def.name}:{payload}"
    return hashlib.sha256(seed.encode()).hexdigest()
```

要点：
- **种子 = `tool_name:规范化JSON子集`**，规范化为 sort_keys + 紧凑分隔符
- 只取 `idempotency_key_fields` 声明的字段子集（`tools/idempotency_key_fields`），
  不在 input 里的字段静默忽略
- 查询键是 `run_id + EventType.TOOL_COMPLETED + ik_key`（`executor.py:190`
  `store.find_by_idempotency_key`）
- 命中缓存 → 返回 `IDEMPOTENCY_HIT`，不重新执行（`executor.py:204-214`）
- **语义注意**: 幂等缓存按 `run_id` 隔离。同一 run 内重跑相同工具+相同关键
  参数 → 命中缓存。SOFT_ERROR 结果也会写入 TOOL_COMPLETED 并被缓存命中
  （`result_type=SOFT_ERROR`，`has_semantic_error=True`）——这意味着 **Bug 3
  场景里 s3 重跑时，如果 input 不变，幂等缓存会直接返回上次的 SOFT_ERROR**！
  新会话需确认这是期望行为（想重跑修正，则 revise 必须改 input，否则被幂等挡住）。
- 幂等键是**确定性纯函数**：无时间戳、无随机数、无 LLM 参与 → 满足约束 2
  （Agent 不感知幂等机制）。

### 4.2 planner.revise 契约

文件: `harness/core/planner.py`

- 入口签名:
  `revise(plan, results, system_state, feedback=None, intent_fallback="")`
- 计算 `executed_step_ids = {sid for sid, r in results.items() if isinstance(r, StepResult) and r.should_not_rerun}`，
  传给 `PlanGuardrail.validate(revised, completed_step_ids=executed_step_ids)`
  和 `_parse_plan(response, executed_step_ids)`（用来过滤 step_tasks 里的未知 step）。
- **step_tasks 解析**（`_parse_plan`）: 只接受 `executed_step_ids` 中的 step
  的 task_state；非合法枚举值 → 降级为 `unknown`。
- 输出: 若 `revised.steps` 为空 → 返回空 plan 表示"任务完成"；`revised.failed`
  → 表示"任务无法完成"。
- **系统状态注入**: `dag_executor.build_dag_status_text` 生成
  `【系统状态 - 不可折叠】` 标记的文本，含 per-step 的 exec_state /
  should_not_rerun（replan=NO/MAYBE）/ task_state / 原始 input。
- 该 revise 上下文注入点在 `planner.py` 的 `get_prompt(AgentPhase.REVISE, ...)`。

### 4.3 两条 self-heal 路径

`harness/core/scheduler/plan.py` `_execute_plan`:

| 路径 | 触发条件 | 行为 |
|------|----------|------|
| layer_failure | `execute_layer` 返回 `ok=False`（有 step FAILED） | 立即 revise → 若返回步骤则 `plan = revised; self_heal_count += 1; break` 重跑 |
| soft_error | 所有层执行完但存在 `has_soft_error` 的 step | 完成后 revise → 若返回步骤则 `plan = revised; self_heal_count += 1; continue` 重跑 |

两处都必须 merge `revised.step_tasks → results[sid].task_state`（本次 Bug 3
修复）。**建议**后续提取公共辅助函数，消除重复。

---

## 【五】Langfuse 可观测性现状（本会话前置工作）

- `harness/monitoring/langfuse_tracer.py` — 纯观测层（非受信），
  `LANGFUSE_ENABLED=false` 时全部 no-op；异步 flush（`asyncio.to_thread`）
- 追踪点: scheduler 迭代、tool 执行、guardrail 触发、confirmation、
  PlanCreated/PlanRevised/PlanCompleted 事件
- score 附加: `tracer.attach_score(...)`；v4 SDK 无同步 fetch，查询走
  `tracer._client.api.scores.get_many(name=..., trace_id=...)`
- 已验证: `eval_dag_parallel_001` / `eval_dag_real_llm_001` 上传成功
- **本次 Bug 正是通过 Langfuse 的 tool 追踪 + event 计数发现的**——观测层
  价值得到验证

### 评估体系（`evaluation/`）

- `run_eval.py`: mock_plan 分支（MockLLMClient + 确定性 plan JSON）、
  mock_actions、真实 LLM smoke 场景
- 数据集: `evaluation/datasets/jagent_eval.yaml`（21 cases）
- 判定原则: guardrail/confirmation/DAG 拓扑用例走**确定性 mock**（真实 LLM
  拒绝危险操作）；真实 LLM 只用于 smoke 测试（宽松断言）
- 评分器: `evaluation/scorers/rule_based.py`（recovery/hallucination/
  output_accuracy/consistency + `_parallelism` 只读最新 PLAN_CREATED，语义保持
  原样、仅文档说明）

---

## 【六】仓库状态与未完成事项

### 未提交改动（`git status` 大量 M/??）

本会话改动（未提交）：
- `harness/models/plan.py`（Bug 2 topological_sort + E501 清理）
- `harness/core/scheduler/plan.py`（Bug 1 + Bug 3 + E501 清理）
- `harness/tools/file_op.py`（新增 `reset_sandbox_root()`）
- `tests/test_dag_self_heal.py`（新文件，7 测试）
- `tests/test_planner_revise_rerun.py`（+4 测试）

仓库原有大量 WIP（非本会话）：
- `harness/api/*`, `harness/core/agent_kernel.py`, `harness/core/context_manager.py`,
  `harness/core/llm_client.py`, `harness/storage/event_store.py`,
  `harness/tools/guardrails.py`, `harness/tools/mcp_manager.py`, 等
- 未跟踪文档: `JAgent-docs/archive/v3.0-3.2/ARCHITECTURE_v3.0_Phase1.md`,
  `JAgent-docs/archive/v2.x/LANGFUSE_*.md`, `JAgent-docs/Prd/PRD_*` 等

### 新会话 TODO（按优先级）

1. **[P0] 设计矛盾裁决**: §三 方向 A vs B，用户拍板后按 AGENTS.md §3.4
   三步流程（报告差异 → 修正 TDD_S1 / ARCHITECTURE_v2.1 §3.7 → 再开发）
2. **[P1] 确认幂等缓存对 soft_error 重跑的影响**: Bug 3 场景若 revise 不改
   input，重跑会命中幂等缓存返回旧 SOFT_ERROR → 重跑形同虚设。需测试验证
   并决定是否在重跑路径绕过幂等缓存
3. **[P1] 消除两条 self-heal 路径的 step_tasks 合并重复代码**（提取辅助函数）
4. **[P2] 提交本会话修复**（当前分支 review/alignment-check 有大量他人 WIP，
   提交前需与用户确认范围）

### 相关文档

- TDD_S1（设计源头）: `JAgent-docs/plans/completion-semantics/TDD_S1_任务完成语义分层.md`
- 架构缺口 S1（已知"工具完成 vs 任务完成"歧义）: `JAgent-docs/archive/v2.x/ARCHITECTURE_v2.1.md §3.7`
- 上一份交接（规划器协议缝隙）: `JAgent-docs/handovers/structured_tool_calls_fix_handover_20260723.md`
- 修复代码: `harness/core/scheduler/plan.py` / `harness/models/plan.py` /
  `harness/core/dag_types.py` / `harness/tools/idempotency.py`

---

## 【七】留给新会话的关键问题清单

- [ ] `should_not_rerun` 是否允许依赖 `task_state`？（受信边界 vs 实际语义）
- [ ] SOFT_ERROR 步骤重跑时，幂等缓存命中是否应被绕过？
- [ ] `completed_step_ids` 只过滤"in-plan"的 completed——如果 revise 引入
      全新 id，原 completed 是否仍应传递？（当前 Bug 1 修复后已不再过滤，
      相当于所有 completed 都传递）
- [ ] 中间层 completed 与其上游同层并行的语义是否需要在 UI / 事件流中体现？
