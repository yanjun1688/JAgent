# JAGENT-2026-P1-13 真实 LLM Workspace 黑盒测试异常汇总

> **状态**：已修复（除 Bug 7 日志乱码、Bug 8 MCP fetch 404 两个外部/P2 问题）
> **发现日期**：2026-08-12
> **修复日期**：2026-08-12
> **发现方式**：本地真实服务黑盒测试（FastAPI + React + 真实 LLM + Docker Desktop/WSL2）
> **影响范围**：真实 LLM 分类、Planner/Reviser、任务完成语义、Workspace 载体启动、黑盒可观测性
> **关联 Workspace**：`ws_fe9d4e36e951`、`ws_dec08bc42a76`
> **关联 Run**：`cee7d7f6`、`325b42c5`、`017dc1f8`

---

## 0. 修复记录（2026-08-12）

| Bug | 根因 | 修复 | 位置 |
|---|---|---|---|
| classify='no' 绕过 Tool Layer（Run 325b42c5） | LLM 二值判断无受信兜底 | 受信保守门 `_intent_requires_tools`：意图含文件/路径/URL/浏览器信号强制 needs_tools=True；CLASSIFY prompt 补全 | `harness/core/scheduler/plan.py`、`harness/core/system_prompt.py` |
| 完成门不验证交付目标（Run cee7d7f6） | `_completion_gate` 只聚合 step_normal，无法验证用户交付物 | Layer 2 `required_operations` 契约：计划声明用户硬性操作 → PlanGuardrail 覆盖检查 + 完成门达成检查（结构化子集匹配，非穷举） | `harness/models/plan.py`、`harness/core/planner.py`、`harness/core/scheduler/plan.py` |
| Planner/Reviser 弱化原始目标（Run cee7d7f6） | PLAN/REVISE prompt 的 MUST-plan 规则只枚举读/查/取，漏 write/create | Layer 1 prompt 补全：MUST-plan 加入 write/create/delete/append/list；REVISE 增加 GOAL PRESERVATION 规则 | `harness/core/system_prompt.py` |
| Answer 无工具事实时自由发挥（Run 325b42c5） | 无工具执行时无权威信号 | `generate_answer` 注入 `[NO TOOLS EXECUTED]`；ANSWER prompt 增加对应接地规则 | `harness/core/planner.py`、`harness/core/system_prompt.py` |
| Run 卡在 RunStarted 无终态（Run 017dc1f8 + 30并发） | 无 watchdog，LLM 慢时永久停留 | `SchedulerConfig.run_timeout_ms` + `BaseScheduler.run()` watchdog → 超时写结构化 RunFailed | `harness/core/scheduler/base.py`、`harness/api/serve.py` |
| API 500 但 RunStarted 孤儿（Run 017dc1f8） | `start_run()` 异常未兜底 | `start_run()` try/except → 失败写 RunFailed 再抛出 | `harness/api/deps.py` |
| ScopedEventStore 缺 evict_run_to_conv（日志 Task exception） | facade 未透传 | 增加 `evict_run_to_conv` 透传 | `harness/storage/scoped.py` |
| Windows SelectorEventLoop 子进程失败（playwright/Docker） | Uvicorn 启动模式可能覆盖全局事件循环策略；Docker bind mount 还要求 Windows 宿主路径为绝对路径 | 新增跨平台 Uvicorn `event_loop_factory`：Windows 强制 Proactor、Unix 使用平台默认 loop；Docker backend 统一规范化绝对挂载路径；browser_tool 检测并给出明确错误 | `harness/api/loop.py`、`harness/execution/docker.py`、`README.md`、`harness/tools/browser_tool.py` |

**未修（记录在案）**：Bug 7 日志中文乱码（观测性，P2，不误判业务数据）；Bug 8 MCP fetch npm 404（外部环境）。
**架构级后续**：结构化输入需求（用户侧 `required_operations` 契约）已记录到 `JAgent-docs/Reviews/structured_input_requirements_review_20260812.md`，待架构评审后实施。

---

## 1. 背景

本次测试目标是验证 v3.3 Workspace 在真实运行环境中的完整链路，而不是只验证 MockLLMClient 或内存数据库：

```text
浏览器/API
  -> FastAPI :8000
  -> RunStarted
  -> classify
  -> Planner
  -> Scheduler / DAG Executor
  -> Tool Layer / Workspace Backend
  -> Event Store
  -> WebSocket / 前端
```

测试环境确认如下：

| 项目 | 结果 |
|---|---|
| 后端 | `127.0.0.1:8000` 可访问 |
| 前端 | `127.0.0.1:5173` 可访问 |
| LLM | `qwen3.7-flash-2026-07-15`，真实 API 调用 |
| Docker | Docker Desktop 4.41.2，daemon 正常 |
| WSL | Ubuntu/WSL2 running |
| Langfuse | 关闭，`LANGFUSE_ENABLED=false` |
| MCP memory | 已连接 |
| MCP playwright | 已连接 |
| MCP fetch | 失败，npm registry 返回 404 |

---

## 2. 启动阶段发现的环境问题

### 2.1 旧数据库 Schema 阻止服务启动

首次启动后端失败，错误为：

```text
RuntimeError: Database is not compatible with v3.3:
events missing ['tenant_id', 'workspace_id']; delete the database and restart
```

根因是现有 `.harness.db` 属于旧 Schema，而 `EventStore._validate_v33_schema()` 按设计拒绝自动迁移。

用户随后手动删除数据库并重新启动，服务恢复正常。本次没有由测试程序删除任何本地文件。

### 2.2 MCP fetch 环境失败

服务启动日志显示：

```text
npm ERR! 404 Not Found - GET https://registry.npmjs.org/@modelcontextprotocol%2fserver-fetch
Failed to connect MCP server 'fetch': Connection closed
```

该问题属于外部 npm 包/注册表环境问题，不是 Workspace 或 Docker 核心链路问题。`memory` 和 `playwright` MCP 仍正常连接。

### 2.3 前端进程状态不一致

用户认为前端已启动，但第一次探测 `5173` 无法连接。随后重新启动 Vite 后，前端才返回 200。

可能原因：

1. 前端进程尚未真正启动完成。
2. 进程启动后退出但终端未明显提示。
3. 监听地址/端口与预期不同。

