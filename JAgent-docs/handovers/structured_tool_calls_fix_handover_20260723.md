# 结构化 tool_calls 链路 + Planner 协议缝隙 修复交接文档

## 【项目背景】

- **项目**: Harness v2.1 Agent-First 任务执行引擎
- **路径**: `D:\Project\JAgent`
- **任务来源**: `JAgent-docs/reviews/fix_prompt_for_ai.md`（提示词）+ 三份 review
  - `reviews/planner_tool_filtering_review_20260722.md`
  - `reviews/planner_protocol_gaps_review_20260722.md`
  - `reviews/structured_tool_calls_review_20260722.md`
- **技术栈**: Python 3.11、FastAPI、Pydantic v2、aiosqlite/SQLite、pytest、pytest-asyncio
- **核心约束**: 事件溯源 + 受信/非受信边界（AGENTS.md）；决策权归 Agent，强制权归系统
- **本次范围**: L4（Agent Kernel / LLMClient）+ L5（Planner / System Prompt / 工具描述）+ 一处 L3 接缝（`_run_tool_call` 透传 `override_tool_call_id`）
- **执行框架**: 按 AGENTS.md §3.4 审查三步——Step 1 报告差异 → Step 2 修文档 → Step 3 分阶段开发，阶段间停下报告等用户确认

## 【本次 Bug 总览】

来源 review 已识别的缺口：

| 编号 | 缺口 | 优先级 |
|---|---|---|
| T-1 ~ T-8 | `LLMClient.chat` 返回 `str`，把 `tool_calls` 压平为 `THOUGHT:/TOOL:/ARGS:` 文本；`tool_call_id` 丢失；多轮历史违反 OpenAI 协议；json 解析失败静默 swallow | P0/P1 |
| B-1 ~ B-3 | `_SERIAL_THINK_PROMPT` 双重教导；`_FallbackKernel` 与 `LLMAgentKernel` 90% 重复；主路径正则依赖 | P1 |
| C-1 ~ C-6 | Planner 工具过滤死分支；revise 丢失原 intent；`StepResult` dict-compat；done step 不展示 output keys；PLAN 示例引用不存在的 `browser_search`；裸 print 调试残留 | P1/P2 |

## 【用户已拍板决策（2026-07-23）】

详见 `fix_prompt_for_ai.md §8`。要点：

1. **ThinkResult 字段**：保留现状（`thought/tool_name/tool_input/token_count/direct_answer`），只新增 `tool_call_id: str | None = None`；**不引入** spec 示意但无消费者的 `tool_input_str`
2. **L3 接缝单点改动允许**：`BaseScheduler._run_tool_call` 调 executor 传 `override_tool_call_id=think_result.tool_call_id`，仅此一处
3. **C-1 始终全量喂工具**：删除 `planner.py:441-444` 入口过滤分支，`_build_tool_descriptions` 始终返回 `registry.list_tool_defs()` 全部工具的 **name + description + 完整 input_schema**（不是仅名字）；同步删除 `_filter_tools_by_intent` / `_extract_tool_keywords` / `ALWAYS_INCLUDE`
4. **C-3 范围收紧**：本次**仅删** `"idempotency_hit"` 死分支字面量；`SOFT_ERROR` 是否计入 `completed_step_ids` **暂缓**——属「任务完成 vs 工具完成」语义问题，需用户研究业界后定方案。详见 `ARCHITECTURE_v2.1.md §3.7` 缺口 S1（已写入业界检索关键字 + 暂缓范围清单）
5. **C-5 PLAN 示例用抽象占位**：不绑定具体工具名，三个示例改为 `A→B→C` / `s1(X) → s2(Y), depends_on=["s1"]` 抽象占位
6. **C-2 双槽 revise 一并做**：`Plan` 模型新增 `user_intent: str = ""` 持久化原始用户意图；`_REVISE_PROMPT` 拆 `Original User Intent / Plan Intent` 双槽

## 【已完成修改】

### Step 2 — 文档对齐（已完成）

