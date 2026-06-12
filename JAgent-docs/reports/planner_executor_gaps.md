# Planner-Executor 架构缺口分析

> **编号**: ARCH-2026-06-08
> **范围**: V0.7 Planner-Executor 架构（Plan→Execute→Revise）
> **状态**: 设计审查稿 v2.0
> **关联文档**: `ARCHITECTURE_v2.1.md` · `TODO_v2.1.md` · `architecture_issues.md`
> **审查迭代**: 2026-06-08 代码交叉审查后决策（详见 §7 变更摘要）

---

## 1. 背景

### 1.1 架构演进

Harness 从 V0.6（串行 `think→act→observe`）演进到 V0.7（`Plan→Execute→Revise`）。核心变化是把"编排"从 LLM 每轮 Think 里抽出来，交给 Planner（LLM 生成 DAG Plan）+ DagExecutor（拓扑排序并行执行）驱动。

```
V0.6 (串行):     think → act(串行) → observe
V0.7 (DAG):     plan → execute(并行) → revise
                    ↑ 每轮 N 步，同层并行
                    ↑ LLM 只负责规划，系统负责执行
```

### 1.2 已完成的架构审查

2026-06-07 完成了 V0.7 架构审查（见 `architecture_issues.md`），修复了：

| 类型 | 修复项 |
|------|--------|
| P0 崩溃 | `is_paused()` None 检查、seq 锁竞态、DAG_STEP fold 重复 |
| P1 功能缺陷 | 顶层导出、参数兼容(`parameters`→`input`)、总结回答、压缩白名单 |
| P2 逻辑错误 | 中文硬编码（全英文化）、breaker 提取、反馈复用、继承重构 |
| 架构重构 | 删除 Orchestrator(-336行)、AgentLoopScheduler 继承 BaseScheduler(-78行) |

审查后基线：**338 项测试全通过**。

### 1.3 审查盲区

`architecture_issues.md` 是 **组件级审查**——每个组件单独检查崩溃/数据损坏/代码质量。但它没有做 **跨层协议审查**——组件之间的数据流/控制流协议从未被形式化定义。

2026-06-08 运行日志（`harness.log`）暴露的 7 个运行期问题，全部落在"组件级审查盲区"内：

| 日志问题 | 审查盲区原因 | 深层性质 |
|----------|-------------|---------|
| ① 变量引用解析失败 | `plan.py` 和 `dag_executor.py` key 命名规范不一致 | 协议缺失 + 语义断裂：Planner 认为 `$s1.json` 是"占位符查找"，Executor 读取 `s1_result` key——二者对"变量如何回填"的理解不同 |
| ② 确认死循环 | 状态在组件之间传播时被折叠丢失 | 语义断裂：Tool Layer 的 CONFIRMATION_NEEDED 是"等待外部输入"，Scheduler 将其理解为"失败需要 revise"——对"阻塞"与"失败"的根本分歧 |
| ③ Guardrail 与工具不匹配 | Schema 校验和变量解析的先后顺序未定义 | 协议缺失：变量解析失败→未解析字面量传给 SchemaGuardrail→类型不匹配→拦截 |
| ④ Revise 上下文混乱 | Planner revise 收到纯文本 dump，无结构化协议 | 协议缺失：缺少保留 ID、可用输出、revise 计数等结构信息 |
| ⑤ 监控反馈未生效 | DAG 路径下 Monitor 从未收到 TOOL_FAILED 事件（只收到 DAG_STEP_FAILED） | 协议缺失 + 语义断裂：Monitor 对"什么是失败"的计数（TOOL_FAILED）与 DAG 路径的事件类型（DAG_STEP_FAILED）不一致 |
| ⑥ LLM 配额（已暂缓） | — | — |
| ⑦ Answer 信息不完整 | Answer 生成时的 intent-vs-result 校验从未定义 | 语义断裂：系统"认为"任务完成就回答成功，不检查实际结果是否符合用户意图 |

7 个问题本质上是 **协议缺失 + 语义断裂** 的组合体。组件间对同一概念（"完成"、"失败"、"阻塞"、"变量引用"）的理解不一致，而组件边界从未被形式化定义。每个缺失协议对应的解决方案同时也是语义对齐。

---

## 2. 架构缺口分析

### 2.1 当前 Planner-Executor 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                         PlanningExecutorScheduler                   │
│                                                                     │
│  ┌──────────────────────┐    ┌──────────────────────────────────┐   │
│  │  Planner (非受信)     │    │  DagExecutor (受信)              │   │
│  │  ├─ plan(intent)      │───▶│  ├─ execute_layer()             │   │
│  │  ├─ revise(plan,     │◀───│  │  ├─ _execute_step_only()     │   │
│  │  │   results, state)  │    │  │  └─ build_dag_status_text()  │   │
│  │  └─ generate_answer() │    │  └──────────────────────────────┘   │
│  └──────────────────────┘    │                                       │
│                                │  ┌──────────────────────────┐      │
│                                │  │  Tool Layer (受信)        │      │
│                                │  │  executor.execute()      │      │
│                                │  │  ┌─ SchemaGuardrail      │      │
│                                │  │  ├─ IdempotencyCheck     │      │
│                                │  │  ├─ ConfirmationCheck    │      │
│                                │  │  ├─ GuardrailRunner      │      │
│                                │  │  └─ Sandbox.invoke()     │      │
│                                │  └──────────────────────────┘      │
│                                                                     │
│  RunMonitor(受信) ── on_append ──▶ FeedbackInjected ──▶ 文本反馈     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 缺口 A：数据平面 — 变量解析协议（缺失）

#### 2.2.1 现状

DAG 步骤之间通过变量引用传递数据。Planner 在步骤 `input` 中生成 `$stepId.field` 格式的引用，DagExecutor 在执行时将其替换为上游步骤的实际输出。

#### 2.2.2 代码矛盾