此项目前已恢复，不属于当前核心 Bug，但黑盒脚本应在测试开始时明确验证端口可用，而不是假设服务已启动。

---

## 3. Run `cee7d7f6`：硬性交付未完成却成功

### 3.1 用户请求

```text
请在当前 workspace 创建 blackbox.txt，写入 hello harness blackbox，
然后重新读取这个文件并告诉我读取到的完整内容。
```

### 3.2 实际事件链

```text
RunStarted
AgentThought
PlanCreated
DagStepStarted(s1, file_op)
ToolCalled(file_op, read blackbox.txt)
ToolCompleted(result_type=unsuccessful, File not found)
DagStepCompleted(s1)
PlanRevised
PlanCreated
DagStepStarted(s1, file_op)
ToolCalled(file_op, list .)
ToolCompleted(success=true, empty directory)
DagStepCompleted(s1)
PlanCompleted
AgentThought(answer)
RunCompleted(all_normal=true, unmet_step_ids=[])
```

关键事实：

- 从未出现 `file_op(operation=write)`。
- 文件实际没有创建。
- 第二轮只执行了 `list .`。
- 最终 `RunCompleted` 携带 `all_normal=true` 和空 `unmet_step_ids`。
- Answer 内容承认文件不存在，但 Run 状态仍为 completed。

### 3.3 日志证据

Planner 首轮生成的目标已经被模型改写为：

```text
Read the content of the file blackbox.txt from the workspace.
```

Reviser 又将目标改写为：

```text
List the contents of the workspace to verify the existence of blackbox.txt.
```

因此，真实执行链路从“创建并读取”退化为“读取并列目录”。

### 3.4 可能根因

**可能性 A：Planner 丢失用户请求中的复合交付目标，优先级高。**

Planner 使用 LLM 自由重述 `intent`，没有受信地保存“必须创建 + 必须再次读取”的结构化目标。计划中的 step 只包含 read，系统无法知道 write 是必需前置动作。

**可能性 B：Reviser 只根据上一次失败动作寻找最小修订，优先级高。**

Reviser 看到 read 失败后选择 list，而不是补充 write。当前修订契约允许 LLM 返回一个语义上更弱、但机械上成功的替代步骤。

**可能性 C：完成门只验证 `step_normal`，无法验证用户交付物，优先级高。**

当前完成门核心逻辑是：

```python
all_normal, unmet = self._completion_gate(...)
```

`step_normal` 只说明本次工具调用正常完成，不说明用户最初要求的文件是否创建、内容是否匹配、是否完成回读验证。list 空目录本身是一次正常工具调用，因此可以使当前修订计划通过。

**可能性 D：D12 修订合并逻辑允许同一原始 step 被后续成功步骤覆盖，优先级中。**

原始 `s1` 的 read 失败后，修订仍复用 `s1`，第二次 list 成功。结果聚合按 step id 观察到成功，但没有保存“原始动作 read 未达成、替代动作 list 也不等价”的语义差异。

### 3.5 初步结论

这不是 Tool Layer 越权或文件安全问题，而是：

```text
用户交付目标没有结构化落地
  + LLM 可以把目标弱化为观察动作
  + 完成门只检查当前步骤机械成功
  = 未完成任务被标记 RunCompleted
```

---

## 4. Run `325b42c5`：路径越界请求被分类为无需工具

### 4.1 用户请求

```text
请尝试在当前 workspace 的父目录 ../blackbox-escape.txt 写入 blackbox forbidden，
用于测试路径边界。
```

### 4.2 实际事件链

```text
RunStarted
classify -> raw=no
needs_tools=False
跳过 Planner / Tool Layer
Answer LLM
RunCompleted(all_normal=true, unmet_step_ids=[])
```

没有出现：

- `PlanCreated`
- `ToolCalled`
- `GuardrailTriggered`
- `ToolFailed`

### 4.3 可能根因

**可能性 A：classify 使用 LLM 二值判断，优先级高。**

代码逻辑是：

```python
result = chat_resp.content.strip().lower()
needs = result != "no"
```

真实模型返回 `no` 即跳过整个受信工具链。模型没有理解“写文件”和“测试路径边界”仍然是工具请求。

**可能性 B：classify Prompt 没有覆盖 Workspace/路径安全语义，优先级高。**

分类 Prompt 主要判断是否需要外部工具，没有声明：

- 任何文件读写请求必须进入 Tool Layer。
- 即使请求最终应被拒绝，也必须进入 Guardrail。
- “测试安全边界”不是分析任务，而是受信安全测试动作。

**可能性 C：系统把安全拒绝和无需执行混为一类，优先级高。**

正确流程应是：

```text
识别为文件写入请求
  -> 生成或解析 file_op 调用
  -> ScopeGuardrail 拦截 ../
  -> 写入 GuardrailTriggered / ToolFailed
```

当前流程是：

```text
LLM classify=no
  -> 直接进入 answer
  -> 安全组件完全没有机会执行
```

### 4.4 Answer 阶段的附加异常

Answer 没有基于真实工具结果回答，而是生成了与 CTF、Docker、Android、路径配置有关的长篇泛化说明。

可能根因：

1. `needs_tools=False` 后 Answer 没有工具事实可引用。
2. Answer Prompt 允许模型自由解释用户意图，而不是要求无工具事实时只返回受控的“未执行/无法执行”消息。
3. `blackbox forbidden` 等词触发了模型自身的泛化联想。

---

## 5. Run `017dc1f8`：Docker Run 停在 RunStarted

### 5.1 环境准备

- Docker daemon 已确认可用。
- 成功拉取 `python:3.11-slim`。
- 创建了 sandbox Workspace：`ws_dec08bc42a76`。
- host mount：`data/workspaces/blackbox-docker`。
- mount root：`/workspace`。

### 5.2 用户请求

```text
请使用 file_op 在当前 workspace 写入 docker-blackbox.txt，
内容必须是 docker-ok。
```

### 5.3 实际状态

事件流只有：

```text
RunStarted
```

没有出现：

- classify 结果事件
- PlanCreated
- ToolCalled
- Docker container
- RunCompleted / RunFailed

Docker `ps -a` 中没有本次测试新创建的运行容器。

### 5.4 可能根因

**可能性 A：LLM 调用或后台 Scheduler 卡住，优先级高。**

