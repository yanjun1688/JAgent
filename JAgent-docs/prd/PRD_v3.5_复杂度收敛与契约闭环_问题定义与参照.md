# PRD（问题定义）— 复杂度收敛、契约闭环与单点风险观察

| 属性 | 值 |
|---|---|
| **文档类型** | 问题定义 PRD（**只列问题与证据，不含修复方案**） |
| **日期** | 2026-09-09 |
| **作者** | 架构导师视角（历次 code review 观察汇总：2026-09-08/09 review 批次 + ③④⑤ 改动过程） |
| **范围** | 架构设计债务观察，非功能规划；面向下一个迭代的裁剪/加固输入 |
| **状态** | 草案，等 owner 决策如何修改 |
| **非目标** | 不排序、不拍板方案、不评估现有实现正确性（无 P0 级已知缺陷） |

---

## 0. 一句话

Harness 的内核设计（受信/非受信边界、证据不因压缩丢失、fail-safe 永不假绿）是真正扎实的；
本 PRD 记录的**不是基础错误，而是"规模开始超出维护容量"的早期信号**——同一语义开始在不同文件里
各写一遍、启发式在驱动关键质量杠杆却无度量闭环、前后端契约同源依赖人肉纪律、环境单点 Windows。

---

## 1. 问题 A：同一语义多实现 → 漂移（不是偶发，是模式）

### 证据（均为本仓库已发生的实例；多数事后已修，但证明该模式反复出现）

- **A1 完成门判定多份内联**：`_execute_plan` 曾把"完成门判定"内联在 3 处 + 1 个 helper，且三处行为已
  漂移（空事件先发 "task complete" 后 RUN_FAILED 等）。见 DESIGN §11.6 /
  `reviews/review_20260908_completion_gate_duplication.md`。