| 位置 | 代码 | key 命名 | 行号 |
|------|------|----------|------|
| `plan.py:upstream_outputs()` | `merged[f"{dep_id}_result"] = output` | `"s1_result"` | line 75 |
| `dag_executor.py:_resolve_variables_in_input()` | `upstream.get(var_name)` | 查 `"s1"` | line 234 |
| `dag_executor.py:_substitute_vars()` | `upstream.get(var_name)` | 查 `"s1"` | line 268 |
| 现有测试 | 硬编码 `upstream={"s1": ...}` | `"s1"` | test_dag_executor.py:128+ |

`upstream_outputs()` 以 `s1_result` 命名 key，但解析器查 `s1`。**永不相交**。变量引用 `$s1.body.url` 永远不被解析，原样字面量传递给下游工具。

#### 2.2.3 根因

变量解析被视为 DagExecutor 的"内部工具方法"，而非 Planner 和 Executor 之间的契约。Planner（非受信）生成引用、Executor（受信）消费引用的 **命名规范从未被形式化**。

#### 2.2.4 影响

- 所有 `$stepId.field` 引用全部失效
- 下游步骤收到的是字面量字符串 `"$s1.body.url"` 而非实际 URL
- 文件写入 12 字节（应为 31 字符 URL）
- SchemaGuardrail 拦截字符串格式的变量引用（`type: object` 收到 `"$s1.json"`）

### 2.3 缺口 B：控制平面 — 状态传播协议（缺失）

#### 2.3.1 现状

Tool Layer 定义了 6 种执行状态，但在 DAG 执行管线中逐层折叠丢失。

#### 2.3.2 状态传播链

```
Tool Layer                → 6 种状态
  COMPLETED, FAILED, TIMEOUT, GUARDRAIL_BLOCKED, CONFIRMATION_NEEDED, IDEMPOTENCY_HIT
    ↓
_execute_step_only()      → 2 种状态（line 216-223）
  "completed" / "error"    ← CONFIRMATION_NEEDED 被折叠为 error
    ↓
_execute_layer()          → 1 位布尔（line 134-184）
  True / False             ← 信息仅保留"有/无失败"
    ↓
_execute_static_plan()    → 1 位布尔
  ok / not ok
    ↓
_plan_execute_revise_loop → 仅检查终态（line 689）
  COMPLETED / FAILED
```

#### 2.3.3 代码矛盾

```python
# dag_executor.py:216-223 — 信息丢失点
if result.status.value in ("completed", "idempotency_hit"):
    return {"status": "completed", ...}
# CONFIRMATION_NEEDED 落在此分支外，永远被当作 FAILED
error = result.error or f"Step failed with status {result.status.value}"
return {"status": "error", "error": error, "retryable": ...}
```

#### 2.3.4 根因

DAG 执行路径从未设计过"阻塞等待外部输入"的概念。架构只考虑了"要么成功要么失败"的二分法，没有为 `CONFIRMATION_NEEDED` 这类非终态阻塞状态留出传播通道。

#### 2.3.5 影响

- 需要确认的工具触发无限 revise 循环（确认请求 → 被当作失败 → Planner revise → 生成相同计划 → 再次确认请求）
- Run `a877a411` 从 17:23:58 到 17:26:17 持续 2.5 分钟，产生 75 个事件
- 最终因 LLM API 配额耗尽失败

### 2.4 缺口 C：反馈平面 — 监控控制协议（缺失）

#### 2.4.1 现状

Monitor 通过 `EventStore.on_append` 回调实时监听事件（`run_monitor.py:70-73`），检测到异常后写入 `FEEDBACK_INJECTED` 事件（`run_monitor.py:358-392`）。Scheduler 在 THINK 前读取反馈（`scheduler.py:154-208`）注入 LLM System Prompt。

#### 2.4.2 设计缺陷

Monitor 的输出只有**文本反馈**，没有**控制指令**：

```python
# run_monitor.py:358-392 — 只能写文本
await self._inject_feedback(
    rid, "high",
    feedback_text=f"Tool '{tool}' failed {count} times",  # 只能说话
    ...
)
```

Scheduler 只能"建议"LLM 采纳：

```python
# scheduler.py:456-458  — 文本注入 System Prompt
feedback_text = self._get_feedback_text(state)
if feedback_text:
    # 由 LLM 决定是否遵守（非强制）
    think_results = await self.kernel.think(...)
```

#### 2.4.3 根因

Monitor 被设计为"被动观察者+文本建议者"，没有控制权。反馈回路依赖 LLM 的配合——但 LLM 可能忽略反馈文本。缺少不依赖 LLM 的硬控制机制。

#### 2.4.4 影响

- 日志中 `[MONITOR]` 仅在运行结束时出现（"Cleaned up run ..."），运行过程中无任何干预
- 无限循环中 Monitor 没有触发熔断
- `consecutive_failures` 在 DAG 路径中始终为 0（计数器的 reset 逻辑和 DAG 的执行模式不匹配）

### 2.5 缺口 D：交叉问题 — Guardrail 与变量解析顺序（未定义）

#### 2.5.1 现状

SchemaGuardrail 在变量解析**之后**执行（因为解析在 `dag_executor.py:199-201` 中、`executor.execute()` 调用之前）。但变量解析本身因缺口 A（key 命名不一致）失败，导致未解析的字符串值传入 SchemaGuardrail。

#### 2.5.2 实际管线顺序

```
upstream_outputs() → resolve_variables() → executor.execute()
                                             ↓
                                      SchemaGuardrail ← 看到的是未解析的字符串
```

#### 2.5.3 根因

架构从未定义"变量解析 vs Schema 校验"的顺序约束。正确顺序是"先解析后校验"，但因为解析内部有 bug，等价于"不解析就校验"。

#### 2.5.4 影响

- `"$s1.json"`（string）触发 `not of type 'object'`
- `"<msg>hello</msg>"`（XML string）触发 `not of type 'object'`
- 日志中大量 GuardrailTriggered 事件

### 2.6 缺口 E：交叉问题 — Revise 上下文协议（缺失）

#### 2.6.1 现状

Planner revise 时收到的上下文是 `build_dag_status_text()` 生成的纯文本，包含步骤状态和部分输入信息，但缺少关键结构。

#### 2.6.2 当前输出内容