1. **`JAgent-docs/archive/v2.x/ARCHITECTURE_v2.1.md` §3.7 新增**：「已知架构缺口 — 任务完成概念歧义（S1，待设计）」
   - 突出展示"工具完成 ≠ 任务完成"的核心歧义
   - 附业界对照表 + 检索关键字（Task Success Criteria / Airflow TaskInstanceState vs DagRun state / Temporal ActivityResult vs WorkflowExecutionState）
   - 明确暂缓改动清单：`planner.py:274-276` / `dag_types.py:13-18` / `dag_executor.py:282-284` / `dag_types.py:37-51`
   - 唯一允许改动：删除 `"idempotency_hit"` 死字面量

2. **`JAgent-docs/reviews/fix_prompt_for_ai.md` §8 新增**：记录全部 7 项已拍板决策

3. **三份 review 文档**：对应缺口行加状态标注（✅已采纳 / ⚠️暂缓设计）

### Step 3 阶段 A — 结构化 tool_calls 接口契约（已完成）

**A-1/A-2：`harness/core/llm_client.py` 新增 Pydantic 模型 + 签名升级**
```python
class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]

class ChatResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = ""
    raw: dict[str, Any] | None = None

# LLMClient.chat / MockLLMClient / OpenAILLMClient 全部 -> ChatResponse
```

**A-3：`OpenAILLMClient.chat` 重写**
- 保留 `tc["id"]`（原 line 126 丢弃，P0 bug 修复）
- `json.loads(raw_args)` 失败时 `_logger.warning` + `arguments = {"_parse_error": raw_args}`，绝不静默
- 删除把 `tool_calls` 压平为 `f"TOOL: {name}\nARGS: {args_str}"` 文本的代码段

**A-4：`MockLLMClient` 改造**
- 构造器接受 `list[str | ChatResponse]`，字符串自动包装为 `ChatResponse(content=str)`
- 测试 fixture 全部改用 `.content` / `.tool_calls` 访问

**A-5：`LLMAgentKernel.think` 重写**（`harness/core/agent_kernel.py` 整文件重写）
- 直接消费 `resp.tool_calls` 构造 `ThinkResult`，**删除** `_parse_results` / `_parse_segment` / `_TOOL_SPLIT_RE` / `_ARGS_GREEDY_RE` / `_THOUGHT_RE` / `_ANSWER_RE` 正则全套
- 文本路径仅当模型未发 tool_calls 而是纯文本 `ANSWER:` / `<STOP>` 时进入
- `_generate_stop_summary` 同步使用 `resp.content`

**A-6：`ThinkResult` 新增字段 + L3 接缝透传**
- `harness/core/scheduler/base.py`：`ThinkResult` 新增 `tool_call_id: str | None = None`
- `BaseScheduler._run_tool_call`（`base.py:476`）调 executor 加 `override_tool_call_id=think_result.tool_call_id`

**A-7：多轮历史按 OpenAI 协议重建**
- `LLMAgentKernel._build_history_messages(state)` 新方法：遍历 seq-ordered timeline，按"think 边界"分组
- 每个 `ThoughtEntry` 开新 `assistant` 消息（含 `content=thought`），其后所有 `ToolResult` 累加到 `assistant.tool_calls` 数组
- flush 时紧随追加与 `tool_calls` 1:1 配对的 `role=tool` 消息（`tool_call_id` 关联）
- 当 thought 在窗口外但 result 在窗口内时，自动创建空 content 的 assistant 占位以满足配对契约

### Step 3 阶段 B — 移除双重教导与降级塌陷（已完成）

**B-1：`_SERIAL_THINK_PROMPT` 拆分**（`harness/core/system_prompt.py`）
- 新增 `AgentPhase.SERIAL_THINK_FN`：function-calling 路径用，**移除** `TOOL:/ARGS:` 文本格式说明，只保留 A/B/C 决策骨架 + "用提供的 tools 调用工具"
- 新增 `AgentPhase.SERIAL_THINK_TEXT`：保留原文本格式，仅 fallback 用
- `LLMAgentKernel` 根据 `tools is not None` 选择 FN 版本

