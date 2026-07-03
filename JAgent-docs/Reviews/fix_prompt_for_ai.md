# 修复任务提示词 — 结构化 tool_calls 链路 + Planner 协议缝隙

> 将本文件整体复制给执行修复的 AI。任务是 **按受信边界约束修复代码**，不是打补丁。

---

## 角色

你是 JAgent 项目的 **实现工程师**，在一个严格分层的事件溯源架构下工作。本项目遵循 `AGENTS.md` 的受信/非受信边界设计。你的职责是按下面的修复清单准确落地，**不得自创架构、不得跨层、不得打补丁**。

## 一、项目架构不可违背的约束（先读懂再动）

1. **决策权归 Agent，强制权归系统**：`tool_calls` 是 Agent 决策的载体，是结构化一等数据，**绝不允许在 L4（Kernel/LLMClient）边界被压平为文本再正则还原**。
2. **错误路径必须可观测**（AGENTS.md §6.1）：任何 json 解析失败、协议违和、id 丢失必须 `_logger.warning` 并写入事件流，**禁止 silent pass / silent return {}**。
3. **前后端同源契约**（§4.1）：`ChatResponse` 一旦确立，必须以 Pydantic v2 固化，OpenAPI 自动生成 TS 类型。
4. **修复根因不补表象**（§3.5）：每处修复必须回答"根因是什么 / 为什么现有机制没拦住 / 如何防止复发"。禁止在调用处加 `if` 打补丁。
5. **禁止跨层**：本次修复仅限 L4（Agent Kernel / LLMClient）与 L5（Planner / System Prompt / 工具描述）。L1-L3 的 `ToolCalledPayload.tool_call_id` 等字段已就位，本次只负责向上回填，不得改动下游。
6. **禁止引入新依赖**：只用项目已有的 `pydantic`、`httpx`、标准库。
7. **禁止在回复中提供与当前任务无关的代码实现**。
8. **禁止添加任何注释**，除非用户明确要求。

## 二、修复任务总览（按优先级 + 依赖顺序执行）

```
阶段 A（P0，主修复）— 结构化 tool_calls 接口契约
  A-1  新增 ChatResponse / ToolCall 数据模型（Pydantic v2）
  A-2  LLMClient.chat 签名:  -> str  改为  -> ChatResponse
  A-3  OpenAILLMClient 实现: 保留 tool_calls + tool_call_id，不再压平为文本
  A-4  MockLLMClient / 其他实现: 返回 ChatResponse
  A-5  LLMAgentKernel.think: 直接消费 ChatResponse.tool_calls，删除正则解析路径
  A-6  ThinkResult 增加 tool_call_id 字段
  A-7  多轮历史按 OpenAI 协议重建 (assistant.tool_calls + 配对 role=tool)

阶段 B（P1，附带修复）— 移除双重教导与降级塌陷
  B-1  _SERIAL_THINK_PROMPT 拆分: 文本版与 function-calling 版分离
  B-2  _FallbackKernel 移除 (与 LLMAgentKernel 90% 重复)
  B-3  _parse_results / _TOOL_SPLIT_RE / _ARGS_GREEDY_RE 移除主路径依赖

阶段 C（P1-P2，Planner 协议缝隙）— 必须在阶段 A/B 通过验收后执行
  C-1  Planner 工具过滤死分支 (_filter_tools_by_intent + ALWAYS_INCLUDE)
  C-2  Planner revise 丢失原始 intent (planner.py:258)
  C-3  StepResult dict-compat (r.get("status"))
  C-4  build_dag_status_text 不展示 done step output keys (E 缺口)
  C-5  System prompt PLAN 示例引用不存在的 browser_search
  C-6  planner.py:228 裸 print 调试残留

阶段 D — 测试与校验
  D-1  更新 test_kernel.py / test_fallback_kernel.py fixture
  D-2  新增多轮历史协议配对测试
  D-3  新增 json 解析失败可观测性测试
  D-4  运行全套测试 + ruff / mypy
```

## 三、阶段 A 详细规格

### A-1 新增数据模型