```
【系统状态 - 不可折叠】
Plan: search weather
Total layers: 2 | Current layer: 2/2
Total steps: 2 | Completed: 1

  - s1(search): [done]  Summary: OK
  - s2(file_op): [failed] Input: {"path": "weather.txt"} | Error: permission denied
```

#### 2.6.3 缺失的结构信息

| 信息 | 重要性 | 缺失后果 |
|------|--------|----------|
| 已占用的 step ID 列表 | 关键 | Planner 生成新计划时复用旧 ID（如 s1） → `depends on unknown step` 错误 |
| 已完成步骤的输出值 | 关键 | Planner 不知道已有数据，重复查询 |
| Revise 次数 | 重要 | LLM 不感知自己改了多少次，可能"坚持同一个方案" |
| 错误原因分类 | 重要 | 不能区分"权限不足"和"格式错误"，给出错误替代方案 |

#### 2.6.4 根因

revise 上下文是"纯文本 dump"而不是"结构化协议"。每段信息都重要，但没有标记哪个是 LLM 必须遵守的（如保留 ID 列表），哪个是可参考的（如执行摘要）。

#### 2.6.5 影响

- 频繁出现 `depends on unknown step 's1'`
- LLM 有时生成全部步骤（s1,s2,s3），有时只生成剩余步骤（s2,s3），策略不一致
- 相同错误反复出现，没有进展

---

## 3. 解决方案

### 3.1 协议 A：变量解析协议

#### 3.1.1 设计目标

定义 Planner（非受信）和 DagExecutor（受信）之间的变量引用契约，确保 `$stepId.field` 在所有执行路径上正确解析。

#### 3.1.2 协议定义

```
1. 变量引用格式: $stepId[.fieldPath]
   示例: $s1, $s1.body, $s1.body.url
   
2. 上游输出暴露格式: upstream = {stepId: full_output}
   示例: upstream = {"s1": {"status_code": 200, "body": {"uuid": "..."}}}
   不: {dep_id}_result 前缀，不: 任何其他命名格式

3. 解析规则:
   - $s1 → 替换为 s1 的完整输出（dict）
   - $s1.body → s1 输出中的 body 字段
   - $s1.body.url → s1 输出中的 body.url 嵌套字段
   - 路径不存在 → 步骤失败（严格模式）
   - 上游步骤不存在 → 步骤失败（严格模式）

4. 解析策略: strict（默认）/ lenient
   - strict（默认）：任一引用无法解析，该步骤立即失败，不调 Executor
   - lenient：保留原值 + warning，继续执行
   - 策略由 DagStep.resolution_policy 声明，Executor 强制执行

5. 循环依赖检测:
   - VariableResolver 在解析前构建步骤间变量引用有向图
   - 检测环（DFS）：s1 引用 s2 output + s2 引用 s1 output → PlanGuardrail 拒绝
   - 同时依赖 depends_on 和变量引用两条边

6. 执行顺序:
   变量解析 → SchemaGuardrail（解析后的实际值校验）
```

#### 3.1.3 变更点

| 文件 | 行号 | 变更 | 类型 |
|------|------|------|------|
| `plan.py` | 75 | `merged[f"{dep_id}_result"]` → `merged[dep_id]` | 修复 |
| 新增 `harness/core/variable_resolver.py` | — | 独立 `VariableResolver` 组件 | 新增 |
| `dag_executor.py` | 226-279 | 委托给 `VariableResolver` | 重构 |
| `_execute_step_only` | 199-201 | 变量解析后增加校验步骤 | 新增 |

#### 3.1.4 VariableResolver 接口

```python
# harness/core/variable_resolver.py

class ResolutionStrategy(str, Enum):
    STRICT = "strict"       # 任一引用失败 → 步骤失败
    LENIENT = "lenient"     # 引用失败 → 保留原值 + warning

class VariableResolver:
    """集中式变量解析器。
    
    职责: 将 $stepId.field 引用替换为实际值。
    定位: 受信组件，在 Tool Layer 执行之前、Guardrail 校验之前运行。
    """
    
    REF_PATTERN = re.compile(r'^\$(\w+)(?:\.([\w.]+))?$')
    INLINE_PATTERN = re.compile(r'\$(\w+)(?:\.([\w.]+))?')
    
    @staticmethod
    def resolve(
        step_input: dict,
        upstream: dict[str, Any],  # key = step_id(bare), value = full output
        strategy: ResolutionStrategy = ResolutionStrategy.STRICT,
    ) -> tuple[dict, list[str]]:
        """解析输入中的所有变量引用。
        
        Args:
            step_input: DagStep.input（含 $ref 占位符）
            upstream: {step_id: output}，来自 plan.upstream_outputs()
            strategy: 解析策略，默认 strict
            
        Returns:
            (resolved_input, warnings)
            warnings: 未解析的引用说明
            
        Raises:
            VariableResolutionError: strict 模式下，有引用无法解析
        """

    @staticmethod
    def resolve_value(value: Any, upstream: dict, strategy: ResolutionStrategy = ResolutionStrategy.STRICT) -> Any:
        """解析单个值（递归支持 dict/list 嵌套）。"""
    
    @staticmethod
    def _resolve_ref(match: re.Match, upstream: dict) -> Any:
        """解析 $stepId.field 引用。"""
    
    @staticmethod
    def _resolve_inline(text: str, upstream: dict) -> str:
        """解析内联引用（如 'prefix_$s1.body_suffix'）。"""
    
    @staticmethod
    def check_cycles(plan: DagPlan) -> list[str]:
        """检测步骤间的变量引用隐式循环依赖。
        
        构建引用有向图（结合 depends_on + 变量引用），DFS 检测环。
        返回环的步骤 ID 列表。空列表 = 无环。
        """

class VariableResolutionError(Exception):
    """变量解析失败——strict 模式下抛出，步骤立即失败。"""
```

#### 3.1.5 测试