**B-2：移除 `_FallbackKernel`**
- **整文件删除** `harness/core/scheduler/fallback_kernel.py`
- 调用方 `harness/core/scheduler/plan.py:470` 改为 `LLMAgentKernel(self.planner.llm)`
- 移除 `from harness.core.scheduler.fallback_kernel import _FallbackKernel` 导入

**B-3：主路径正则依赖移除**
- 随 A-5 完成，`_ARGS_GREEDY_RE` / `_TOOL_SPLIT_RE` / `_THOUGHT_RE` / `_parse_segment` / `_parse_results` 全部删除

### Step 3 阶段 D · D-1/D-2 测试更新（已完成部分）

- **删除** `tests/test_fallback_kernel.py`（`_FallbackKernel` 已删）
- **重写** `tests/test_kernel.py`：
  - 删除所有 `_parse_results` / 正则解析相关用例
  - `MockLLMClient` 断言全部改用 `.content` / `.tool_calls`
  - 新增 D-2.1 `test_llm_kernel_preserves_tool_call_id_from_provider`：Mock 注 `ChatResponse.tool_calls id="call_abc"`，断言 `ThinkResult.tool_call_id == "call_abc"`
  - 新增 D-2.2 `test_llm_kernel_history_pairing_two_rounds`：构造 2 轮历史，断言 messages 含 2 条 `assistant.tool_calls` + 2 条 `role=tool` 1:1 配对，顺序正确
  - 新增 D-2.3 `test_openai_client_preserves_tool_call_id_on_parse_failure` + `test_parse_failure_observable_no_silent_pass`：json 失败时 `_parse_error` + warning，无异常抛出
  - 保留 `_generate_stop_summary` 两个测试（fixture 改 `ChatResponse`）
- **修正** `tests/test_commands.py`：删除 `TestFallbackKernelParsing` 整个 class（依赖 `_parse_results`）

### 下游连带修复（因 `LLMClient.chat` 返回类型变化）

`ChatResponse` 返回类型变更引发 4 处下游 `await self.llm.chat(...)` 调用需要取 `.content`，已修复：

- **`harness/core/context_manager.py:320`** `_summarize_episode`：`chat_resp = await self.llm_client.chat(...)` 后 `response = chat_resp.content`
- **`harness/core/scheduler/plan.py:98`** `_classify_intent`：`chat_resp.content.strip().lower()`
- **`harness/core/planner.py:229`** `plan()`：`chat_resp = await self.llm.chat(...)` → `response = chat_resp.content`
- **`harness/core/planner.py:285`** `revise()`：`chat_resp.content` 传给 `_parse_plan`
- **`harness/core/planner.py:378`** `generate_answer()`：`chat_resp.content.strip()`
- **`harness/core/agent_kernel.py:99`** `_generate_stop_summary`：`resp.content.removeprefix(...)`

## 【当前中间态风险 / 未完成项】

### 阶段 C — 完全未开始

`fix_prompt_for_ai.md §五` 的 6 个 Planner 协议缝隙修复全部待开工：

- **C-1**：删 `planner.py:441-444` 入口过滤分支喂全量 + 删 `_filter_tools_by_intent` / `_extract_tool_keywords` / `ALWAYS_INCLUDE`
- **C-2**：`harness/models/plan.py:Plan` 新增 `user_intent: str = ""`；`_REVISE_PROMPT` 拆双槽；`Planner.plan` / `revise` 调用方透传
- **C-3**：仅删 `planner.py:276` 死分支字面量 `"idempotency_hit"`（保留 `"completed"` 单一判据）。**SOFT_ERROR 暂缓**（见 `ARCHITECTURE_v2.1.md §3.7`）
- **C-4**：`dag_executor.py:344-389` `build_dag_status_text` 对 done step 追加 `→ outputs: [key1, key2]`，从 `StepResult.output` 或 `.output` 字段提取
- **C-5**：`system_prompt.py:61-67` PLAN 三个示例改为抽象占位（`A→B→C`），`_build_step_schema_text` 内常量示例同步改抽象
- **C-6**：删除 `planner.py:228` `print(self.registry.list_tool_defs())`（**注：C-1 删除过滤入口后该 print 若仍存在需一并删**）