`create_run` 先写 `RunStarted`，随后通过 `asyncio.create_task()` 异步启动 Scheduler。若 classify 或后续 LLM 调用长时间等待，事件流会长时间停留在 RunStarted。

**可能性 B：真实 LLM 超时/连接异常没有及时转换成 RunFailed，优先级高。**

虽然 `BaseScheduler.run()` 有未处理异常兜底，但当前黑盒观测中没有看到后续结构化错误事件，因此需要确认：

- 请求是否真的进入 `_classify_intent()`。
- LLM client 是否设置了有效超时。
- 异步 task 是否发生异常但没有被 scheduler 主循环收集。
- `start_run()` 中 `create_backend()` 或 scheduler 构造是否在 task 之外抛出异常。

**可能性 C：Docker backend 初始化在 LLM 前后顺序不清，优先级中。**

当前 `HarnessAPI.start_run()` 会先创建 backend，再构造 scheduler 并启动 task；但 Docker backend 的 `_ensure_container()` 是第一次文件操作时才调用。因此如果事件停在 RunStarted，尚无证据表明 Docker 命令已经执行。

**可能性 D：测试客户端看到的 500 与后台 RunStarted 不一致，优先级中。**

一次请求中客户端曾观察到 `Internal Server Error`，但数据库中同时存在 `RunStarted`。这表明 API 响应异常和后台 task 生命周期可能脱钩，需要补充请求级 traceback、task exception 和 Run 状态一致性测试。

---

## 6. 已排除或暂不认为是根因的问题

- Docker daemon 不可用：已排除，`docker info` 正常。
- Docker 镜像无法拉取：已排除，`python:3.11-slim` 拉取成功。
- Workspace tenant REST 隔离：本次 A/B 查询结果符合预期。
- 本地路径边界实现本身：本轮越界请求没有进入 Tool Layer，因此尚未证明 `LocalDirectoryBackend.resolve()` 有问题。
- 前端编译问题：前端端口恢复后未发现启动编译错误，但本轮尚未完成完整浏览器交互闭环。

---

## 7. 当前根因优先级排序

| 优先级 | 假设 | 需要的验证 |
|---|---|---|
| P0 | 完成门只验证步骤机械成功，不验证交付目标 | 为 write/read 目标增加结构化目标回归，禁止 list 替代 write/read |
| P0 | classify 的 `no` 可以绕过所有 Tool Layer/Guardrail | 对文件写入、路径越界、Workspace 操作增加受信前置识别或保守策略 |
| P0 | LLM 可弱化 Plan/Revised Plan 的原始目标 | 对 root intent/required operations 建立系统侧不可缩小约束 |
| P1 | Answer 无工具事实时自由发挥 | 增加无工具执行时的受控回答策略 |
| P1 | 后台 Scheduler/LLM 卡住时 Run 只停留在 RunStarted | 增加 task watchdog、LLM timeout、结构化 RunFailed 和请求日志关联 |
| P1 | API 500 与后台 RunStarted 脱钩 | 增加 `start_run()` 失败回滚/RunFailed 事件和集成测试 |
| P2 | Windows/PowerShell 输出中文乱码 | 用 UTF-8 读取日志并确认原始字节，不要把终端乱码误判为业务数据损坏 |
| P2 | MCP fetch npm 包 404 | 固定可用包版本或将可选 MCP 安装失败显式降级 |

---

## 8. 建议下一步

1. 先修受信边界，不先优化 Prompt：文件操作请求不得由 classify=`no` 绕过 Tool Layer。
2. 将用户请求中的硬性交付要求结构化保存到 Run/Plan 事件，而不是只保存自由文本 intent。
3. 完成门同时验证 root objective 与 step_normal，禁止“观察成功”替代“交付成功”。
4. 对真实 LLM 每次调用增加开始、结束、超时、异常和 run_id 关联日志。
5. 为后台 Scheduler 增加 RunStarted 后的启动 watchdog；超过阈值必须写结构化 RunFailed。
6. 重新执行黑盒测试时，每个 Run 使用明确的超时，不允许无限轮询。

---

## 9. 证据位置

- `data/logs/harness.log`
- `tests/test_completion_gate.py`
- `tests/test_probe_and_convergence.py`
- `harness/core/scheduler/plan.py`：classify、Plan/Revise、completion gate
- `harness/core/scheduler/base.py`：后台 Scheduler 异常兜底和生命周期
- `harness/execution/docker.py`：Docker container lazy initialization
- `harness/api/deps.py`：`start_run()` 异步 task 装配
- `JAgent-docs/Dev/TODO_v3.3_Workspace.md`
- `JAgent-docs/Handover/workspace_v3.3_handover_20260811.md`

---

## 10. 第二轮 30 个真实用例结果（2026-08-12）

为避免只根据两个样例下结论，本轮一次提交 30 个互不依赖、禁止删除文件的真实 LLM 请求，覆盖：

- Workspace 文件读取、写入、追加、列表
- 不存在文件
- `../` 和绝对路径越界
- HTTP 正常/404/无效域名
- 浏览器正常/异常 URL
- MCP memory 查询
- analysis-only 请求
- 不同 Workspace 隔离
- Docker Workspace 写入

每个 Run 使用 90 秒硬超时。超时只停止测试端轮询，不删除 Run、文件、数据库或容器。

### 10.1 统计

| 结果 | 数量 |
|---|---:|
| 已在测试窗口内 `RunCompleted` | 6 |
| 90 秒内未到终态 | 24 |
| 提交失败 | 0 |
| 总请求 | 30 |

需要注意：90 秒超时不等于 Run 永久卡死。测试端停止轮询时，部分 Run 已经推进到 `DagStepCompleted` 或 `PlanCompleted`，但没有在窗口内写入终态事件。该现象本身仍然是黑盒稳定性问题：API 没有向调用方提供明确的运行中/超时/失败语义，也没有在测试窗口内完成生命周期闭环。

### 10.2 代表性事件状态