```python
class TestVariableResolverProtocol:
    """验证变量解析协议的全路径正确性"""
    
    async def test_upstream_outputs_key_matches_resolver(self, store, executor, registry):
        """upstream_outputs 真实输出 + resolver = 正确解析
        覆盖: plan.py→upstream_outputs → VariableResolver.resolve → 下游步骤 input"""
    
    async def test_resolve_body_uuid_integration(self, store, executor, registry):
        """$s1.body.uuid 通过完整 DAG 链正确解析"""
    
    async def test_resolve_missing_step_strict_fails(self):
        """strict 模式: 引用不存在的步骤 → VariableResolutionError"""
    
    async def test_resolve_none_field_strict_fails(self):
        """strict 模式: 字段不存在 → VariableResolutionError"""
    
    async def test_resolve_missing_step_lenient_warns(self):
        """lenient 模式: 引用不存在的步骤 → 保留原值 + warning"""
    
    async def test_cycle_detection_catches_implicit_ref_loop(self):
        """s1 引用 $s2.x + s2 引用 $s1.x: check_cycles 检测到环"""
    
    async def test_cycle_detection_passes_acyclic(self):
        """正常 DAG（无环）: check_cycles 返回空列表"""
```

### 3.2 协议 B：状态传播协议

#### 3.2.1 设计目标

定义 Tool Layer 的 6 种 ExecutionStatus 如何在 DAG 执行管线中保真传播，不因中间层的二值化折叠而丢失信息。

#### 3.2.2 协议定义

```
状态分类:
  ├─ 终态成功: COMPLETED, IDEMPOTENCY_HIT
  ├─ 终态失败: FAILED, TIMEOUT
  ├─ 规则拦截: GUARDRAIL_BLOCKED (可 revise)
  └─ 外部阻塞: CONFIRMATION_NEEDED (不可 revise，等待外部事件)

各层处理规则:
  Tool Layer → 产生 6 种 ExecutionStatus ✓（已有）
  DagExecutor._execute_step_only → 4 种 DagStepStatus（新增）
  DagExecutor._execute_layer → 3 种返回值（扩充）
  Scheduler._execute_static_plan → 3 路分支（新增）
```

#### 3.2.3 DagStepStatus 定义

```python
# harness/models/plan.py 或 新增 harness/models/execution.py

class DagStepStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"                  # 异常/超时
    GUARDRAIL_BLOCKED = "guardrail_blocked"  # 规则拦截
    CONFIRMATION_NEEDED = "confirmation_needed"  # 外部阻塞

@dataclass
class StepExecutionResult:
    """DAG 步骤执行的结果——结构化状态传播单元"""
    step_id: str
    status: DagStepStatus
    output: Any = None
    error: str | None = None
    retryable: bool = False
    confirmation_id: str | None = None
    guardrail_id: str | None = None
    summary: str = ""
```

#### 3.2.4 变更点

| 文件 | 行号 | 变更 | 类型 |
|------|------|------|------|
| `dag_executor.py` | 216-223 | `_execute_step_only` 返回 4 种状态而非 2 种 | 修复 |
| `dag_executor.py` | 134-184 | `_execute_layer` 返回 3 类(True/False/CONFIRMATION_NEEDED) | 增强 |
| `scheduler.py` | 804-921 | `_execute_static_plan` 增加确认分支 | 新增 |
| `scheduler.py` | 626-701 | `_plan_execute_revise_loop` 增加确认处理 | 新增 |

#### 3.2.5 关键代码变更

**`_execute_step_only()`** 改为全状态映射：

```python
# dag_executor.py:216-223
status_map = {
    ExecutionStatus.COMPLETED: DagStepStatus.COMPLETED,
    ExecutionStatus.IDEMPOTENCY_HIT: DagStepStatus.COMPLETED,
    ExecutionStatus.FAILED: DagStepStatus.FAILED,
    ExecutionStatus.TIMEOUT: DagStepStatus.FAILED,
    ExecutionStatus.GUARDRAIL_BLOCKED: DagStepStatus.GUARDRAIL_BLOCKED,
    ExecutionStatus.CONFIRMATION_NEEDED: DagStepStatus.CONFIRMATION_NEEDED,
}
status = status_map.get(result.status, DagStepStatus.FAILED)
if status == DagStepStatus.COMPLETED:
    return {"status": "completed", "output": result.output, "summary": summary}
elif status == DagStepStatus.CONFIRMATION_NEEDED:
    return {"status": "confirmation_needed", "confirmation_id": result.confirmation_id}
elif status == DagStepStatus.GUARDRAIL_BLOCKED:
    return {"status": "guardrail_blocked", "guardrail_id": result.guardrail_id}
else:
    return {"status": "failed", "error": error, "retryable": result.retryable}
```

**`_execute_layer()`** 确认特殊处理：

```python
# dag_executor.py:134-184
confirmed_steps = [r for r in raw_results if isinstance(r, dict) and r.get("status") == "confirmation_needed"]
if confirmed_steps:
    _log.info("[layer] %d step(s) need confirmation — pausing", len(confirmed_steps))
    return "CONFIRMATION_NEEDED"
any_failed = any(isinstance(r, Exception) or (isinstance(r, dict) and r.get("status") in ("failed", "guardrail_blocked")) for r in raw_results)
if any_failed:
    return False
return True
```

**`_execute_static_plan()`** 三路分支：

```python
# scheduler.py:830-868
result = await self.dag_executor.execute_layer(...)
if result == "CONFIRMATION_NEEDED":
    _sched_act.info("[execute] Steps need confirmation — pausing")
    await self.store.append_event(
        run_id, EventType.RUN_PAUSED,
        RunPausedPayload(reason="waiting_confirmation").model_dump(),
    )
    await self._wait_for_resume(run_id)
    # 注意: 不调 revise，不消耗 LLM
    # 确认后通过 idempotency key 重试
    continue
elif result is False:
    # 真正的失败 → 调 revise
    ...
else:
    # 正常完成
    ...
```

#### 3.2.6 确认事件源机制

**现有实现已覆盖**（无需重新设计）：

- 确认机制在 `executor.py:156-223`：查询 SQLite 中 CONFIRMATION_RECEIVED 事件
- 事件查询在 `event_store.py:328-345`：`find_confirmation_by_id()` 按 `run_id + confirmation_id` 索引
- REST 端点（L6）写 CONFIRMATION_RECEIVED，Scheduler `_wait_for_resume` 轮询消费