### 阶段 D — 测试仍有 7 个失败需修

最近一次全量跑结果：`9 failed, 661 passed, 2 skipped`。修复下游 `.content` 后重跑 `test_planner.py / test_context_manager.py / test_api.py` 显示**仍有 7 失败**：

#### `test_planner.py` 4 个失败（已部分修，未重跑验证）
- `test_generate_answer_includes_feedback` / `test_answer_context_has_step_info` / `test_answer_context_shows_duration` / `test_answer_no_tool_results_produces_clean_output`
- **根因**：`tests/test_planner.py:15` 的 `_MockLLM.chat` 仍返回 `str`，已改为返回 `ChatResponse(content="Mock answer")`（line 20-22 修改完成但**未重跑验证**）
- **下一步**：重跑 `pytest tests/test_planner.py -v --tb=short` 确认修复

#### `test_context_manager.py` 3 个失败（未修）
- `test_kernel_uses_summary_when_present` / `test_kernel_formats_episode_summary_fields`：期望 `results[0].thought == "using summary"`，实际含 `THOUGHT:` 前缀和 `\n<STOP>` 后缀
- **根因**：A-5 重写后的 `_consume_response` 在 `<STOP>` 路径中 `thought = content[:200]` 未剥除 `THOUGHT:` 前缀。旧 `_parse_results` 有 `_THOUGHT_RE` 正则剥前缀，新路径走简化逻辑后丢失了这层
- `test_kernel_uses_keep_recent_count_for_window`：期望 `len(thought_msgs) == 3`，实际只有 1 条
- **根因**：`_build_history_messages` 的 window 计算或 thought grouping 与旧逻辑不等价，需对比旧行为重写
- **下一步**：阅读 `tests/test_context_manager.py:543-615` 三个测试，理解旧 `_parse_results` 如何剥 `THOUGHT:` 前缀，在 `_consume_response` 的 `<STOP>` 分支补一行 `thought = thought.removeprefix("THOUGHT:").strip()`；window 问题需对比旧行为调试

#### `test_api.py` 1 个失败（已修，未重跑）
- `test_create_run_starts_scheduler`：之前因 `_FallbackKernel` import 失败；阶段 B 删除后已通过 import 修复，**未重跑验证**

## 【修改方向 / 下一步执行清单】

### Step 1：修完阶段 D 剩余 7 个测试
1. 重跑 `pytest tests/test_planner.py tests/test_api.py -v --tb=short` 确认 `_MockLLM` 修复生效
2. 修 `test_context_manager.py` 3 个 thought 剥前缀失败：
   - `harness/core/agent_kernel.py:280` 附近 `<STOP>` 分支补 `thought = thought.removeprefix("THOUGHT:").strip()`
   - window 问题：对比旧 `agent_kernel.py:198-204` 的 `timeline.sort` 与 window 计算，确保 `_build_history_messages` 行为等价
3. 跑 `pytest tests/ -v --tb=short` 必须全绿

### Step 2：进入阶段 C
按 `fix_prompt_for_ai.md §五` 顺序执行 C-1 → C-6。每改一项跑相关测试：
- C-1/C-6 改完后跑 `test_planner.py`
- C-2 改完后跑 `test_planner.py` + 需新增 revise 双槽断言测试
- C-3 改完后跑 `test_planner.py`
- C-4 改完后跑 `test_dag_executor.py`
- C-5 改完后跑 `test_planner.py` + `test_kernel.py`（system prompt 测试）

### Step 3：阶段 D 完整收尾
- D-3 校验命令：
  ```bash
  mypy harness/core/llm_client.py harness/core/agent_kernel.py harness/core/scheduler/
  ruff check harness/
  pytest tests/ -x -v
  ```
- 禁 `# type: ignore` 填充；禁 silent pass；禁任何与本次任务无关代码