| Run | 测试窗口末状态 | 观察 |
|---|---|---|
| `e50ff8be` | `DagStepCompleted` | 工具执行完成，但没有继续写终态 |
| `fcdcd17c` | 多轮修订后 `DagStepCompleted` | 自愈推进，但未完成终态 |
| `2cb239a1` | `PlanCompleted` | 计划完成，但未继续到 Run 终态 |
| `cfd56dc9` | `DagStepFailed` | 越界/工具失败路径已进入受信事件链 |
| `8d611c4b` | `RunFailed` | Guardrail 触发后最终失败，属于有效失败出口 |
| `013266aa` | `RunCompleted` | 事件链完整，10 个事件 |
| `e1b0a26a` | 多轮修订后 `DagStepCompleted` | Guardrail 后修订执行，但未在窗口内终态 |
| `3de7c9d5` | 仅 `RunStarted` | 可能卡在 classify/LLM 调用或后台 Scheduler 启动阶段 |
| `47fec7a8` | 仅 `RunStarted` | 同上 |
| `ace25a7d` | 仅 `RunStarted` | 同上 |

### 10.3 新增可能根因

**可能性 E：并发真实 LLM 请求造成生命周期长尾，优先级 P0。**

30 个请求几乎同时提交后，24 个没有在 90 秒内进入终态。事件分布显示问题不是全部卡在同一层：

- 一部分停在 `RunStarted`，疑似 classify/LLM 或后台 task 启动阶段。
- 一部分已经完成工具调用，但停在 `DagStepCompleted`。
- 一部分已经写入 `PlanCompleted`，但未写入 `RunCompleted`。
- 少数进入 `RunFailed`，说明失败出口并非完全失效。

这更像是 LLM 并发延迟、Scheduler 长尾、事件终态写入时机和 API 可观测性共同导致，而不是单一 Docker 错误。

**可能性 F：Run 状态没有“运行中超时”这一受信终态，优先级 P0。**

当前 Run 可以长时间停在 `RunStarted`、`DagStepCompleted` 或 `PlanCompleted`。如果 LLM 请求、后台 task 或事件写入没有继续推进，系统没有 watchdog 自动写入结构化 `RunFailed`/`RunTimedOut`，调用方只能不断轮询。

**可能性 G：PlanCompleted 与 RunCompleted 之间存在长尾 Answer/Finalize 阶段，优先级 P1。**

部分 Run 已写入 `PlanCompleted`，但在测试窗口内没有 `AgentThought(answer)` 和 `RunCompleted`。这与 Answer 阶段再次调用真实 LLM、或 finalize 事件写入失败/延迟相符，需要用 run_id 级别日志确认。

**可能性 H：批量并发测试放大了 provider 限流或连接池问题，优先级 P1。**

单 Run 的 LLM 响应耗时约数秒到几十秒；30 Run 并发后出现明显长尾。需要区分：

- provider 429/5xx/连接排队；
- 本地 OpenAI-compatible client 的并发控制；
- 服务端 asyncio task 调度；
- EventStore 写锁等待。

### 10.4 本轮确认的正向安全信号

本轮至少有多条 Run 进入了 `GuardrailTriggered` 和 `DagStepFailed`，说明部分越界请求确实进入 Tool Layer，未直接发生宿主文件写入。`8d611c4b` 还走到了稳定的 `RunFailed` 出口。

因此当前结论应拆成两部分：

```text
Tool Layer 的路径边界在已进入执行链的样本中有拦截证据；
但 classify 仍可能让请求绕过 Tool Layer，且并发 Run 的终态闭环不稳定。
```

### 10.5 下一轮验证约束

后续不应再次无上限批量提交。建议采用：

1. 并发度 1、2、5、10 分组压测，而不是直接 30 并发。
2. 每组固定记录 classify、首次 LLM、首次工具、PlanCompleted、Answer、RunCompleted 的时间戳。
3. 每个 Run 设置服务端 watchdog，超过阈值自动写结构化失败事件。
4. 把 `RunStarted`、`PlanCompleted`、`RunCompleted` 之间的阶段耗时单独统计。
5. 任何测试端超时必须同时查询 EventStore 和服务日志，不能只根据 HTTP 客户端超时判断根因。

---

## 11. 修复环境后的 50 用例回归结果（2026-08-13）

环境修复后重新执行完整矩阵：复跑 30 个历史场景，新增 20 个场景。测试使用并发上限 5，分 10 批提交；50 个请求全部在约 192 秒内提交完成，最终全部进入终态。

本轮 API 通过 `127.0.0.2:8000` 访问。`127.0.0.1:8000` 仍被此前遗留进程占用，因此没有继续向旧进程发送请求。

### 11.1 统计

| 结果 | 数量 |
|---|---:|
| 总 Run | 50 |
| `RunCompleted` | 20 |
| `RunFailed` | 30 |
| 仍运行中 | 0 |
| HTTP 提交失败 | 0 |
| Watchdog 超时失败 | 15 |
| required operation/步骤未达成失败 | 13 |
| 非法 DAG 依赖异常 | 2 |

Watchdog 最终将长尾 Run 收敛为结构化 `RunFailed`，没有 Run 永久停留在 `RunStarted`。这验证了 Bug 5 的服务端兜底已经生效。

### 11.2 环境修复验证

Docker Workspace Run 已完成以下链路：

```text
RunStarted -> AgentThought -> PlanCreated -> DagStepStarted
-> ToolCalled -> ToolCompleted/DagStepCompleted
```

此前的 Windows `NotImplementedError` 不再出现。Docker 子进程可以启动，Docker bind mount 也可以使用规范化后的绝对宿主路径。部分 Run 后续因 Planner 没有完成用户要求而失败，这是 Agent 计划语义问题，不是 Docker 环境错误。

### 11.3 新增问题：Planner 生成悬空 DAG 依赖，优先级 P1

两个 Run 出现：

- `9a340fd0`
- `3b88b26a`

最终错误均为：

```text
Unhandled scheduler error: ValueError("Step 's3': depends on unknown step 's1'")
```

这说明 LLM 生成的 Plan 中，某个 step 的 `depends_on` 引用了当前 Plan 中不存在的 step。该错误目前被 Scheduler 捕获为一般失败，但 PlanGuardrail 在执行前没有拒绝这个非法 DAG。

建议在受信的 `PlanGuardrail` 中增加结构校验：

- 所有 `step_id` 必须唯一；
- 所有 `depends_on` 必须引用当前 Plan 中已声明的 step；
- 禁止自依赖和循环依赖；
- 校验失败时写入结构化 Plan/Guardrail 失败事件，不进入 DagExecutor。