**新增 PAUSED 保护**：在 `_plan_execute_revise_loop`（行 636-639）已有 PAUSED 检查，但 `_execute_static_plan` 的 revise 分支（行 837-868）缺少——在调 revise 前加：

```python
state = await self._refresh_state(run_id)
if state.status == RunStatus.PAUSED:
    return self._pending_plan_pause(state)
```

#### 3.2.7 确认状态机

```
  ┌──────────┐    工具需要确认    ┌─────────────┐
  │ RUNNING  │ ────────────────▶ │ WAITING_     │
  │ (DAG执   │                   │ CONFIRMATION │
  │  行中)   │                   │             │
  └──────────┘                   └──────┬──────┘
       ▲                               │
       │  resume + 重试                 │  Operator
       │  (idempotency key)             │  确认/拒绝
       │                               ▼
       │                        ┌──────────────┐
       │                        │ 确认 → 继续   │
       │                        │ 拒绝 → FAILED│
       │                        └──────────────┘
       │
       │  不涉及 Planner!
       │  不消耗 LLM!
```

**IDEMPOTENCY_HIT 在 DAG 路径的语义**：无需变更。幂等键基于 `tool_name + 已解析 input` 计算，DAG 路径先解析变量再传 input，所以 IK 正确包含依赖值。同层无关步骤工具名或 input 不同 → IK 不碰撞。恢复后相同输入 → 相同 IK → IDEMPOTENCY_HIT。

#### 3.2.8 测试

```python
class TestConfirmationInDagPath:
    """DAG 路径下的确认流程——验证状态传播协议"""
    
    async def test_dag_step_confirmation_pauses(self, store, executor, registry):
        """需要确认的步骤: pause, 不调 revise, 不消耗 LLM"""
    
    async def test_dag_confirmation_continue_after_approval(self, store, executor, registry):
        """确认后: RUN_RESUMED → 重试被阻塞的步骤(通过 ik) → 正常继续"""
    
    async def test_dag_confirmation_denied_terminates(self, store, executor, registry):
        """拒绝后: TOOL_FAILED → RUN_COMPLETED (不再重试)"""
    
    async def test_mixed_layer_confirmation_and_completed(self, store, executor, registry):
        """同层混合: 一些完成 + 一些待确认 → 整体 pause"""
```

### 3.3 协议 C：监控控制协议

#### 3.3.1 设计目标

赋予 Monitor 不依赖 LLM 配合的硬控制能力——写入控制指令，Scheduler 强制执行。

**审查决策背景**：
- Monitor 当前监听 TOOL_FAILED/GUARDRAIL_TRIGGERED（`monitor.py:88`），但 DAG 路径写的是 DAG_STEP_FAILED → `_consecutive_failures` 始终为 0 → 所有干预在 DAG 路径下失效（审查发现的实际代码问题，审查者未提及）
- 监控指令通过 PlanGuardrail 强制执行（系统强制），不依赖 Planner 主动遵守

#### 3.3.2 协议定义

```
数据类型:
  - 反馈 FeedbackInjected(已有): 文本，给 LLM 参考，非强制
  - 指令 RunCommand(新增): 结构化控制信号，Scheduler 强制执行
  
Scheduler 消费点:
  _plan_execute_revise_loop：每个循环周期开始时检查
  _execute_static_plan：每层执行后检查
  revise 前：检查是否有终止/暂停指令
```

#### 3.3.3 RunCommand 事件

```python
# harness/models/events.py 新增

class RunCommandType(str, Enum):
    HARD_ABORT = "hard_abort"       # 立即终止（不可取消）
    SOFT_ABORT = "soft_abort"       # 当前步骤完成后再终止
    PAUSE_EXECUTION = "pause"       # 暂停执行
    SKIP_TOOL = "skip_tool"         # 标记某工具在当前运行中不可用
    LOWER_PARALLEL = "lower_parallel"  # 降低某工具并发度

class RunCommandPayload(BaseModel):
    command: RunCommandType
    reason: str
    affected_tool: str | None = None
    target_run_id: str = ""
```

#### 3.3.4 Monitor 增强

**DAG_STEP_FAILED 事件监听**（`run_monitor.py:88` 必须修复的 bug）：

```python
# 当前只监听 TOOL_FAILED/GUARDRAIL_TRIGGERED
if event.event_type in (EventType.TOOL_FAILED, EventType.GUARDRAIL_TRIGGERED, EventType.DAG_STEP_FAILED):
    #                    新增 DAG_STEP_FAILED ↑
```

修复后 Monitor 在 DAG 路径下也能跟踪 `_consecutive_failures`。

**运行级阈值配置**：

```python
@dataclass
class MonitorConfig:
    max_consecutive_failures: int = 3     # 运行级，默认3次
    max_revise_count: int = 5              # 最大 revise 次数
    confirmation_timeout_s: int = 300      # 确认等待超时
    escalation_policy: EscalationPolicy = EscalationPolicy.DEFAULT

class EscalationPolicy(str, Enum):
    DEFAULT = "default"         # 3→5: feedback → soft_abort → hard_abort
    CONSERVATIVE = "conservative"  # 2→3: 更快升级
    RELAXED = "relaxed"         # 5→10: 更慢升级
```

**DAG 级 revise 模式检测**（`run_monitor.py` 新增）：

```python
class RunMonitor:
    _revise_history: dict[str, list[dict]] = {}  # run_id → [{"steps_hash": str, "errors": list}]
    _abort_requests: dict[str, str] = {}  # run_id → reason, Monitor 内存标记
    
    def _get_revise_signature(self, payload: dict) -> str:
        """从 PLAN_REVISED payload 生成 signature"""
        ...
    
    def _detect_revise_loop(self, run_id: str) -> bool:
        """检测 revise 循环：最近 N 次 signature 是否相同"""
        history = self._revise_history.get(run_id, [])
        recent = history[-5:]
        if len(recent) < 3:
            return False
        # 检查最近 3 次是否相同
        sigs = [h["signature"] for h in recent[-3:]]
        return len(set(sigs)) == 1
```

**反馈升级为指令**（分层干预）：