### Step 4：输出「改动摘要」
按 `fix_prompt_for_ai.md §七` 第 6 条：文件、行号、改动类型（修改/新增/删除）、对应缺口编号（T-1~T-8 / C-1~C-6）

## 【禁止事项提醒】

1. **禁 commit git**——除非用户明确说 "commit"
2. **禁 silent pass**——阶段 D 测试必须实写
3. **禁 `# type: ignore` 填充**——mypy/ruff 报错必须根治
4. **禁跨层**——本次仅 L4/L5 + 一处 L3 接缝（`_run_tool_call`），其他 L1/L2/L3 不动
5. **禁改暂缓范围**——`ARCHITECTURE_v2.1.md §3.7` 列出的 4 处暂缓改动（`planner.py:274-276` 等）只能删死字面量，不要碰 SOFT_ERROR 语义
6. **禁加注释**——除非用户明确要求

## 【关键文件清单】

### 已修改
| 文件 | 改动类型 | 对应缺口 |
|---|---|---|
| `harness/core/llm_client.py` | 重写 | T-1/T-2/T-3/T-5 |
| `harness/core/agent_kernel.py` | 重写 | T-3/T-4/T-8/A-5/A-7 |
| `harness/core/system_prompt.py` | 修改（拆分 SERIAL_THINK_FN/TEXT） | T-7/B-1 |
| `harness/core/scheduler/base.py` | 修改（ThinkResult + _run_tool_call 透传） | A-6 |
| `harness/core/scheduler/plan.py` | 修改（删 import + fallback 改 LLMAgentKernel + classify 取 .content） | B-2 |
| `harness/core/context_manager.py` | 修改（取 .content） | 连带 |
| `harness/core/planner.py` | 修改（plan/revise/generate_answer 取 .content） | 连带 |
| `tests/test_kernel.py` | 重写 | D-1/D-2 |
| `tests/test_commands.py` | 修改（删 TestFallbackKernelParsing） | D-1 |
| `tests/test_planner.py` | 修改（_MockLLM 返 ChatResponse） | D-1 |

### 已删除
| 文件 | 对应缺口 |
|---|---|
| `harness/core/scheduler/fallback_kernel.py` | B-2 |
| `tests/test_fallback_kernel.py` | D-1 |

### 已更新文档
| 文件 | 改动 |
|---|---|
| `JAgent-docs/archive/v2.x/ARCHITECTURE_v2.1.md` | 新增 §3.7 缺口 S1 |
| `JAgent-docs/reviews/fix_prompt_for_ai.md` | 新增 §8 拍板决策 |
| `JAgent-docs/reviews/planner_tool_filtering_review_20260722.md` | P0.1/2/3 状态标注 |
| `JAgent-docs/reviews/planner_protocol_gaps_review_20260722.md` | B/E/G 状态标注 |
| `JAgent-docs/reviews/structured_tool_calls_review_20260722.md` | §4 状态标注 |

### 待修改（阶段 C）
| 文件 | 待改缺口 |
|---|---|
| `harness/core/planner.py` | C-1/C-2/C-3/C-6 |
| `harness/core/dag_executor.py` | C-4 |
| `harness/core/system_prompt.py` | C-5 |
| `harness/models/plan.py` | C-2（`Plan.user_intent` 字段） |
| `harness/core/dag_types.py` | **暂缓**（仅 §3.7 暂缓清单内） |

## 【验证基线】

- 阶段 A/B 修复后定向跑：`pytest tests/test_kernel.py tests/test_commands.py` → **40 passed**
- 全量跑（修复下游 .content 前）：`9 failed, 661 passed, 2 skipped`
- 全量跑（修复下游 .content 后，最后一次）：`7 failed, 72 passed` 仅跑 test_planner/context_manager/api 三件套
- **未跑** 阶段 D 完整全量验证

## 【下一步首要动作】

**修 `test_context_manager.py` 的 3 个 thought 剥前缀失败** → 跑全量 → 确认阶段 A/B/D 完全绿 → 停下报告用户 → 等用户确认后进入阶段 C。