# Quality Evaluator — 设计文档 v1.0

> **文档类型**: 架构设计（预实现）
> **关联架构**: `ARCHITECTURE_v2.1.md`
> **创建日期**: 2026-07-03
> **状态**: 待审查

---

## 1. 背景与动机

### 1.1 当前问题

JAgent 现有的质量保障体系由三层组成：

| 层级 | 组件 | 覆盖范围 | 局限性 |
|------|------|----------|--------|
| **系统层** | Guardrails、Schema 校验、Idempotency 查重 | 输入合法性、操作安全性、输出结构 | 只检查"步骤对不对"，不检查"结果好不好" |
| **提示层** | SYSTEM_PROMPT 中的 Rules、Examples | 引导 Agent 少犯错 | 完全依赖 LLM 遵守，无强制力 |
| **监控层** | RunMonitor 异常检测 | 连续失败、Token 超量、重复调用 | 只检测异常模式，不评估语义质量 |

**盲区：语义正确性。** 当一个 Run 执行完成后，系统能回答：
- ✅ 工具调了几次、有没有超时、Guardrail 有没有触发
- ❌ 用户要求的三件事，Agent 到底做完了没有
- ❌ 最终答案里的数字/路径，和工具实际返回的数据一致吗
- ❌ 工具返回了 SOFT_ERROR，这个结果实际上能用吗

当前 `state.status == COMPLETED` 意味着"执行过程没出系统错误"，不意味着"用户需求被满足"。

### 1.2 业务场景

一个用户请求"帮我查一下 AWS 东京区 EC2 价格，和新加坡区做个对比，输出表格"：

- Plan 生成了 2 个 `http_request` 步骤
- 两个步骤都返回了 `ToolCompleted`（status_code=200，body 有数据）
- Scheduler 标记 `COMPLETED`

**但实际上**：东京区请求因为 API key scope 不够，返回的 body 里是 `{"error": "unauthorized"}`。Agent 没识别出来，照样生成了含"N/A"的对比表格。

当前系统无法在语义层面发现这个问题——因为工具返回了 200，系统判定为成功。

---

## 2. 目标

### 2.1 核心目标

在 JAgent 的执行管道**之外**，建立一个**独立的语义质量评估组件**，在 Run 完成后对执行结果进行回顾性检查。

### 2.2 设计原则

1. **不改变执行结果**：Quality Evaluator 是纯观测组件，不修改 Run 状态（COMPLETED 还是 COMPLETED），不向 Agent 注入新指令。评测结果对执行管道透明。

2. **不在 Scheduler 内耦合**：Evaluator 通过 Event Store 的 `on_append` 回调机制获得事件通知，不 import Scheduler、Planner、Executor。Scheduler 不知道 Evaluator 的存在。

3. **可扩展、可替换**：检查项是独立的小组件（`QualityCheck`），新增检查不修改已有代码。Rule 检查和 LLM 检查共享同一接口，可随时切换。

4. **不影响执行性能**：LLM 检查通过 `asyncio.create_task()` 异步执行，不阻塞事件写入和主循环。检查失败不影响 Run 的完成。

5. **对前端透明、对运营可观测**：检查结果以标准 `QualityCheckCompleted` 事件写入 Event Store，前端通过已有的事件流 WebSocket 订阅即可展示。

### 2.3 不做什么

- ❌ 根据质量评分自动重试或修改 plan（自愈属于 Planner/Revise，不是 Evaluator）
- ❌ 根据质量评分改变 Run 状态
- ❌ 实时中断正在执行的步骤
- ❌ 替代现有的 Guardrails 或 Monitor（它们是执行期防护，Evaluator 是回顾期评估）
- ❌ 评估 Agent 的推理过程质量（只评估可观测的执行结果）

---

## 3. 产品范围

### 3.1 第一期交付（MVP）

两个检查项，覆盖最核心的两个质量维度：

| 检查项 | 类型 | 触发时机 | 评估维度 |
|--------|------|----------|----------|
| **Step Completeness** | Rule 检查（零成本） | 每个 `ToolCompleted` 事件 + `RunCompleted` | 工具执行的结构完整性：每个 tool_call 都有对应 result？result 里有必需字段吗？ |
| **Answer Accuracy** | LLM 检查（~3s，~2K tokens） | `RunCompleted` 事件 | 最终答案和工具执行结果的一致性：答案里的数字/路径/结论和 tool 实际返回的数据一致吗？有没有幻觉？ |