```python
# run_monitor.py
async def _escalate(self, run_id: str, level: int, reason: str):
    """分层升级"""
    match level:
        case 1:
            # 文本反馈（已有）
            await self._inject_feedback(...)
        case 2:
            # 软指令：本轮完成后终止
            await self._inject_command(...)
        case 3:
            # 硬指令：立即终止
            await self._inject_command(...)
```

**完整 detect→escalate 链**：

```python
# 重复 revise 检测
if self._detect_revise_loop(run_id):
    count = len(self._revise_history[run_id])
    if count >= 5:
        await self._escalate(run_id, 3, f"Revise loop: {count} identical plans")
    elif count >= 3:
        await self._escalate(run_id, 2, f"Repeated revise ({count}x), same plan pattern")
    else:
        await self._escalate(run_id, 1, f"Observing repeated revise pattern ({count}x)")
```

#### 3.3.5 PlanGuardrail 指令验证（系统强制）

Monitor 的指令由 **PlanGuardrail 强制执行**，不依赖 Planner 主动遵守：

```python
# planner.py:PlanGuardrail.validate()
class PlanGuardrail:
    """校验计划是否遵守活跃的控制指令。"""

    def validate(self, plan: DagPlan, commands: list[RunCommand]) -> list[str]:
        errors = []
        for cmd in commands:
            if cmd.command == RunCommandType.SKIP_TOOL:
                for step in plan.steps:
                    if step.tool == cmd.affected_tool:
                        errors.append(
                            f"Tool '{step.tool}' is blocked by active command. "
                            f"Reason: {cmd.reason}"
                        )
        return errors
```

Scheduler 在每次 revise 前将活跃指令注入 PlanGuardrail，违反指令的计划被拦截。

#### 3.3.6 终止后的终态处理协议

当前实现：终止时只写 RUN_FAILED（scheduler.py:920-921），无沙箱清理、无 partial_results。

**新增 RunTerminated 事件**：

```python
class RunTerminatedPayload(BaseModel):
    termination_reason: str           # "hard_abort" / "quota_exhausted" / "operator_cancelled"
    cleanup_status: str | None = None # "clean" / "partial" / "failed"
    partial_results: dict[str, Any] = Field(default_factory=dict)  # 已完成步骤的输出
    completed_steps: int = 0
    total_steps: int = 0

# 终止协议：
# 1. 写 RUN_TERMINATED 事件
# 2. 清理 Sandbox 进程（Scheduler 调 clean_running_processes）
# 3. 调 Monitor.cleanup()
# 4. Answer 基于 RUN_TERMINATED 事件生成（不编造成功）
```

#### 3.3.7 Scheduler 增强

#### 3.3.8 测试

```python
class TestMonitorControlProtocol:
    async def test_dag_step_failed_triggers_monitor(self, store):
        """DAG_STEP_FAILED 事件触发 Monitor _consecutive_failures 计数器"""
    
    async def test_hard_abort_terminates_immediately(self, store):
        """HARD_ABORT → 正在执行的 DAG 被终止"""
    
    async def test_revise_loop_detection_three_strikes(self, store):
        """3 次相同 revise → SOFT_ABORT"""
    
    async def test_revise_loop_detection_five_strikes(self, store):
        """5 次相同 revise → HARD_ABORT"""
    
    async def test_skip_tool_blocked_by_guardrail(self, store):
        """SKIP_TOOL → PlanGuardrail 拒绝使用该工具的新计划"""
    
    async def test_escalation_from_feedback_to_abort(self, store, monitor):
        """反馈 → 软指令 → 硬指令 自动升级"""
    
    async def test_run_terminated_includes_partial_results(self, store):
        """硬终止后: RunTerminated 包含已完成步骤的输出"""
```

### 3.4 协议 D：Revise 上下文协议

#### 3.4.1 设计目标

定义 Planner revise 时接收的结构化上下文格式，确保 LLM 有足够信息做出正确决策，同时避免信息过载。

#### 3.4.2 协议定义

```
必含段:
  1. Execution Summary     — 执行状态摘要（全部步骤的状态）
  2. Available Outputs    — 已完成步骤的输出值
  3. Reserved Step IDs    — 已被占用的 step ID 列表（LLM 不可复用）
  4. Revise Counter       — 当前 revise 次数 + 每次的 reason
  
可选段:
  5. Failed Details       — 失败步骤的详细错误
  6. Pending Confirmations — 待确认列表
```

#### 3.4.3 输出格式

```
【系统状态 - 不可折叠】
## Execution Summary
- Intent: search weather and save
- Total steps: 3 | Completed: 1 | Failed: 1 | Blocked: 0
- Step s1(echo): [completed]  output: {"echo": "hello"}
- Step s2(http_request): [completed]  output: {"status_code": 200}
- Step s3(file_op): [failed]  error: permission denied

## Available Upstream Outputs
- s1 → {"echo": "hello"}
- s2 → {"status_code": 200, "body": {"uuid": "abc-123"}}

## Reserved Step IDs
s1, s2, s3 — do not reuse. New steps must use s4, s5, etc.

## Revise History
- Revise #0 (original plan): 3 steps
- Revise #1 (after s3 failed): this is current
- Revise count: 1 so far
```

#### 3.4.4 Prompt 增强

```python
# planner.py:_REVISE_PROMPT_TMPL 新增段

_REVISE_PROMPT_FRAGMENTS = {
    "reserved_ids": (
        "\n## Reserved Step IDs\n"
        "{reserved_ids}\n"
        "这些 step ID 已经被使用。新的 revise 步骤必须使用更大的 ID "
        "（如 {next_id}, {next_id_next}）。\n"
    ),
    "revise_counter": (
        "\n## Revise Counter\n"
        "你已修订计划 {count} 次。如果同一步骤反复失败，请换一种方法。\n"
    ),
}
```

#### 3.4.5 PlanGuardrail 自动依赖化解 + ID 冲突处理