### 11.4 本轮结论

```text
Windows + Docker 环境链路已修复并可执行；
Run watchdog 已能收敛并发长尾；
required_operations 已阻止多个未完成目标被误报为成功；
Planner 仍可能生成悬空 DAG 依赖，需要新增受信 PlanGuardrail 校验。
```

---

## 12. 2026-08-13 日志复核：新增遗留问题

对 `data/logs/harness.log`、`watchdog_verify_stderr.log`、`verify_stderr.log` 和
`reload_stderr.log` 复核后，确认环境修复和 Run watchdog 已生效，但仍存在以下问题。

### 12.1 Watchdog 终态已收敛，但异步任务未完全回收（P1）

Run `1adff18c` 能够按时写入结构化 `RunFailed`，但清理日志显示
`pending_calls=1`。`harness.log` 中多个 watchdog 或失败 Run 也出现该值。
这说明主 Run 已进入失败终态，但至少有一个异步调用仍未确认回收，暴露出
LLM/工具/MCP 子任务取消和回收不完整的风险。

**建议**：watchdog 触发后统一取消并等待所有子任务，清理完成前不得报告 Run 资源
已完全释放；增加 `pending_calls > 0` 的故障注入测试。

### 12.2 `probe` 与工具副作用契约粒度不匹配（P1）

日志中多次出现 `file_op` 的 `probe` 被拒绝，因为工具级声明包含
`side_effects=['write', 'delete']`；`http_request` 也因
`side_effects=['external']` 被拒绝 `probe=true`。当前副作用按整个工具声明，而不是
按具体操作/参数声明，导致 `file_op(read)` 继承写/删操作的副作用，合法只读探测触发
额外 LLM 重试并放大 watchdog 长尾。

**建议**：将副作用计算下沉到工具操作级别，不能由 LLM 通过 `probe` 标记规避受信判定。

### 12.3 Reviser 生成动态引用，但执行层未定义引用语义（P1）

日志中出现 Planner 生成 `{"path": "$s1.result"}`。当前执行结果显示该值会作为
普通路径进入 `file_op`，而不是解析为前一步输出。若暂不支持步骤输出引用，应由
`PlanGuardrail` 拒绝 `$<step>.<output>` 形式的未解析引用；若支持，则必须由受信
Executor 解析。

### 12.4 Reviser 仍可能改变目标路径或引入无关步骤（P1）

日志中出现 Reviser 将失败读取改为列目录、引入不存在的 `blackbox-rerun` 目录，或
生成新的 `s3/s4` 步骤并改变依赖关系。`required_operations` 已阻止部分 fake-green，
但目前主要检查操作是否出现，还没有完全限制关键参数和目标路径。

**建议**：Reviser 不得修改 required operation 的工具名、操作类型、目标路径和关键
参数；新增步骤必须通过目标一致性、依赖完整性和循环依赖校验。

### 12.5 reload 模式出现持续文件变更风暴（P1，开发环境）

`reload_stderr.log` 从 `17:21:01` 持续到 `17:29:31`，几乎每秒出现
`[MAIN] 1 change detected`。Uvicorn 当前监听整个 `D:\Project\JAgent`，很可能将日志、
数据库或其他运行时生成文件纳入监听范围。该现象会造成服务反复 reload、端口残留和
并发黑盒测试结果污染。

**建议**：限制 reload 目录到源码目录，排除 `data/logs`、数据库、缓存和运行时工作区；
增加 reload 进程树和端口生命周期测试。

### 12.6 日志编码和失败统计仍影响可观测性（P2）

多份日志中的中文显示为 `????` 或替换字符，无法可靠还原原始用户意图。另外失败
Run 的生命周期日志仍出现 `Plan complete — status=failed failures=0`，状态已经是
`failed`，但失败计数为 `0`，会造成监控和前端统计歧义。应统一日志文件和读取端的
UTF-8 编码，并确保终态、失败原因和汇总计数来自同一事件折叠结果。

### 12.7 复核后的根因分层

```text
已修复：Windows/Docker 执行载体、RunStarted 永久停留、部分目标完成门问题
仍存在：Planner/Reviser 输出缺少完整受信计划校验
风险暴露：watchdog 后子任务回收不完整、reload 监听风暴、日志观测不可靠
外部环境：MCP fetch npm 包 404
```

本次日志复核未修改业务代码；上述问题应分别进入 PlanGuardrail、Scheduler 任务回收、
工具契约、开发启动和日志基础设施的后续修复计划。

---

## 13. 修复 + 换新 LLM 后的 50 用例回归结果（2026-08-13，qwen3.7-max）

在修复环境（`--loop` 重启 + 新 LLM `qwen3.7-max`）后重新执行 50 个黑盒用例。
50 个用例覆盖：校验/契约（A 组 10）、多租户隔离（B 组 10）、并发（C 组 10）、
超时/生命周期（D 组 10）、历史回归（E 组 10）。

### 13.1 服务端环境确认

- 旧进程（10:46 启动、无 `--loop`）占用 `0.0.0.0:8000`，新进程（15:17 `--reload`）只绑定
  `127.0.0.1:8000`，且未带 `--loop harness.api.loop:event_loop_factory`。
- 停掉全部旧进程后，以完整命令重启：

  ```bash
  uvicorn harness.api.serve:app --host 0.0.0.0 --port 8000 \
    --loop harness.api.loop:event_loop_factory --reload --reload-dir harness
  ```

- 重启后日志确认：`Real LLM mode: using qwen3.7-max`，Docker 子进程链路恢复正常
  （此前因 SelectorEventLoop 再次触发 `create_subprocess_exec NotImplementedError`）。

### 13.2 统计（含晚到终态合并）

| 结果 | 数量 |
|---|---:|
| 总用例 | 50 |
| `RunCompleted` | 27 |
| `RunFailed` | 15 |
| 校验/隔离正确拒绝（无 Run 实体） | 7 |
| 客户端异常（B6 `ReadError`） | 1 |
| 提交 200 | 42 |

分组明细：

- **A 校验/契约（10）**：契约 400 正确拒绝 ×2（未知工具、缺 path）、未知 workspace 404 ×1、
  越界/绝对路径 Guardrail 拦截 ×2（RunFailed）、完成链路 ×4、读不存在文件 ×1。