### 3.2 后续规划（待一期数据验证后决定）

| 检查项 | 类型 | 触发时机 | 评估维度 |
|--------|------|----------|----------|
| Plan Coverage | LLM | `PlanCreated` 事件 | Plan 是否覆盖了用户的全量要求 |
| Step Effectiveness | LLM + Rule | `ToolCompleted`（仅 SOFT_ERROR） | SOFT_ERROR 的结果实际上还能用吗 |
| Compression Fidelity | Rule | `ContextCompressed` 事件 | 压缩前后关键信息丢失率 |
| Token Efficiency | Rule | `RunCompleted` 事件 | 每完成一个用户需求平均消耗了多少 token |

### 3.3 用户视角

**前端展示**：Run 详情页底部新增"质量评估"区域，展示最近一次检查的 verdict（pass / warn / fail）、score、issues 列表。

**分析 API**：已有的 `AnalysisService` 端点可以按 `check_id` / `verdict` 聚合，生成质量趋势报告。

**不会发生的事**：
- 用户不会看到"因为质量评分低，Run 被标记为失败"
- 用户不会看到 Evaluator 对 Agent 的行为产生任何影响
- 运营人员可以在 Dashboard 上看到"过去 24 小时，30% 的 Answer 存在准确性警告"

---

## 4. 系统架构