```python
# planner.py:PlanGuardrail.validate()
class PlanGuardrail:
    def __init__(self, reserved_ids: set[str] | None = None):
        self.reserved_ids = reserved_ids or set()
    
    def validate_and_fix(self, plan: DagPlan) -> DagPlan:
        """校验并修复计划。
        
        1. ID 冲突检测: 如果 Planner 使用已占用的 ID → 自动重命名
        2. 已完成依赖化解: 引用已完成步骤 → 自动解除（仅 warning）
        """
        id_map = {}  # old_id → new_id
        for step in plan.steps:
            if step.id in self.reserved_ids:
                new_id = self._next_available_id()
                id_map[step.id] = new_id
                _log.warning("Step ID '%s' conflicts with reserved ID — renamed to '%s'", step.id, new_id)
                step.id = new_id
        
        for step in plan.steps:
            for i, dep in enumerate(step.depends_on):
                if dep in id_map:
                    step.depends_on[i] = id_map[dep]
                elif dep not in self.reserved_ids:
                    errors.append(f"Step '{step.id}': depends on unknown step '{dep}'")
        
        return plan
```

#### 3.4.6 变更点

| 文件 | 行号 | 变更 | 类型 |
|------|------|------|------|
| `dag_executor.py` | 293-336 | `build_dag_status_text` 新增段 | 增强 |
| `planner.py` | 114-134 | `_REVISE_PROMPT_TMPL` 新增 reserved_ids 段 | 增强 |
| `planner.py` | 145-195 | PlanGuardrail 自动化解已完成依赖 | 增强 |
| `scheduler.py` | 899-912 | revise 前注入 revise 计数 | 新增 |

#### 3.4.7 测试

```python
class TestReviseProtocol:
    async def test_reserved_ids_in_prompt(self, store, registry):
        """revise prompt 中包含 Reserved Step IDs 段"""
    
    async def test_auto_resolve_completed_deps(self, registry):
        """已完成步骤的依赖不触发 guardrail 错误"""
    
    async def test_revise_counter_tracks_across_cycles(self, store, executor, registry):
        """多次 revise 后计数正确"""
    
    async def test_context_includes_failed_details(self, store, executor, registry):
        """失败的步骤包含详细错误信息"""
```

---

## 4. 解决 Guardrail Schema 放宽（独立修复）

除上述 4 个协议外，有一个独立修复——`http_request` 的 `body` 字段 Schema 过严。

### 4.1 问题

```python
# http_request.py:28-31
"body": {
    "type": "object",
    "description": "JSON body for POST/PUT/PATCH requests",
}
```

严格 `object` 类型，不支持：
- 字符串 XML body
- 原始 JSON 字符串
- `null`（GET 请求显式不发送 body）

### 4.2 修复

```python
# http_request.py:28-31
"body": {
    "oneOf": [
        {"type": "object"},
        {"type": "string"},
        {"type": "array"},
        {"type": "null"},
    ],
    "description": "Request body for POST/PUT/PATCH requests. Use object for JSON, string for raw/XML.",
}
```

运行时处理（`http_request_fn`）：

```python
if body is not None:
    if isinstance(body, dict):
        req_kwargs["json"] = body       # JSON 序列化
    elif isinstance(body, str):
        req_kwargs["content"] = body     # 原始字符串
        req_kwargs.setdefault("headers", {})["Content-Type"] = "text/plain"
    elif isinstance(body, (list, tuple)):
        req_kwargs["json"] = body        # JSON 序列化数组
```

### 4.3 测试

```python
class TestHttpRequestBodyTypes:
    async def test_object_body(self): ...
    async def test_string_body(self): ...
    async def test_xml_body(self): ...
    async def test_null_body_get(self): ...
```

---

## 5. 实施路线图

### 5.1 优先级与依赖

```
Phase 1 — 协议 A 数据平面（变量解析）
  依赖: 无
  文件: plan.py:75, 新增 variable_resolver.py, dag_executor.py
  测试: +7
  效果: 变量引用从"永远失败"变为"正常工作"
  新增: strict/lenient 策略、循环依赖检测

Phase 2 — 协议 B 控制平面（状态传播）
  依赖: Phase 1（确认流程需要变量解析正常才能验证）
  文件: dag_executor.py, scheduler.py, models/execution.py
  测试: +5
  效果: 确认从"死循环"变为"阻塞等待外部事件"
  新增: PAUSED 状态检查（revise 前）

Phase 3 — 协议 D Revise 上下文
  依赖: 无（与 Phase 1/2 并行）
  文件: dag_executor.py, planner.py
  测试: +4
  效果: revise 质量提升，ID 碰撞消除
  新增: PlanGuardrail ID 冲突自动处理

Phase 4 — 协议 C 监控控制
  依赖: Phase 2（熔断需要状态传播到位）
  文件: events.py, run_monitor.py, scheduler.py, planner.py
  测试: +7
  效果: 监控从"形同虚设"变为"有控制力"
  新增: DAG_STEP_FAILED 监听修复、配置化阈值、RunTerminated 事件、PlanGuardrail 指令验证

Phase 5 — Guardrail Schema 放宽
  依赖: 无（独立修复）
  文件: http_request.py
  测试: +4
  效果: XML body 和变量引用字符串不再被拦截

Phase 6 — 错误分类 + Answer 校验（新增）
  依赖: 无（独立修复）
  文件: models/execution.py, scheduler.py
  测试: +3
  效果: 系统错误不触发无意义 revise；Answer 不编造成功结果
```

### 5.2 测试基线

| 阶段 | 新增测试 | 累积 | 验证方式 |
|------|----------|------|----------|
| 当前 | — | 338 | `pytest tests/ -v` |
| Phase 1 | 7 | 345 | 变量解析集成 + 循环检测 |
| Phase 2 | 5 | 350 | 确认流程 DAG 测试 |
| Phase 3 | 4 | 354 | Revise 上下文测试 |
| Phase 4 | 7 | 361 | Monitor 控制指令 + RunTerminated 测试 |
| Phase 5 | 4 | 365 | body 多类型测试 |
| Phase 6 | 3 | 368 | 错误分类 + Answer 一致性测试 |