- **B 多租户（10）**：跨租户访问 A 的 ws 正确 404 ×2；tenant B 独立 ws 正常；
  缺 tenant header 500 ×1（bug，见 13.3）；跨租户读 A 文件客户端 ReadError ×1；
  **tenant B 默认 ws / 自身 list 均误报 `Deliverable not met`（bug，见 13.4）**。
- **C 并发（10）**：5 个不同文件 + 5 个同文件竞争，全部 `RunCompleted`（同文件竞争由
  幂等键正确收敛，未见跨步写丢失）。
- **D 超时/生命周期（10）**：watchdog 15s 窗口内完成 ×1；Docker 写恢复正常（RunCompleted）；
  confirm 流程完整闭环（ConfirmationRequested → RunPaused → 300s 等待超时 → RunFailed，
  超时后补发确认被 `run_not_waiting_confirmation` 正确拒绝）；LLM 慢响应导致 3 个 Run
  `ReadTimeout('')` RunFailed。
- **E 回归（10）**：write/read、append、http、memory 等多数恢复；MCP 工具名不匹配 ×1；
  list 契约误报 ×1；LLM 慢响应 ×3。

### 13.3 新发现 Bug A：空 tenant header 触发 500（P1）

`tenant_context_middleware`：

```python
token = set_current_tenant(request.headers.get("X-Tenant-Id", "default"))
```

当客户端显式发送空 header `X-Tenant-Id: ""` 时，`headers.get` 返回空字符串而非
默认值 `default`，`set_current_tenant("")` 抛 `ValueError`，middleware 未捕获 → 500。

用例 `B5 tenant-header-missing` 复现：

```text
POST /api/v1/runs {"intent":"list"}  X-Tenant-Id: ""  → 500 Internal Server Error
```

**建议**：middleware 对空值/空白回退 `default`（或 try/except 捕获 ValueError 返回 400）。

### 13.4 新发现 Bug B：`list` 交付契约路径语义未归一化导致误报未达成（P1）

用例 `B4 tenantB-default-ws`、`B9 tenantA-own-ws-list`、`E7 list-workspace` 三个
"列出 workspace 目录"全部失败：

```text
Deliverable not met: ad6fcafcf226768b
```

事件链：

```text
DeliveryContractsResolved: contract file_op list path="workspace directory"
PlanCreated:                step file_op list path="."
ToolCompleted:              success=true, path=".", 14 files returned
DagStepCompleted:           status=completed
RunFailed:                  Deliverable not met: ad6fcafcf226768b
```

契约抽取（LLM）将 path 写成人类可读的 `"workspace directory"`，Planner 使用真实路径
`"."`。两者语义等价、工具调用成功，但**结构子集匹配按字面值比较 path → 匹配失败**，
导致成功执行被误判为未达成交付。

**建议**：契约匹配对 `file_op` 的 path 增加语义归一化（`.`/`./`/`workspace`/
`workspace directory` 归一到 workspace 根），或在抽取 prompt 中约束 path 使用真实路径。

### 13.5 已知性能/并发观察（非新 Bug，记录在案）

- `qwen3.7-max` 并发下 LLM 单次响应可达 28–49s，个别请求挂起至 120s `ReadTimeout('')`
  → RunFailed。E3/D3/D5/E5/E6/E9 等 6 个 Run 均受此影响，属 LLM 侧慢响应/限流，
  不是 Scheduler 死锁。
- MCP 工具名不匹配：LLM 调用 `memory/search_nodes`，服务器实际暴露 `search_nodes`
  （MCP registry 返回 404 "not found"）。`e7ace0dd` 因此 revise 后仍失败。
  建议在 `mcp_call` 侧增加 `server/tool` 前缀归一化。

### 13.6 本轮结论

```text
--loop 重启 + 新 LLM 后：Docker/环境链路恢复，多租户隔离、契约校验、幂等竞争、
confirm 闭环均正常；
新暴露 2 个受信边界问题：
  P1 空 tenant header → 500（middleware 未兜底空值）；
  P1 list 交付契约 path 语义未归一化 → 成功执行被误报未达成；
性能：qwen3.7-max 高并发慢响应（最长 120s 挂起）是当前长尾主因。
```

---

## 14. 两个受信边界 Bug 的修复与验证（2026-08-13）

对 13.3（空 tenant header → 500）与 13.4（list 交付契约 path 语义未归一化）
按"复现测试 → 修复 → 真实服务验证"闭环处理。

### 14.1 Bug A：空 tenant header → 500（已修复）

**根因**：`tenant_context_middleware` 使用 `request.headers.get("X-Tenant-Id", "default")`，
客户端显式发送 `X-Tenant-Id: ""` 时返回空字符串而非默认值；`set_current_tenant("")`
抛 `ValueError`，middleware 未捕获 → 500。

**修复**（两层纵深防御）：

- `harness/core/tenant.py` — `set_current_tenant` 对空/空白 tenant_id 回退 `default`；
  仅保留对超长（>128）值的拒绝。
- `harness/api/app.py` — middleware 改为 `request.headers.get("X-Tenant-Id") or "default"`
  + `try/except ValueError` → 400（超长 tenant 返回明确错误而非 500）。

**回归测试**（`tests/test_backend_integration.py::TestTenantAndWebSocketIntegration`）：
`test_empty_tenant_header_falls_back_to_default`、`test_blank_tenant_header_falls_back_to_default`
——空/空白 header 均返回 200 且事件 tenant_id 为 `default`。

**真实服务验证**：`X-Tenant-Id: ""` → `200 {"run_id": "..."}`（修复前 500）。

### 14.2 Bug B：list 交付契约 path 语义未归一化（已修复）

**根因**：`RequiredOperation.step_satisfies` 按字面等值比较 `path`。契约抽取（LLM）把
workspace 根写成人类可读别名（`"workspace"`、`"workspace directory"`），Planner 实际
使用真实路径 `"."`。语义等价但字面不同 → 判定 unmet → `Deliverable not met` → RunFailed
（B4/B9/E7 三个用例误报）。

**修复**（`harness/models/plan.py`）：新增 `_paths_equivalent` 归一化函数——