- **A2 Episode→LLM 渲染器两份且字段集不同**：`agent_kernel` 渲染 Title/Summary/Key decisions/**Tools
  used**/**Errors**/**Current plan**，`planner/prompts`（answer 构建）只有 Title/Summary/Key
  decisions/Key findings——同一对象在两条 LLM 工作视图路径上被各写一遍且不一致。新增 `preserved_excerpts`
  字段时需手动同步两处。已收敛为 `Episode.to_context_text()` 单源（DESIGN §11.9）。
- **A3 抽取与规划的重试语义脱节**：`ContractExtractor.extract` 只在 LLM 调用异常时重试，JSON 解析失败
  直接返回空；`Planner.plan/revise` 则把解析失败经 `retry_prompt` 反馈重试。同属"受信消费非受信输出"的
  两条路径，语义不同。review ③ 已对齐（DESIGN §11.9）。
- **A4 两个受信分类器对未知工具的默认方向相反**：`step_is_mutating` 对未注册工具返回 False（=非 mutating），
  `recovery._is_read_only_action` 对未知工具默认视为有副作用（=非只读）——方向相反，属受信校验器间隐式
  顺序依赖。review Q-06 已对齐为 fail-closed。见 `reviews/review_20260908_unknown_tool_q06_fail_closed.md`。
- **A5 "定义即死"残留反复出现**：`_seq_locks`、`recovery._TOOL_UNAVAILABLE_PATTERNS`、
  `agent_kernel._is_stop_signal` 等死代码为同批第三次同类残留，才触发全项目扫描
  （`reviews/review_20260908_dead_code_sweep.md`）——说明存在产生"旁路实现"的习惯性路径。

### 结构性判断

受信规则在代码中多次出现时，**修一处漏一处会让安全语义悄悄漂移**；目前唯一防线是 review 循环人肉比对，
成本高且不可扩展。缺的是"同一条规则只有一个真源"的**强制**机制（编译期/测试期/CI 期）。

### 可借鉴的外部参照（启发，非方案）

- 契约测试：**Pact**（服务间契约）、**openapi-diff / openapi-generator + CI diff 失败即红灯**。
- 属性/变异测试找"双实现漂移"：**Hypothesis**、**mutmut**（变异测试能暴露"另一份实现被改了但测试没死"）。
- 决策可追溯：**adr-tools** / Architecture Decision Records（本项目 DESIGN §11.x 已是 ADR 雏形，可工具化）。
- 单一真源模式：pydantic model → JSON Schema → 前后端类型单向派生 + CI 校验（见 §4）。

---

## 2. 问题 B：概念与状态空间超出维护容量（第二系统征兆）

### 证据

- **B1 事件面庞大**：38 种 EventType + 各自 payload model（`models/events.py`，README 亦标注 "38 种"）。
- **B2 正交状态/流程堆叠**（`fold.py` 的 case 分支可见全貌）：
  ExecState/TaskState 正交状态、CompletionVerdict/DeliverableVerdict/step verdict 多重完成判定、
  三层压缩（lazy_clear/episode_archive/emergency_compact）+ importance 打分 + keep_recent 窗口 +
  checkpoint/resume + preserved_excerpts、monitor 反馈注入、局部修复（F-6）、carrier reaper、
  Q-07 watchdog、幂等+语义重试共享预算……单组件接缝极多。
- **B3 同类"兜底"多层叠加**：未知工具的拦截同时由 revision invariants → PlanGuardrail → dag_executor
  三层保证（见 Q-06 review 的"三层兜底"论述）——每层都对，但任何一层被单独调用即成真洞，靠隐式顺序依赖。
- **B4 每加一个概念的边际成本是线性叠加的**：要同步架构文档 + AGENTS 校验清单（§6.3 人肉）+ 前后端枚举 +
  事件 payload + 测试 pin + OpenAPI。概念越多，同步链越长，漂移概率越高。
- **B5 两条主路径并存**：Mock 与 Real LLM 客户端、文本/function-calling 双路径、serial/planning 双调度器；
  非受信路径自动化测试成本高，倾向落到 scripts（不进 CI）。

### 影响

认知负荷高 → 本次各 review 修的重复/漂移**几乎都发生在概念接缝处**。风险是"每修一个 bug 增加一处
通用防护"（AGENTS §3.5）在概念过多时会退化为"每修一处再加一层"，让系统越来越重。

### 可借鉴的外部参照

- **第二系统效应**（F. Brooks《人月神话》）——警惕"这次把所有上次没做对的一次性做对"。
- 复杂度预算进 CI：**radon**（圈复杂度/维护指数）、**wily**（按提交跟踪复杂度与重复），设红线而非事后度量。
- 内存/记忆分层研究的**反向简化**：MemGPT/Letta、Mem0、Zep/Graphiti、Generative Agents
  的"分层 + 重要性/时效"模型可作为裁剪三层压缩的参照系（注意受信边界约束）。
- 主动裁剪的工程文化：37signals 的 shaping / 每次迭代设"删除配额"（deletion budget）比无上限叠加更可续。

---

## 3. 问题 C：启发式驱动关键路径 + 缺度量闭环

### 证据

- **C1 token 估算为启发式**：`HeuristicTokenCounter` 约 0.25 字/字符；且
  `_async_estimate_context_tokens` 只统计 thoughts/tool_results、**不统计 summary**（含 preserved
  excerpts）——压缩触发与实际占用脱节。PRD_v3.4 记载的 `token_estimate=31470 token_limit=3000`
  事故即由估算/阈值失配放大。
- **C2 importance 打分为关键词启发式**：decision thought=0.7 / 普通=0.5 / failed&timeout=0.8 /
  unsuccessful=0.6 / completed=0.2，代码 docstring 自认 "Phase 1 MVE"。
- **C3 LLM 摘要保真度无自动评估**：压缩是有损折叠，目前只能人工读；而系统**恰好保留了不可变原文
  （Event Store）**，具备低成本做"摘要能否支撑下游问答"自动比对的独特条件，却未利用。
- **C4 成本/质量无 per-run 闭环**：有上限（Q-07 总预算、retry cap、max_tokens），但每 Run 实际
  token 消耗/轮次/压缩次数没有记账回流到产品与观测面板。

### 影响

这些近似恰好控制着"喂给 LLM 什么"——是质量与成本的第一杠杆，却恰恰是最没有度量的部分。

### 可借鉴的外部参照

- 真实计数：**tiktoken** + 观测侧 usage 埋点（已接 Langfuse，可回流 token 用量面板）。
- 摘要/记忆保真度自动评估：**ARES / RAGAS / LLM-as-judge**；本项目可用 Event Store 原文当 ground
  truth，做"仅凭 summary 能否回答原任务相关问题"的回归——这是多数项目没有的便宜条件。
- 记忆分层参照：MemGPT/Letta 的 memory 分层、Mem0 提取式记忆、Zep/Graphiti 时间知识图
  （用于取代/增强关键词 importance 的启发式）。

---

## 4. 问题 D：前后端契约"同源"未闭环（依赖人肉纪律）

### 证据

- **D1** README 声称后端 Pydantic 为唯一来源、前端类型由 OpenAPI 生成；但 `public/openapi.json`
  是**入库的手工导出物**，且曾出现 `bugs/JAGENT-2026-P1-11_Checked_In_OpenAPI_Invalid_UTF8.md`
  ——说明生成链路是手工的、无 CI 门禁。
- **D2** 本批新增 `Episode.preserved_excerpts` 这类事件 payload 字段时，前端/TS 类型同步靠 AGENTS
  §6.3 清单人肉检查；事件枚举"38 种"前后端同步同样靠纪律。
- **D3 文档拓扑自身已漂移**：顶层 `README.md` 第 6 行把 `JAgent-docs/README.md` 标为"文档总入口
  （进度看板 + 全站导航）"，**该文件实际不存在**（2026-09-09 核对 `JAgent-docs/` 目录，无 README）。

### 影响

结构宣称"单源"，实际依赖 review 纪律兜底 → 迟早一次字段/枚举漏同步，前端静默坏或类型失真。

### 可借鉴的外部参照

- **openapi-generator / openapi-diff + CI**：提交后自动生成并 diff，不一致即失败。
- **schemathesis**：基于 OpenAPI 的属性测试，自动打 API 暴露契约破绽。
- **Pact**：前后端分离时的消费者驱动契约测试。
- 消除"D3 这类漂移"：文档链接/入口纳入 CI 死链检查（或入口自动化生成）。

---

## 5. 问题 E：Windows 单点 + 编码/平台脆弱

### 证据

- **E1 编码事故（本次实际发生）**：用 PowerShell 默认编码重写 6 个脚本文件，直接把 UTF-8 写坏成非法
  字节（ruff E902），靠 `git checkout` 恢复——而恢复还连带还原了同文件里已完成的死代码删除（差点丢成果）。
  根因之一：仓库存在中文文件名/GBK-UTF-8 展示乱码，且 shell 默认编码不是 UTF-8。
- **E2 平台分支代码多**：asyncio Proactor、playwright-mcp、Docker 子进程各有一堆平台修复与 README
  专门的长段 Windows 运行说明（`loop.py` event_loop_factory、`reload/workers` 限制等）。
- **E3 后端覆盖不均**：测试大多跑本地 directory 后端；docker/ssh ExecutionBackend 的平台差异行为
  （沙盒隔离级别）覆盖弱，多为手动/脚本验证。

### 影响

换环境/换人即出血；若无 CI 在非 Windows 上验证，平台债长期不可见。

### 可借鉴的外部参照

- **.gitattributes** 统一行尾/编码；**pre-commit** 加编码校验钩子。
- CI 上 **Linux + Windows 双跑**，或至少 Linux 跑全量、Windows 跑路径敏感子集。
- 全仓强制 UTF-8（含文件名 ASCII 化或统一编码），消灭"编码正确性靠人"的状态。

---

## 6. 过程层观察（人 + AI 工作流的护栏缺口，与代码无关）

### 证据

- 跨会话改动长期积压在未提交工作区，`checkout`/`reset` 可直接摧毁成果（E1 已发生一次差点丢）。
- 一次提交 45 文件、混合多个 review 项（`447cfa2`），无法按项 revert/bisect/分块评审。

### 可借鉴的外部参照

- GitHub flow / 短命分支；conventional commits；单主题小 PR。
- 中断即 WIP commit/stash 的纪律；合并后删分支；本地以 `origin/main` 快进同步，不做二次本地合并。

---

## 7. 分级汇总（仅风险，不含方案）

| # | 风险 | 分级 | 状态 | 主要证据 |
|---|---|---|---|---|
| A | 同语义多实现漂移 | P1（结构性） | 已现 5 类实例，多数已修但无强制机制 | §1 A1–A5 |
| B | 概念/状态空间超维护容量 | P1（结构性） | 进行中 | §2 B1–B5 |
| C | 启发式驱动关键路径、无度量 | P2 | 进行中 | §3 C1–C4 |
| D | 前后端契约同源未闭环 | P2 | 进行中（含 D3 文档入口失效） | §4 D1–D3 |
| E | Windows 单点 + 编码脆弱 | P2 | 已现一次实际损坏事故 | §5 E1–E3 |
| F | 元流程护栏缺失（人+AI） | P2 | 已现差点丢成果 | §6 |

---

## 8. 明确不在本文范围

- 不给出修复方案、不做优先级/排期裁决（等 owner 决策）。
- 不评估现有实现"对不对"——当前无已知 P0（无证据丢失、无安全绕过、完成门不假绿）。
- 不做产品功能规划（新能力/路线图）。

---

## 9. 一句话收尾

先让系统变简单，再让它变强：当前最大的风险不是缺能力，而是**能力/概念的边际成本正在逼近单人维护的
上限，而唯一的护栏（review + 文档同步）本身就是最贵的那部分**。

------------------------------
先说一句最重要的：你感觉"维护不动"，不是因为你懒或能力不够，而是这套系统的复杂度长在了错误的地方。它不是"功能太多"，而是"同一个东西被反复实现、然后你必须花力气让它们保持一致"。我们这几轮修的所有 bug——3 份完成门、两个字段集不一致的渲染器、抽取器和规划器重试语义脱节——没有一个是"新功能"，全部是"并行副本之间的漂移"。修副本是修不完的。
所以大方向我只会给你一个：
把系统收敛成两层：极小的神圣内核 + 可丢弃的政策层；内核里每条规则只有一个真源，其余一切由事件推导、由内核组合，禁止旁路副本。每次迭代设删除预算。
一层一层拆开讲
内核 = 必须小、必须对、变化要慢。 我数下来你的真核其实只有 5 个概念，一只手数得过来：
1. append-only 事件流；
2. fold()（唯一状态推导，含证据投影）；
3. Step/工具契约（幂等 + guardrail + backend 注入）；
4. DeliveryContract + 完成判定（永不假绿）；
5. 受信边界（谁有强制权）。
这 5 样是你真正的护城河，值得 100% 分支覆盖、慢改、每次改动像改宪法。 除此之外的一切——三层压缩、importance 打分、monitor 反馈、局部修复、checkpoint 节拍、reaper、preserved excerpts——**都只是"折叠之上的策略"，不是新的状态机。判断标准一句话：如果某个政策复杂到需要它自己的正确性论证和状态机，它就不该存在，砍掉或改成参数。
政策层 = 可删、可换、不必个个全量测试。 这样你的维护成本结构会翻转：现在每个概念都要求同等待遇（文档 + 测试 pin + 前后端同步 + OpenAPI），一旦内核只留 5 个概念、其余算政策，你就不再需要给 38 种事件、双 Verdict、三层兜底分别付"全保"的费用。
三个让这套能活下来的配套原则
1. Derive, don't duplicate。 状态从 fold 出、TS 类型从 Pydantic 出、OpenAPI 进 CI diff、prompt 从注册表出、常量阈值一个 config。你每次想"再写一份"的地方，就是未来的漂移点——这是可执行的红线，不是口号。
2. 新需求 = 组合内核原语，而不是新增原语。 想加"记忆分层"？先在 fold + 契约上做成政策，别引入第 6 个内核概念。
3. 先度量后加复杂度。 你的摘要无自动评估、token 估算靠 0.25/字符——在给记忆/压缩加任何东西之前，先用 Event Store 原文做"只凭 summary 能否回答"的回归。你能拿到多数项目没有的免费 ground truth。
操作节奏（比架构更救命）
不是再来一轮大重构（你现在的状态经不起），而是*"冻结新功能 + 删除配额"跑两三个迭代*：每个迭代砍掉一个非内核概念或合并一份副本，砍完跑全量。两三个迭代后你会发现维护面肉眼可见地小一圈。别重写——你的内核是对的，重写只会把已经验证过的正确性丢掉。
最后一句实话
这套系统是你和 AI 协作维护的。那就要为"AI 可维护性"设计：单一真源、小文件、显式契约、CI 能自动判错的规则——因为这些正是让 AI 不漂移的东西。你现在的文档很厚，但文档是"给人防漂移"的；把防漂移从文档搬进 CI 和类型系统，才是让你真正喘过气来的方向。