### 5.3 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| Phase 1 变量解析改为 strict 可能破坏现有依赖 strict 模式的计划 | 现有 Planner 产生的计划中有未解析引用 → 步骤全部失败 | 初始阶段使用 lenient + warning，等 Planner 适应后再切 strict |
| Phase 2 中 `_execute_layer` 返回值类型变更 | 调用方期望 bool 但收到 str | 使用 3 值枚举而非 3 种不同返回类型 |
| Phase 4 中 DAG_STEP_FAILED 监听可能导致 Monitor 双重计数 | DAG 路径同时写 TOOL_FAILED + DAG_STEP_FAILED？ | 确认 DAG 路径不写 TOOL_FAILED（当前代码已验证只写 DAG_STEP_FAILED） |

---

## 6. 附录

### 6.1 问题与协议映射表

| 日志问题 | 对应缺口 | 解决方案 | 优先级 |
|----------|----------|----------|--------|
| 变量引用解析失败 | A 数据平面缺失 | Phase 1: key 命名统一 + VariableResolver | P0 |
| 确认死循环 | B 控制平面缺失 | Phase 2: 状态保真传播 + 确认状态机 | P0 |
| Guardrail 与工具不匹配 | D 未定义顺序 + 独立 Schema | Phase 5: Schema 放宽 | P1 |
| Revise 上下文混乱 | E Revise 协议缺失 | Phase 3: 结构化上下文 + ID 冲突处理 | P1 |
| 监控反馈未生效 | C 反馈平面缺失 | Phase 4: 控制指令 + DAG_STEP_FAILED 监听修复 | P2 |
| Answer 信息不完整 | — | Phase 6: 意图-结果一致性校验 | P2（从 P3 上调） |
| LLM 配额（已暂缓） | — | 暂缓（用户指定） | — |
| 系统错误触发无意义 revise | — | Phase 6: 系统/业务/用户错误分类 | P1（新增，低成本高价值） |
| Monitor DAG 路径失效 | C 反馈平面泄露 | Phase 4: DAG_STEP_FAILED 加入监听 | P0（审查发现的 bug） |

### 6.2 现有测试覆盖与缺口

| 测试文件 | 覆盖 | 未覆盖 |
|----------|------|--------|
| `test_dag_executor.py` (224行) | 独立解析函数、层执行、事件顺序 | `upstream_outputs`→`resolver` 集成、确认流程 |
| `test_planner.py` (190行) | 解析、Guardrail、feedback | Revise 上下文协议、step ID 碰撞 |
| `test_scheduler.py` (660行) | 串行路径确认、暂停/恢复、breaker | DAG 路径确认、DAG 路径 breaker |
| `test_monitoring.py` (384行) | 事件监听、反馈注入、token 预警、清理 | DAG 级检测、控制指令、revise 模式检测 |

---

## 7. 审查决策变更摘要

2026-06-08 代码交叉审查后，对原 v1.0 方案做出以下决策：

### 7.1 ACCEPT（落实进文档）

| ID | 审查建议 | 具体变更 |
|----|----------|----------|
| D-030 | `resolve()` 默认 strict 模式 | §3.1.2 协议定义，§3.1.4 `ResolutionStrategy` |
| D-031 | 循环依赖检测 | §3.1.2 §3.1.4 `check_cycles()` |
| D-032 | 暂停/恢复并发保护 | §3.2.6 revise 前增加 PAUSED 状态检查 |
| D-033 | Monitor 阈值可配置 | §3.3.4 `MonitorConfig` |
| D-034 | RunTerminated 事件 | §3.3.6 `RunTerminatedPayload` |
| D-035 | PlanGuardrail 指令验证 | §3.3.5 违反指令的计划被拦截 |
| D-036 | PlanGuardrail ID 冲突自动处理 | §3.4.5 `validate_and_fix()` |
| D-037 | 错误分类（SYSTEM/BUSINESS/USER_ERROR） | Phase 6（§5.1 新增） |
| D-038 | Answer 一致性校验升级 P2 | Phase 6（§6.1 更新） |

### 7.2 REJECT（不纳入方案）

| ID | 审查建议 | 驳回理由 |
|----|----------|----------|
| D-040 | 大对象惰性传递 | 预优化。当前输出仅单层执行驻留内存，未发现 OOM |
| D-041 | IDEMPOTENCY_HIT DAG 语义问题 | 代码验证：IK 基于已解析 input 计算，DAG 路径正确 |
| D-042 | 异步事件网关 | 现有 SQLite 查询（`find_confirmation_by_id`）已满足需求 |
| D-043 | 输出隐私字段白名单 | Scope creep。工具控制自己的 output schema |
| D-044 | 可观测性异常评分 | L7 范畴，不在 L2-L3 协议修复中 |
| D-045 | 资源配额管理 | 无证据的预优化 |
| D-046 | LLM 故障降级 | 用户明确排除了 Issue 6 |

### 7.3 PARTIALLY ACCEPT（在方案内做简化版）

| ID | 审查建议 | 简化方案 |
|----|----------|----------|
| D-050 | 外部确认事件源 | 确认机制已存在（executor.py:156-223），L6 增加 REST 端点写 CONFIRMATION_RECEIVED |
| D-051 | Planner 对抗性 | 不修复 Planner 行为。PlanGuardrail 强制执行指令（§3.3.5） |
| D-052 | ID 生成策略 | 不委托给受信生成器。Guardrail 检测冲突后自动重命名（§3.4.5） |
| D-053 | 运行状态机 | 在文档中定义状态转换图（已有），不重构代码 |

### 7.4 代码审查发现但审查者未提及的问题

| ID | 问题 | 代码定位 | 修复 |
|----|------|----------|------|
| D-060 | Monitor DAG 路径失效 | `monitor.py:88` 未监听 DAG_STEP_FAILED | Phase 4: DAG_STEP_FAILED 加入事件过滤 |

---

*本文档基于 harness.log 运行期日志分析 + 实际代码审查 + 现有架构文档交叉分析生成。*
*版本 v2.0 引入了 2026-06-08 代码交叉审查的 14 项决策决议（7 ACCEPT / 6 REJECT / 4 PARTIAL + 1 审查发现）。*
*每个解决方案均定位到具体的文件、行号、接口和测试用例。*