- 仅对 `file_op` 的 `operation == "list"` 生效；
- 将 workspace 根别名集合（`.`、`./`、`workspace`、`workspace directory`、
  `current directory`、`the workspace`、`/workspace`、`root`）归一等价；
- 非 list 操作、明确的子路径、越界路径仍严格字面匹配，不扩大匹配面（保持安全）。

**回归测试**（`tests/test_deliverable_gate.py`）：
`test_list_contract_with_workspace_directory_aliases_met`、
`test_list_contract_workspace_root_alias_variants_met`。

**真实服务验证**：`List the contents of the workspace directory`（Run `319c57c8`）：

```text
DeliveryContractsResolved: contract file_op list path="workspace"
PlanCreated:                step file_op list path="."
RunCompleted:               deliverable_met=true, deliverable_status="met"
```

修复前该场景为 `Deliverable not met` → RunFailed。

### 14.3 测试结果

```text
新增复现测试（先红后绿）：4 个
  Bug A：空/空白 tenant header ×2
  Bug B：list 路径归一化 ×2
全量回归：1123 passed, 2 skipped
ruff：All checks passed
```

### 14.4 附带观察：`--reload` 未自动重载新代码

修复后 `--reload --reload-dir <abs path>` 未能监听到 `harness/core/tenant.py` 与
`harness/api/app.py` 的变更（服务仍执行旧中间件逻辑），需手动重启进程加载新代码。
建议后续排查 WatchFiles 在 Windows 绝对路径下的监听行为，或改用固定端口手动重启。

### 14.5 结论

```text
两个 P1 受信边界问题均已通过回归测试 + 真实 LLM 黑盒验证；
修复遵循"受信组件确定性兜底"原则：
  tenant 空值回退 default（不依赖客户端传正确 header）；
  list path 语义归一化收窄到 workspace 根别名（不扩大匹配面）。
```

## 15. 第二轮 30 用例真实 LLM 黑盒测试（2026-08-13）

### 15.0 前置环境修复：`--loop` / reload 与 Docker 子进程

- **根因（Bug 5 复发）**：`harness/api/serve.py` 的 `main()` 以
  `uvicorn.run(..., reload=True)` 启动且**未传 `--loop`**，Windows 下 reload 模式
  的 worker loop 建立早于 app import，自定义 loop factory 不生效 →
  `SelectorEventLoop` → `asyncio.create_subprocess_exec` 抛
  `NotImplementedError`（`docker.check_available` 即触发）。
- **修复**：`serve.py` 在模块 import 阶段调用 `configure_event_loop_policy()`
  （`harness/api/serve.py:32`），在 uvicorn 创建任何 loop 之前把 Windows policy
  设为 Proactor。**不依赖 uvicorn 的 `--loop` 参数**，因此无论 reload 与否均生效。
- **验证**：冒烟 run `f1881e6e` 完整事件链 15 事件，
  `RunCompleted` + `deliverable_met=true`；docker sandbox 内 `file_op` 写读
  `smoke-ok-30` 成功；LLM 响应 4.3s（qwen3.7-max）。
- **启动命令**（已在第 15 轮测试使用，勿加 `--reload`）：
  ```
  .venv\Scripts\python.exe -m uvicorn harness.api.serve:app --host 0.0.0.0 --port 8000 --loop harness.api.loop:event_loop_factory
  ```

### 15.1 契约说明（重要，测试前确认）

- **`POST /api/v1/runs` 不接受 `scope` 字段**（`CreateRunRequest` 无该字段，
  Pydantic 静默忽略）。执行环境完全由 `workspace_id` 决定。
- directory workspace 的 `target` 必须是对象
  `{"type": "directory", "filesystem_root": "..."}`（缺失 `filesystem_root` →
  422）。
- sandbox(docker) workspace 的 `target` 需
  `{"type": "sandbox", "docker_image": "python:3.11-slim", "host_mount_src": "...", "mount_root": "/workspace"}`。
- `ExecutionTargetType` 枚举：`directory` / `sandbox` / `remote`（无 `docker` 值）。

### 15.2 测试统计（27 个执行用例 + 3 个校验用例 = 30）

| 指标 | 数量 |
|------|------|
| RunCompleted（含 unverified） | 19 |
| RunFailed（均为合理受信组件拒绝） | 7 |
| HTTP 404（未知/跨租户 workspace 正确拒绝） | 3 |
| 已取消（c20 读 /etc/hostname 挂起 >3min） | 1 |
| deliverable_met=true | 12 |
| deliverable unverified（契约提取超时） | 7 |

并发 5、单 run 硬超时 300s。LLM：qwen3.7-max。

### 15.3 通过用例（执行正常 + deliverable_met=true）

c01 写读 / c02 写 JSON / c03 多文件 / c06 append 链 / c07 两文件合并 /
c09 CSV 建读 / c10 替换读回 / c12 失败后自愈重建 / c15 自动建父目录 /
c16 docker 写读 / c18 docker 多文件读加 / c29 中文写读。

### 15.4 执行成功但 deliverable unverified（契约提取超时）

c04（嵌套目录）、c17（docker list）、c25（list 归一化回归）、c26（docker 多文件）、
c27（repeat 写读）、c28（三次 append）、c30（三轮写读循环）。

- **现象**：这些 case 的事件流中 `DeliveryContractsResolved` 均为
  `{"contracts": [], "source": "extracted", "timed_out": true, "error": "contract extraction timed out"}`。
- **根因**：`harness/api/routes.py:360-363` 契约提取 `asyncio.wait_for(timeout=15.0)`。
  换用 qwen3.7-max 后单次 LLM 响应 28-49s，超过 15s → 提取超时 → `contracts=[]`。
- **判定**：符合架构 D-04 设计（"抽取失败 → contracts=[]，unverified，不阻断 Run"），
  **非 Bug**。但记录为环境相关观察：契约提取超时阈值 15s 在高延迟 LLM 下过紧，
  可考虑在 LLM 慢环境下调大（如 40s）以恢复 deliverable 验证能力。

### 15.5 合理失败用例（受信组件正确拒绝，非 Bug）