在 `harness/core/llm_client.py` 顶部新增（Pydantic v2）：

```python
from pydantic import BaseModel, Field

class ToolCall(BaseModel):
    id: str                      # provider 分配的 tool_call_id，禁止丢弃
    name: str
    arguments: dict              # 已 json.loads；解析失败时填 {"_parse_error": raw}

class ChatResponse(BaseModel):
    content: str = ""            # 普通文本（可为空）
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = ""
    raw: dict | None = None      # 诊断用原始响应；Provider 返回的 choice dict
```

### A-2 修改 `LLMClient.chat` 抽象签名

`harness/core/llm_client.py:27-34`：

当前:
```python
async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, ...) -> str:
    ...
```

改为:
```python
async def chat(self, messages: list[dict], *, tools: list[dict] | None = None, ...) -> ChatResponse:
    ...
```

所有子类同步改签名。

### A-3 `OpenAILLMClient.chat` 实现

`harness/core/llm_client.py:61-145` 重写。关键要点：

1. 当 `msg.get("tool_calls")` 存在时：
   - `ToolCall(id=tc["id"], name=fn["name"], arguments=parsed)`
   - **保留 `tc["id"]`**，当前代码（line 126）直接丢弃，这是 P0 bug。
   - `json.loads(raw_args)` 失败时：
     - `_logger.warning("[LLM] tool_call arguments json decode failed: id=%s name=%s raw=%.200s", tc_id, name, raw_args)`
     - `arguments = {"_parse_error": raw_args}`
     - **绝不静默吞掉，绝不 fallback 为原字符串让下游再解析一次**。
2. 返回 `ChatResponse(content=content_or_empty, tool_calls=[...], finish_reason=..., raw=choice)`。
3. **删除把 `tool_calls` 压平为 `f"TOOL: {name}\nARGS: {args_str}"` 文本行并 `return "\n".join(lines)` 的代码段（当前 line 121-144）**——这是结构化丢失的根因。

### A-4 `MockLLMClient` 与其他实现

- `MockLLMClient`：若原本返回预编排文本，改为返回 `ChatResponse(content=..., tool_calls=[ToolCall(id="mock-...", name=..., arguments=...)]`。
- 测试 fixture 中所有 `await client.chat(...)` 返回字符串的断言全部改为 `.content` / `.tool_calls` 访问。

### A-5 `LLMAgentKernel.think` 直接消费 `ChatResponse`

`harness/core/agent_kernel.py`，当前 line 212：

```python
response = await self.client.chat(messages, tools=schemas)
results = _parse_results(response)   # 删除
```

改为：

```python
resp = await self.client.chat(messages, tools=schemas)
results = [
    ThinkResult(
        thought=resp.content or "",
        tool_name=tc.name,
        tool_input=tc.arguments,
        tool_call_id=tc.id,
    )
    for tc in resp.tool_calls
]
if not results and resp.content:
    # 仅当模型未发起 tool_calls 而是纯文本 ANSWER/<STOP> 时进入
    ...
```

删除对 `_parse_results` / `_TOOL_SPLIT_RE.split` / `_ARGS_GREEDY_RE.search` 的依赖（这些正则本身可保留在文件底部供 fallback 文本路径用，但主路径不再调用）。

### A-6 `ThinkResult` 增加字段

`harness/core/agent_kernel.py` 中 `ThinkResult` dataclass：

```python
@dataclass
class ThinkResult:
    thought: str
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_input_str: str | None = None
    tool_call_id: str | None = None   # 新增
```

下游所有消费者（`scheduler/loop.py`、`fold.py` 等）如有用到 `ThinkResult` 的地方，在构造 `ToolCalledPayload` 时把 `tool_call_id` 透传——**检查 `harness/core/scheduler/loop.py` 与 `harness/core/fold.py`，Tool Layer 已支持，不要漏**。

### A-7 多轮历史按 OpenAI 协议重建

`harness/core/agent_kernel.py:198-204` 当前实现违规。改为：

每轮 think-act-observe 在 messages 数组中的正确形态是：