### 4.1 顶层架构图（更新后）

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Interface Layer (API)                             │
│   REST: /api/v1/runs CRUD  │  WS: /api/v1/runs/{id}/events               │
│   REST: /api/v1/analysis/* │  POST /confirm /pause /resume /cancel       │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────┐
│                     Scheduler Layer (L3, 受信)                            │
│                                                                          │
│   ┌──────────────────────┐    ┌──────────────────────────────────────┐   │
│   │ AgentLoopScheduler   │    │ PlanningExecutorScheduler (V0.7)     │   │
│   │ (Think→Act→Observe)  │    │ (Plan→Execute→Revise→Answer)        │   │
│   └──────────────────────┘    └──────────────────────────────────────┘   │
│                                                                          │
│   ┌──────────────────────────────────────────────────────────────┐       │
│   │ ContextManager (受信) ← 自动压缩 + Checkpoint                │       │
│   └──────────────────────────────────────────────────────────────┘       │
└──────────────────────────────────────────────────────────────────────────┘
                           │
                           │ 写入事件                                       │
                           ▼                                               │
┌──────────────────────────────────────────────────────────────────────────┐
│                      Tool Layer (L2, 受信)                               │
│   ToolExecutor (8-step flow)  │  Guardrails  │  Idempotency  │  Sandbox  │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
                           │ 写入事件
                           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                   Event Store (L1, 受信, Append-Only)                    │
│                                                                          │
│   SQLite (开发) / PostgreSQL + JSONB (生产)                              │
│   on_append 回调 → WS 广播 + Monitor + Evaluator 事件驱动                │
└────┬──────────────┬──────────────────────────────────────────────────────┘
     │              │
     │ ① on_append  │ ① on_append
     ▼              ▼
┌──────────────┐  ┌──────────────────────────────────────────────────────┐
│  RunMonitor  │  │  QualityEvaluator (NEW — 观察层，受信)                │
│  (受信)       │  │                                                     │
│              │  │  订阅 RunCompleted / ToolCompleted 事件               │
│  连续失败检测  │  │                                                     │
│  Token 超量   │  │  ┌──────────────────────────────────────────────┐   │
│  重复调用检测  │  │  │ EvaluatorRunner                              │   │
│              │  │  │   dispatch(event) → filter(trigger_events)     │   │
│      ↓       │  │  │   → asyncio.create_task(evaluate)              │   │
│  Feedback-   │  │  │   → 写 QualityCheckCompleted 事件              │   │
│  Injected    │  │  └──────────────────────────────────────────────┘   │
│  (→ Agent)   │  │                                                     │
│              │  │  ┌──────────────┐  ┌──────────────┐                 │
│              │  │  │ StepComplete-│  │ AnswerAccuracy│                │
│              │  │  │ nessCheck    │  │ Check (LLM)   │                │
│              │  │  │ (Rule)       │  │              │                 │
│              │  │  └──────────────┘  └──────────────┘                 │
│              │  │                                                     │
│              │  │  输出：QualityCheckCompleted 事件 (→ 前端 Dashboard) │
│              │  └──────────────────────────────────────────────────────┘
│              │
│      两者互不依赖，独立运行
└──────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│                      Agent Kernel (L4, 非受信)                            │
│   LLM 推理 → 生成 plan / thought / answer                               │
└──────────────────────────────────────────────────────────────────────────┘
```

### 4.2 关键架构决策

#### 决策 1：独立 Observer，不并入 Monitor

**理由**：
- Monitor 是有状态组件（维护 `_consecutive_failures`、`_token_totals` 等计数器），Evaluator 是无状态纯函数（每次 `fold_events()` 从事件流重新计算）
- Monitor 输出 `FeedbackInjected`（闭环，影响 Agent 行为），Evaluator 输出 `QualityCheckCompleted`（开环，只供人观看）
- Monitor 的 `_on_event` 在 `append_event` 中同步 await，不可有延迟操作；Evaluator 通过 `create_task` 异步执行，LLM 调用不阻塞事件写入
- 职责混淆导致组件膨胀

**替代方案考虑过但拒绝**：在 Scheduler 的 `run()` finally 块中触发。拒绝原因：Scheduler 不应知道质量检查组件存在，引入向前不兼容——新增 Scheduler 类型需要加参数。

#### 决策 2：触发器绑定到生命周期事件

QualityCheck 不写"我什么时候运行"的逻辑代码，而是声明式地绑定到事件类型：

```
StepCompletenessCheck.trigger_events = [TOOL_COMPLETED, RUN_COMPLETED]
AnswerAccuracyCheck.trigger_events  = [RUN_COMPLETED]
```

`EvaluatorRunner` 收到事件后，按 `trigger_events` 分发。新增检查只需声明新的事件绑定，不修改 dispatcher。

#### 决策 3：fire-and-forget 异步执行

LLM 检查耗时 2-5 秒。如果同步执行会阻塞 `append_event`，导致所有事件写入停滞。使用 `asyncio.create_task()` 分发，Executor 挂了只记录日志，不影响主流程。

代价：`RUN_COMPLETED` 事件到达前端时，Answer Accuracy 检查可能还在跑。前端需要处理"质量评分加载中"的中间态。

#### 决策 4：通用 Check 接口 + 两种实现

不搞万能框架。只有两种检查类型：

- `RuleQualityCheck`：纯 Python 判断，零成本，总是运行。适合结构完整性检查。
- `LLMQualityCheck`：调用 LLM 做语义判断，有 token 成本。可通过 `sample_rate` 参数控制采样比例。

同一个接口 `async def evaluate(state: RunState) -> QualityReport`，两种实现，`EvaluatorRunner` 不关心具体类型。

### 4.3 数据流

```
1. Scheduler 执行完 plan，写入 RUN_COMPLETED 事件
        │
2. EventStore.append_event() → 触发 on_append 回调
        │
        ├→ WebSocket 广播（前端收到 Run 完成通知）
        ├→ RunMonitor._on_event（无匹配规则，跳过）
        └→ EvaluatorRunner._on_event
              │
              │ event.event_type == RUN_COMPLETED ✓
              │
3. asyncio.create_task(_evaluate_run)
        │
4. fold_events(get_events(run_id)) → 获取完整 RunState
        │
5. 遍历 checks:
   ├→ StepCompletenessCheck.should_run(state) → True
   │    └→ evaluate(state) → QualityReport(verdict="pass", ...)
   │         └→ append_event(QUALITY_CHECK_COMPLETED, payload)
   │
   └→ AnswerAccuracyCheck.should_run(state) → True
        └→ evaluate(state)
             ├→ 构造 prompt(intent + tool_results + answer)
             ├→ llm_client.chat(prompt)
             ├→ _parse_response(LLM_json_output)
             └→ append_event(QUALITY_CHECK_COMPLETED, payload)
                    │
6. EventStore → on_append → WebSocket 广播
        │
7. 前端收到 QualityCheckCompleted 事件 → 渲染质量面板
```

### 4.4 事件模型

新增一个事件类型，两个 payload：

**EventType**：`QUALITY_CHECK_COMPLETED`

**QualityIssuePayload** — 单个问题的结构化描述：

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `str` | 问题分类：`unfulfilled` / `hallucination` / `inconsistency` / `missing_data` |
| `severity` | `str` | 严重程度：`error` / `warning` / `info` |
| `detail` | `str` | 人类可读的问题描述 |
| `source` | `str \| null` | 指向相关 step_id 或 tool_name |

**QualityCheckCompletedPayload** — 单次检查的完整结果：

| 字段 | 类型 | 说明 |
|------|------|------|
| `check_id` | `str` | 检查唯一标识，如 `"step_completeness"`、`"answer_accuracy"` |
| `target` | `str` | 被检查的对象：`"answer"` / `"step_results"` |
| `evaluator_type` | `str` | `"rule"` 或 `"llm"` |
| `verdict` | `str` | `"pass"` / `"warn"` / `"fail"` |
| `score` | `float \| null` | Rule 检查为 null，LLM 检查为 0-1 |
| `issues` | `list[QualityIssuePayload]` | 发现的问题列表 |
| `summary` | `str \| null` | 一句话总结（LLM 生成） |
| `duration_ms` | `int` | 检查耗时 |

**RunState 新增字段**：`quality_checks: list[QualityCheckCompletedPayload]` — 累积所有已完成的检查结果。

### 4.5 文件结构

```
harness/
├── evaluator/                    # 新增目录
│   ├── __init__.py               # 导出 EvaluatorRunner, QualityCheck, 内置 checks
│   ├── base.py                   # QualityReport dataclass, QualityCheck(ABC), RuleQualityCheck, LLMQualityCheck
│   ├── checks.py                 # 内置检查 + 各自的 prompt 常量
│   │   ├── StepCompletenessCheck (RuleQualityCheck)
│   │   └── AnswerAccuracyCheck  (LLMQualityCheck)
│   └── runner.py                 # EvaluatorRunner: attach + on_append dispatch + create_task
├── models/
│   └── events.py                 # +1 EventType, +2 payload models, +1 PAYLOAD_MODEL_MAP entry
├── core/
│   └── fold.py                   # +1 field on RunState, +1 fold case
├── api/
│   └── serve.py                  # +5 行：构造 EvaluatorRunner + attach
└── monitoring/
    └── run_monitor.py            # 不改
```

---

## 5. 影响评估

### 5.1 执行管道影响

**零影响。** Evaluator 完全在 Scheduler/Planner/Executor 的外部运行。检查结果不回流到执行管道。

### 5.2 Event Store 影响

**极小**：
- 新增一个事件类型和两个 payload 类
- 每个 Run 产生 2-3 个额外的 `QualityCheckCompleted` 事件（每个检查 1 个）
- Payload 体积：每个 ~1KB
- 索引：无新增索引（复用 `(run_id, seq)` 主键）

### 5.3 LLM 开销

| 检查 | 输入 tokens | 输出 tokens | 预估耗时 | 频率 |
|------|------------|------------|----------|------|
| Answer Accuracy | ~2,000 | ~200 | 2-3s | 每个有 tool 执行的 Run |
| Step Completeness | 0 | 0 | <1ms | 每个 ToolCompleted + Run 结束 |

以日均 100 Run、每个 Run 平均 5 个 tool 步骤计算：
- LLM token 消耗：100 × 2,200 ≈ 220K tokens/天（约 $0.10-0.50，取决于模型）
- Event Store 存储：100 × 3 个事件 × 1KB ≈ 300KB/天
- 内存：per-run state 在检查完成后立即释放，无持久占用

### 5.4 API 层影响

**仅 serve.py**。构造 EvaluatorRunner 时需要一个 `LLMClient` 实例（已有），一个 `EventStore` 实例（已有），和一组 `QualityCheck` 实例。不需要新的 API 端点，不需要修改 HarnessAPI 内部结构。

EvaluatorRunner 通过 `store.on_attach(callback)` 注册回调，与 WebSocket 广播、Monitor 使用相同的回调机制。`append_event` 的回调是 `try/except` 包裹的，单个 Evaluator 回调失败不会影响其他回调。

### 5.5 前端影响

**无需修改**。`QualityCheckCompleted` 事件通过已有 WebSocket 通道推送，与所有其他事件完全一致。前端收到后按 `event_type` 分支渲染即可。

`AnalysisService` 后续可以添加按 `check_id` / `verdict` 聚合的查询，但不在一期范围内。

### 5.6 测试影响

新增 `tests/test_evaluator.py`：
- 单元测试：Rule 检查逻辑、LLM 检查的 prompt 组装和 response 解析
- 集成测试：EvaluatorRunner 的事件分发和 `create_task` 调度
- 使用已有 `MockLLMClient` 和 `store` fixture 测试完整路径

**已有测试不受影响**：Evaluator 不修改任何已有组件，不引入新的 import 依赖到 Scheduler/Planner/Executor。

### 5.7 风险

| 风险 | 缓解措施 |
|------|----------|
| LLM 检查超时（>30s） | `LLMQualityCheck` 内部设置 `asyncio.wait_for(timeout=30)`，超时返回 `verdict="warn"` + 超时说明 |
| LLM 响应解析失败 | `_parse_response` 失败时返回 `verdict="warn"` + `issues=[{"type":"check_failed", "detail": raw_error}]` |
| Evaluator 回调抛异常 | `_on_event` 和 `_trigger_checks` 用 `try/except Exception` 包裹，异常只记录日志 |
| 并发 Run 的 Evaluator 资源竞争 | 每个 Run 的检查是独立的 `create_task`，互不阻塞。LLM client 的 rate limit 由 `OpenAILLMClient` 现有逻辑处理 |
| QualityCheckCompleted 事件到达晚于 RUN_COMPLETED | 前端需处理中间态。Event Store 保证 seq 单调递增，前端按 seq 排序即可自然处理 |

---

## 6. 组件接口契约

### 6.1 QualityCheck（抽象基类）

| 方法/属性 | 类型 | 说明 |
|-----------|------|------|
| `check_id: str` | 类属性 | 唯一标识，如 `"answer_accuracy"` |
| `trigger_events: list[EventType]` | 类属性 | 此检查监听的事件类型列表 |
| `should_run(state: RunState) → bool` | 方法 | 决定是否对此 Run 执行检查。默认：`bool(state.tool_results)` |
| `evaluate(state: RunState) → QualityReport` | 异步方法 | 执行检查，返回结构化报告 |

### 6.2 EvaluatorRunner

| 方法 | 说明 |
|------|------|
| `__init__(checks, store)` | 接收 QualityCheck 列表和 EventStore 引用 |
| `attach()` | 在 EventStore 注册 `on_append` 回调 |
| `_on_event(event)` | 收到事件后匹配 trigger_events，通过 `create_task` 异步执行 |
| `_evaluate_checks(run_id, matching_checks)` | fold 事件流 → 遍历 checks → 写 QualityCheckCompleted 事件 |

### 6.3 LLMQualityCheck（RuleQualityCheck 的子类相反——它是 QualityCheck 的子类）

LLM 检查额外需要以下构造参数：

| 参数 | 说明 |
|------|------|
| `llm_client: LLMClient` | LLM 调用客户端 |
| `prompt_template: str` | 包含 `{intent}`、`{tool_results_summary}`、`{answer}` 等占位符的模板 |
| `output_schema: dict` | 期望 LLM 输出的 JSON Schema（用于 structured generation） |
| `sample_rate: float` | 采样比例，默认 1.0。0.2 表示 20% 的 Run 执行此检查 |

---

## 7. 同类系统参考

| 参考 | 借鉴点 | 差异 |
|------|--------|------|
| **LangSmith Evaluators** | 独立的评估组件，不修改执行流程 | JAgent 不依赖外部平台，评估结果内置于事件流 |
| **RAGAS (RAG 评估框架)** | Answer Relevancy / Faithfulness 维度的 prompt 设计 | RAGAS 面向检索增强生成，JAgent 面向工具执行 |
| **Stripe API Idempotency** | 幂等事件写入模式 | 相同——每个 QualityCheckCompleted 事件有唯一的 `(run_id, check_id)` 组合 |
| **Prometheus AlertManager** | 规则引擎 + 评估分离 | AlertManager 告警触发行动，Evaluator 只记录不行动 |

---

## 8. 里程碑

| 阶段 | 交付 | 验收标准 |
|------|------|----------|
| **设计审查** | 本文档 | 团队确认架构方向、检查维度、集成方式 |
| **MVP 开发** | StepCompletenessCheck + AnswerAccuracyCheck + EvaluatorRunner + 事件模型 + 测试 | 2 项检查通过测试，已有 355 项测试全通过 |
| **集成上线** | serve.py wiring + 前端质量面板 | 真实 Run 后 UI 可展示检查结果 |
| **运营观察** | 收集 1-2 周生产数据 | 分析 Pass/Warn/Fail 分布，决定后续检查的优先级 |

---

*本文档遵循 `AGENTS.md` 第 10 条：不含示例代码，仅描述产品范围、架构设计和接口契约。*
*角色：Agent 导师 · 架构守护者 · 最佳实践布道者*