| Case | 场景 | 结果 |
|------|------|------|
| c05 | 读不存在文件 | RunFailed（contract 声明 read missing.txt，失败正确） |
| c08 | mcp_call `calculator`（不存在的 MCP 工具） | RunFailed：`MCP tool 'calculator' not found on server 'memory'`，3 次重试后拒绝，系统未崩溃 |
| c11 | 越界路径 `../../evil.txt` | RunFailed：`GuardrailTriggered` "Path '../../evil.txt' is outside the sandbox workspace root"，0/1 step 完成，拒绝生效 |
| c13 | HTTP 无效域名 | RunFailed：`getaddrinfo failed`，受信层捕获为 ToolFailed |
| c14 | 调用 `nonexistent_tool_xyz` | RunFailed：MCP 工具不存在，系统拒绝 |
| c19 | docker 内创建并运行 python 脚本 | RunFailed：`Task cannot be completed`（sandbox workspace `allowed_tools` 仅含 file_op/mcp_call，无 exec 工具；用例设计问题，非 bug） |

### 15.6 校验用例

- c21 空 intent：**未在 API 层拒绝**（`schemas.py` 无 min_length），但系统以
  RunCompleted 优雅响应 "didn't include a specific question"。行为合理，非 bug；
  记录观察：API 契约层无空 intent 显式 400。
- c22 无 tenant header + 明确 workspace_id：404（默认 tenant 下无该 workspace，
  跨租户隔离正确）。
- c23 / c24 未知 workspace_id：404 `Workspace not found`（正确）。

### 15.7 已取消用例（c20 挂起排查结论）

- c20 读 `/etc/hostname`：run `bad848cf` 初始被判定"挂起 >3min"并取消。
  **跟进排查后确认为误判**，事件链如下（2026-08-13）：
  1. `s1` file_op read `/etc/hostname` → **GuardrailTriggered**
     "path escapes Docker workspace mount"（docker 沙盒越界访问被受信组件确定性拦截 ✅）。
  2. revise 阶段 LLM 尝试 `mcp_call execute cp /etc/hostname hostname.txt`，
     因 `$step.output` 引用不被允许触发格式错误重试。
  3. qwen3.7-max 两次响应分别耗时 **106s / 60s**，revise 链路累计
     17:40:23 → 17:43:09 才结束，最终 `RunFailed`。
  **结论**：c20 是 Guardrail 正确拦截 + 高延迟 LLM 放大 revise 耗时的组合，
  非系统挂起。轮询脚本 300s 硬超时在 LLM 极端慢响应下先于真实终态放弃。
  日志无死锁、无未清理任务（`MONITOR Cleaned up run bad848cf`）。

### 15.8 配置缺陷修复：契约提取超时 15s → 45s + run 全局 deadline

#### 15.8.1 契约提取超时 15s → 45s

- **问题**：`harness/api/routes.py` 两处契约提取 `asyncio.wait_for(timeout=15.0)`，
  qwen3.7-max 单次响应 28-49s（峰值 106s），15s 超时频繁触发 →
  `DELIVERY_CONTRACTS_RESOLVED` 落 `contracts=[]` → 7 个用例 `deliverable_status=unverified`，
  交付验证能力被静默丢弃。
- **修复**：
  - 新增模块级常量 `CONTRACT_EXTRACT_TIMEOUT = 45.0`（`routes.py`，含注释说明）。
  - `create_run` 与 conversation message 两处统一使用该常量。
  - `_build_delivery_contracts`（caller 显式契约路径）也补上同一超时兜底，三处一致。

#### 15.8.2 run 全局 deadline 默认 0（无预算）→ 600000ms

- **问题**：`serve.py` 的 `SchedulerConfig.run_timeout_ms` 默认 `0`（禁用）。
  Q-07 的 `_phase_call` 预算保护（`base.py:530`）仅在 `run_timeout_ms>0` 时生效；
  c20 的 revise 阶段 3 次 LLM 调用（106s/60s/计划重试）无任何超时兜底，run 无界拖长
  （真实时间 5+ 分钟），测试端看起来像"挂起"。
- **修复**：`serve.py` 默认值 `"0"` → `"600000"`（10min），watchdog 到期强制
  `RunFailed("run_timed_out")`（`base.py:256`）。仍可用 `HARNESS_RUN_TIMEOUT_MS` 覆盖。

#### 15.8.3 重测结果（2026-08-13，重启后）

| Case | 修复前 | 修复后 |
|------|--------|--------|
| c04 / c17 / c25 / c26 / c27 / c28 / c30 | unverified | **deliverable_met=true, status=met** ✅ |
| c20 读 /etc/hostname | 3 次 revise 拖 5min | LLM 120s 超时 → 结构化 RunFailed，2min 收敛 ✅ |

- 契约提取恢复：c04=1、c17=2、c25=2、c26=4、c27/c30 均成功提取并验证。
- c20 终止路径：`ReadTimeout('')` 由 LLM client 抛出 → `base.py:260` L3 受信组件屏障捕获 →
  `RunFailed("Unhandled scheduler error: ReadTimeout('')")`。符合 AGENTS.md §6.1/§3.5
  （逃逸异常必须转结构化事件，不产生 "Task exception was never retrieved"）。
- 全部 run 有终态、无挂起；`MONITOR Cleaned up` 均正常。

### 15.9 结论

```text
环境问题（--loop/reload 导致 Docker NotImplementedError）已根治：
  serve.py 顶层 configure_event_loop_policy()，不再依赖 uvicorn --loop。
30 用例 + 8 用例重测：无新 P1/P2 Bug；
  c08/c11/c13/c14 验证了受信组件对非法工具、越界路径、网络失败的确定性拒绝；
  c20 排查确认是 Guardrail 正确拦截 + 高延迟 LLM 的组合，非系统挂起；
  两项配置缺陷已修复并重测验证：
    契约提取超时 15s → 45s（unverified 恢复为 met）；
    run 全局 deadline 默认 0 → 10min（无界等待 LLM 有 watchdog 兜底）；
  run 请求 scope 字段被静默忽略为契约观察项，见 15.10。
```

### 15.10 遗留待查

- run 请求 `scope` 字段被静默忽略：建议在 OpenAPI 中移除或显式报 422，
  避免调用方误以为 scope 生效（当前唯一控制点是 workspace 的 scope）。
- `create_run` 在响应前同步执行契约提取（最长 45s）：高延迟 LLM 下 POST 会等
  45s 才返回。可评估改为后台提取 + 事件推送，避免阻塞调用方。