```
{
  "role": "assistant",
  "content": "THOUGHT: ...",            # 可空
  "tool_calls": [
    {"id": "call_xxx", "type": "function",
     "function": {"name": "tool_a", "arguments": "{...}"}}
  ]
}
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "completed: <output or error>"
}
```

注意三点铁律：
1. 一条 assistant message 中的多个 `tool_calls` 必须与紧随其后的多个 `role:tool` 消息**按 tool_call_id 一一配对**，provider 严格校验时会拒绝任何缺失或多余的 `tool` 消息。
2. `tool_calls` 与 `content` 可以共存于同一条 assistant 消息，但**不能把多个 tool_call 拆成多条 assistant 消息**。
3. timeline 中一次 think 的 thought + 该次发起的所有 tool_calls **必须打包成一条 assistant message**，不能 thought 一条、tool_calls 又一条。

实现要点：遍历 timeline 时按 "think 边界" 分组——遇到 `kind == "thought"` 时 flush 上一个 assistant + 其对应的所有 tool 结果，再起新的 assistant message。

## 四、阶段 B 详细规格

### B-1 `_SERIAL_THINK_PROMPT` 拆分

`harness/core/system_prompt.py:116-143`。

新增两套 prompt：
- `AgentPhase.SERIAL_THINK_FN`：走 function-calling 路径时用。**移除 `TOOL:/ARGS:` 文本格式说明**，只保留 A/B/C 决策骨架 + "用提供的 tools 调用工具" 一句话。
- `AgentPhase.SERIAL_THINK_TEXT`：保留原文本格式，仅 fallback 用。

`LLMAgentKernel` 根据 `tools is not None` 选择 FN 版本。

### B-2 移除 `_FallbackKernel`

`harness/core/scheduler/fallback_kernel.py` 整文件删除。

理由：
- 与 `LLMAgentKernel` 90% 重复（system_prompt、timeline、summary 生成完全一致）。
- 唯一差异是不传 `tools=schemas`。结构化通道打通后没有理由关闭 function calling。
- 调用方（搜索 `_FallbackKernel` 的使用点）改为直接用 `LLMAgentKernel`。

`harness/core/agent_kernel.py` 底部的文本路径解析函数（`_parse_results`、`_TOOL_SPLIT_RE`、`_ARGS_GREEDY_RE`）若移除 `_FallbackKernel` 后无引用，一并删除。

### B-3 主路径正则依赖移除

随 A-5 完成后，确认 `_ARGS_GREEDY_RE`、`_TOOL_SPLIT_RE`、`_THOUGHT_RE` 在主路径无任何调用。若 fallback 已删除则直接删掉这些正则与 `_parse_segment` / `_parse_results`。

## 五、阶段 C 详细规格（Planner 协议缝隙）

### C-1 Planner 工具过滤死分支

`harness/core/planner.py:427-439` `_filter_tools_by_intent`：

当前 `ALWAYS_INCLUDE = {"file_op"}` 永不为空，导致 `return relevant if relevant else tool_defs` 的 fallback 永不触发——intent 未匹配的工具被静默丢弃。

修复方向（二选一，**选前请与用户确认当前业务语义**）：

- 方案 A：`ALWAYS_INCLUDE` 应改为 intent 命中时的补充集合，而不是无条件并集。即只有当某工具自身的 `intent` 匹配时才把它加入，`file_op` 不应特殊待遇。
- 方案 B：保留 `ALWAYS_INCLUDE` 但当 LLM 判定无 intent 匹配时（`relevant` 为空），直接 `return tool_defs` 全量回退——让 LLM 自己在 `tool_defs` 全量里选，而不是被截断。

推荐方案 B，最小改动且符合 "Agent 决策权归 Agent" 原则。但**必须在修复前向用户确认**。

### C-2 Planner revise 丢失原始 intent

`harness/core/planner.py:258`：

revise 只传了 `plan.intent[:200]`，原始用户 intent 在多轮 revise 后被截断/丢失。

修复：在 `Plan` 对象或 Planner 实例上持久化原始 `user_intent`，每轮 revise 都带上完整原文。

### C-3 `StepResult` dict-compat

`harness/core/planner.py:274-276` 用 `r.get("status") in ("completed", "idempotency_hit")`：

- `StepResult` 已有 `.get` 后向兼容方法（`dag_types.py:37-51`）。
- 但 `"idempotency_hit"` 不在 `StepStatus` 枚举中，是死分支。
- `SOFT_ERROR` 未被计入 completed。

修复：改用 `r.status == StepStatus.COMPLETED` 或显式枚举集合判断，移除 `"idempotency_hit"` 死分支，评估 `SOFT_ERROR` 是否应计入（向用户确认）。

### C-4 `build_dag_status_text` 不展示 done step output keys

`harness/core/dag_executor.py:344-389`：done 步骤只显示状态不显示产出的 output_keys，导致 LLM 在 revise 时无法可靠写 `$s1.x` 引用。

修复：done 步骤的状态文本追加 `→ outputs: [key1, key2]`，从 `StepResult.outputs` 或 `StepResult.output` 字段提取。

### C-5 PLAN 示例引用不存在的工具

`harness/core/system_prompt.py:61-67`：`_PLAN_PROMPT` 示例里用了 `browser_search`，但 `BROWSER_DEF` 的 action 枚举只有 `navigate/click/type/extract/screenshot`，没有 `search`。

修复：改用实际存在的工具名 + action 组合，或改为 `browser.navigate` + `http_request` 等真实可执行示例。**修复后核对与实际 `ToolRegistry.list_tool_defs()` 输出一致**。

### C-6 裸 print 调试残留

`harness/core/planner.py:228`：`print(self.registry.list_tool_defs())` 直接删除。

## 六、阶段 D 测试与校验

### D-1 更新现有测试

- `test_kernel.py` 中所有 `await client.chat(...)` 返回字符串的断言改为 `.content` / `.tool_calls` 访问。
- `test_fallback_kernel.py`：若 `_FallbackKernel` 已删除则整个文件删除或改测 `LLMAgentKernel` 在无 `tools=` 时的降级行为。

### D-2 新增测试用例

1. **tool_call_id 透传测试**：Mock 注入 `ChatResponse.tool_calls=[ToolCall(id="abc", name="file_op", arguments={...})]`，断言 `ThinkResult.tool_call_id == "abc"`，并断言后续 `ToolCalledPayload.tool_call_id == "abc"`。
2. **多轮历史协议配对测试**：构造含 2 轮工具调用的 `RunState`，送入 `LLMAgentKernel.think`，断言组装的 `messages` 内含：
   - 一条 `assistant` 消息带 `tool_calls` 数组（2 个元素）
   - 紧随两条 `role=tool` 消息，`tool_call_id` 与 assistant 的两个 id 一一对应
   - 不存在孤立的 `tool` 消息或孤立的 `assistant.tool_calls`
3. **json 解析失败可观测性测试**：Mock `OpenAILLMClient` 返回 `arguments="not a json"`，断言：
   - `ToolCall.arguments == {"_parse_error": "not a json"}`
   - `_logger.warning` 被调用
   - 不抛异常，`ThinkResult` 正常产出

### D-3 校验命令

完成后必须运行（若命令不存在请向用户确认）：

```bash
# 类型检查
mypy harness/core/llm_client.py harness/core/agent_kernel.py harness/core/scheduler/
# Lint
ruff check harness/
# 测试
pytest tests/ -x -v
```

若 lint/typecheck 报错，**必须修复到全部通过**，禁止 `# type: ignore` 填充。

## 七、执行纪律

1. **按阶段顺序执行**，A 未通过禁动 B，B 未通过禁动 C。
2. **每阶段完成后停下，列出改动文件清单与对应的验收点，等待用户确认后再进入下一阶段**。不要一口气改完所有阶段。
3. **修复前先读 `AGENTS.md`§3.4 的审查三步**——若发现代码现状与本提示词描述不符，**先报告差异再动**，禁止按自己的理解强行修改。
4. **禁止跳过测试**。阶段 D 的测试必须实写，不得只写 `pass`。
5. **禁止提交 git**——除非用户明确说 "commit"。
6. 完成后输出一份「改动摘要」：文件、行号、改动类型（修改/新增/删除）、对应缺口编号（T-1~T-8 / C-1~C-6）。

---

## 附：三份 review 文档位置（执行前必读）

- `D:\Project\JAgent\JAgent-docs\Reviews\planner_tool_filtering_review_20260722.md`
- `D:\Project\JAgent\JAgent-docs\Reviews\planner_protocol_gaps_review_20260722.md`
- `D:\Project\JAgent\JAgent-docs\Reviews\structured_tool_calls_review_20260722.md`

读完再动手。不确定就问，禁止猜。

---

## 八、用户审阅差异报告后已拍板决策（2026-07-23，本执行依据）

执行审查三步之 Step 1 报告差异后，用户已确认下列决策。本执行步骤按此为准。

### 8.1 ThinkResult 字段（覆盖 A-6 spec）

- **保留现状字段**：`thought / tool_name / tool_input / token_count / direct_answer`（`token_count` / `direct_answer` 被现有测试和 ANSWER/<STOP> 路径依赖，不可删）
- **新增**：`tool_call_id: str | None = None`
- **不引入** spec 示意但无消费者的 `tool_input_str`

### 8.2 L3 接缝单点改动（用户允许）

`harness/core/scheduler/base.py:476` 调 `executor.execute(..., override_tool_call_id=think_result.tool_call_id)` 是 A-6 透传断言能成立的接缝。仅此一处 L3 改动，不扩大范围。

### 8.3 C-1 始终全量喂工具（覆盖原 spec 二选一）

- 直接删除 `harness/core/planner.py:441-444` 入口过滤分支
- `_build_tool_descriptions` 始终返回 `registry.list_tool_defs()` 全部工具的 **name + description + 完整 input_schema**（不是仅名字）
- 删除 `_filter_tools_by_intent` 与 `_extract_tool_keywords`（无其它引用）及 `ALWAYS_INCLUDE` 硬编码
- 规模阈值（业界 ~50 工具）暂不加；当前 4 个一阶工具 + 9 个 MCP 子工具完全无压力

### 8.4 C-3 范围收紧（仅删死代码）

- **本次仅删** `"idempotency_hit"` 死分支字面量（grep 全项目无写入路径，纯死代码）
- `SOFT_ERROR` 是否计入 `completed_step_ids` **暂缓**——这是「任务完成 vs 工具完成」语义问题，需用户研究业界后定方案
- 完整设计、关键字、暂缓范围详见 `ARCHITECTURE_v2.1.md §3.7`（缺口 S1）
- 暂缓范围对应文件：`planner.py:274-276` / `dag_types.py:13-18` / `dag_executor.py:282-284` / `dag_types.py:37-51`

### 8.5 C-5 PLAN 示例用抽象占位（覆盖原 spec 真实工具）

- **不绑定具体工具名**，PLAN 三个示例改为抽象占位（如 `A→B→C`，`s1(X) → s2(Y), depends_on=["s1"]`）
- `_build_step_schema_text` 内的常量示例同步改抽象
- 理由：示例优先表达 plan 结构（independent/dependent/dataflow），不应误导 LLM 用固定常量

### 8.6 C-2 双槽 revise（一并纳入本次）

- 在 `harness/models/plan.py:Plan` 模型上新增 `user_intent: str = ""` 持久化原始用户意图
- `_REVISE_PROMPT` 拆 `Original User Intent / Plan Intent` 双槽
- `Planner.plan` 首次生成时记录 `user_intent`；`Planner.revise` 透传原文，不再随多轮被截断/丢失
- 调用方（`scheduler/plan.py`）传入原始 intent 即可

### 8.7 阶段顺序与执行明示

- 严格按 A → B → C → D 顺序，阶段间停下报告改动清单 + 验收点等用户确认
- D-3 校验命令：`mypy harness/core/llm_client.py harness/core/agent_kernel.py harness/core/scheduler/`；`ruff check harness/`；`pytest tests/ -x -v`
- 禁 `# type: ignore` 填充；禁 silent pass；禁任何与本次任务无关代码