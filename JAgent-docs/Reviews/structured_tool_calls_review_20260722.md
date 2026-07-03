# Review — 结构化 tool_calls 输出链路

> **日期**: 2026-07-22
> **范围**: `harness/core/agent_kernel.py`、`harness/core/llm_client.py`、`harness/core/scheduler/fallback_kernel.py`、`harness/core/system_prompt.py`
> **关注点**: LLM 与 Agent Kernel 之间 `tool_calls` 是否真正以结构化方式流转（**用户明确表示不在乎降级，只在乎结构化输出**）
> **基线**: 上一轮 review 中标记为 🟡 P2 的「串行 Fallback 路径在多轮场景下的问题」
> **本次结论**: 此前降级为 P2 是错判，根因不在降级路径，而在 **`LLMClient.chat` 接口签名本身把结构化输出压平为文本**——主路径同样受害。应升级为 **P0**。

---

## 0. 关键判断

**当前主路径（`LLMAgentKernel`）并非「OpenAI Function Calling + 结构化 tool_calls」**——尽管 API 请求侧已经把 `tools=schemas` 传给 provider，但响应侧把结构化 `tool_calls` **压回 `THOUGHT:/TOOL:/ARGS:` 文本格式**，再由 `_parse_results` 用正则解析回来。

这是一个 **结构化 → 文本 → 正则 → 结构化** 的有损往返：

```
OpenAI API → msg.tool_calls (struct) → LLMClient.chat 内部压平为 "TOOL:\nARGS: {...}"
            → 字符串返回 → _parse_results 正则解析 → ThinkResult
```

用户对「降级路径无结构化」的描述不够准确——**根本就没有任何一条路径保留结构化输出**。降级只是把同一套文本协议变成更脆的一支。

---

## 1. 证据链

### 1.1 `LLMAgentKernel.think` 已经正确传 `tools=schemas`

`harness/core/agent_kernel.py:212`：

```python
response = await self.client.chat(messages, tools=schemas)
```

`build_tool_schemas`（`system_prompt.py:179-191`）也按 OpenAI `function-calling` 规范拼装：

```python
{"type": "function", "function": {"name": ..., "description": ..., "parameters": td.input_schema}}
```

请求侧合规。

### 1.2 `OpenAILLMClient.chat` 收到结构化 `tool_calls` 但立刻压平

`harness/core/llm_client.py:121-141`：

```python
if msg.get("tool_calls"):
    lines = []
    if content:
        lines.append(f"THOUGHT: {content}")
    tool_names = []
    for tc in msg["tool_calls"]:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")
        try:
            parsed = json.loads(raw_args)
            args_str = json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError:
            args_str = raw_args
        lines.append(f"TOOL: {name}")
        lines.append(f"ARGS: {args_str}")
        tool_names.append(name)
    ...
    return "\n".join(lines)
```

**问题 1**：`tool_calls` 被序列化为 `"TOOL: name\nARGS: {...}"` 文本行。
**问题 2**：`tc.get("id")`（即 provider 分配的 `tool_call_id`）**直接丢弃**——全代码 grep 无任何地方回填该 id——而 OpenAI 协议要求后续 `tool` 角色 message 必须以 `tool_call_id` 关联结果。
**问题 3**：当 `json.loads(raw_args)` 失败时，**fallback 为 `args_str = raw_args`**（原字符串），后续 `_parse_results` 再解析仍会失败，**但这里没有 logging.warn，没有事件**——错误被静默吞掉。

### 1.3 `LLMAgentKernel.think` 用正则再次反向解析

`harness/core/agent_kernel.py:213`：

```python
response = await self.client.chat(messages, tools=schemas)
results = _parse_results(response)
```

`_parse_results` → `_TOOL_SPLIT_RE.split(response)` → `_parse_segment` 用 `_ARGS_GREEDY_RE.search(seg)` 抓 `\{.*\}` 再 `json.loads`。

**问题 4（用户关注的"tool_calls 结构化输出"真问题）**：API 给的是结构化对象，被压成文本，再用贪婪正则解析回去。每多一步转换就多一层失败模式：
- 工具参数里含 `}` 字符的字面量：贪婪正则吃到最后一个 `}`，匹配过头 → 若整段内还有 `ARGS:` 出现就吞到更后面的 `}`；幸好 `_TOOL_SPLIT_RE` 按 `\nTOOL:` 先切段，单段内贪婪通常是平衡的最外层 `}`，`json.loads` 一般能过。
- 但若工具参数含有 **不平衡的 `}`**（如 `"snippet": "if (x) { foo() } else {"`），`json.loads` 失败，进入 `agent_kernel.py:46-48`：

  ```python
  except json.JSONDecodeError:
      pass
  return tool_name, {}
  ```

  **静默返回 `{}`**。Tool 用空 `input` 被调用，会在 Tool Layer 因 schema 校验失败或语义错误才报错——根因被埋在最底层，与 `AGENTS.md` §6.1「错误路径必须可观测」直接冲突。

### 1.4 多轮历史的拼装不遵循 OpenAI `tool_calls` 协议

`harness/core/agent_kernel.py:198-204`：

```python
for kind, item in timeline:
    if kind == "thought":
        choice = f" ({item.tool_choice})" if item.tool_choice else ""
        messages.append({"role": "assistant", "content": f"THOUGHT{choice}: {item.thought}"})
    else:
        content = f"Tool '{item.tool_name}' result ({item.status}): {item.output or item.error}"
        messages.append({"role": "user", "content": content})
```

**问题 5**：按 OpenAI 协议，模型先前发起的 `tool_calls` 必须以 `{"role":"assistant","tool_calls":[...],"content":...}` 回放，每个 `tool_call` 对应一条 `{"role":"tool","tool_call_id":...,"content":...}` 的结果消息。当前代码用 `assistant.content="THOUGHT:" + user.content="Tool 'X' result: ..."` 替代。

后果：
- 严格 provider（OpenAI 官方、Anthropic tool use）会因消息序列不符合规范直接 400 报错；Bailian/DeepSeek/通义目前容忍，但**契约不稳定**，provider 任何一次收紧校验就会断。
- 模型上下文中**没有 `tool_call_id` 关联**，多工具并行时模型无法判断哪个结果对应哪次调用，多轮强行重建链路时易混淆——这是你看到的「多轮场景下问题更严重」的根因之一。
- 一旦上次某个 tool_call 没有 `tool` 角色结果回填，下一次请求会被 provider 拒绝（部分 provider 强制 `tool_calls → tool` 必须配对）。

### 1.5 `_FallbackKernel` 干脆不开 `tools`

`harness/core/scheduler/fallback_kernel.py:78`：

```python
response = await self.client.chat(messages)   # 没有 tools=schemas
```

**问题 6**：降级路径根本没传 `tools=` 参数，模型只能从 system_prompt 的 `tool_list` 文本里 "自然语言" 自己拼出 `THOUGHT:/TOOL:/ARGS:` 格式。即模型支持 function calling 的能力被强行关闭，**强制要求模型按 SERIAL_THINK prompt 的文本协议输出**。

降级路径的本质是「换 Kernel，不换协议格式」——而协议格式本身是文本的，所以降级只是放大了主路径就已有的问题，不是新增问题。

### 1.6 `SERIAL_THINK` prompt 与 `tools=schemas` 双重教导

`harness/core/system_prompt.py:116-143` `_SERIAL_THINK_PROMPT` 同时教：

```
## Available Tools
{tool_list}            ← 文本列表（自然语言）

## Instructions
...
**Option A — Call a tool:**
THOUGHT: <your reasoning>
TOOL: <tool_name>
ARGS: <JSON arguments>
```

**问题 7**：主路径 `LLMAgentKernel` 已经传 `tools=schemas`，但 prompt 仍教模型用文本写 `TOOL:/ARGS:`。这是「双重教导」——OpenAI Function Calling 文档明确建议**当 `tools` 已传时 prompt 不要再教模型自定义格式**，否则模型可能：
- 一边返回 `tool_calls`，一边在 `content` 里也写 `TOOL:/ARGS:` 字样（双重输出）；
- 当上下文累积了过去的文本 `TOOL:` 行作为 `assistant` 消息时，模型倾向模仿文本格式，干脆不用 `tool_calls` 字段——`tools=schemas` 白传。

---

## 2. 问题清单与定级

| 编号 | 描述 | 根因层级 | 优先级 |
|---|---|---|---|
| **T-1** | `LLMClient.chat` 返回 `str`，把 `tool_calls` 压平为 `THOUGHT:/TOOL:/ARGS:` 文本再返回 | 接口契约 | **P0** |
| **T-2** | `tool_call_id` 在 `OpenAILLMClient` 中丢失 | 契约缺失 | **P0** |
| **T-3** | `LLMAgentKernel.think` 用正则反向解析文本回结构化 | 实现倒置 | **P0**（与 T-1 同修） |
| **T-4** | 多轮历史用 `assistant.content="THOUGHT..."` / `user.content="Tool '...' result:"` 替代 `assistant.tool_calls` + `role=tool` 序列 | 协议违和 | **P0** |
| **T-5** | `_parse_segment` JSON 解析失败静默 `return {}`，无事件、无日志升级 | 可观测性 | **P1** |
| **T-6** | `_FallbackKernel` 不传 `tools=schemas`，强制模型走文本协议 | 设计塌陷 | **P1**（与降级完全移除一并修） |
| **T-7** | `_SERIAL_THINK_PROMPT` 与 `tools=schemas` 同时教模型工具调用语法 | prompt 违约 | **P1** |
| **T-8** | `_ARGS_GREEDY_RE` 贪婪匹配在含 `}` 字符的工具输出场景错误率高 | 解析脆弱 | P2（随 T-3 移除） |

**重新对齐定级**：原 P2 严重低估——`OpenAILLMClient` 写回文本后已经被所有路径消费，不是「仅 fallback 脆弱」，而是**主路径也脆弱**，且其中 T-2 / T-4 在 OpenAI 官方、Anthropic 等严格 provider 下随时可能断连。

---

## 3. 与上轮 review 描述的纠错

原文：

> "无 OpenAI tools API 的结构化 tool_calls，靠正则解析 LLM 输出"

不准确。`LLMAgentKernel` 实际**传了** `tools=schemas`（`agent_kernel.py:212`）；`OpenAILLMClient.chat` 也接收了 `msg.tool_calls`（`llm_client.py:121`）。问题不是「没有结构化 input/output」，而是：

> **结构化输出在 `LLMClient` 接口边界被强行降回文本**，再由 `LLMAgentKernel` 用正则解析回来。`tool_call_id` 全程丢失。多轮历史按文本拼装，违反 OpenAI `assistant.tool_calls → role:tool` 协议。

这是一个**接口设计层面**的问题，单点修复 `OpenAILLMClient` 不够——必须升级 `LLMClient.chat` 签名本身，让结构化 `tool_calls` 作为一等数据流回 Kernel。

---

## 4. 修复建议

> 状态（2026-07-23）：T-1 ~ T-7、4.1 ~ 4.5 全部纳入本次阶段 A/B 执行；ThinkResult 字段以 `fix_prompt_for_ai.md §8.1` 为准（保留 token_count/direct_answer，不引入 tool_input_str）。

### P0 — 主修复（接口契约_alone）

**4.1 `LLMClient.chat` 返回结构化对象**

把签名从 `-> str` 改为 `-> ChatResponse`，新增：

```python
@dataclass
class ToolCall:
    id: str                 # provider 分配的 tool_call_id
    name: str
    arguments: dict         # 已 json.loads

@dataclass
class ChatResponse:
    content: str            # 普通文本（可为空）
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    raw: dict | None = None # 诊断用，落 Event Store 可观测性
```

`OpenAILLMClient`：
- `msg.tool_calls` → `ToolCall(id=tc["id"], name=..., arguments=json.loads(...))`；
- `json.loads` 失败时 **`_logger.warning` 并落入 `ToolCall.arguments={"_parse_error": raw_args}`**，绝不静默；
- 不再写 `f"TOOL: {name}\nARGS: ..."` 文本。

`MockLLMClient`：返回 `ChatResponse(content=...)` 或预编排的 `tool_calls`，覆盖测试。

**4.2 `LLMAgentKernel.think` 直接消费 `ChatResponse.tool_calls`**

```python
resp = await self.client.chat(messages, tools=schemas)
results = []
for tc in resp.tool_calls:
    results.append(ThinkResult(thought=resp.content or "", tool_name=tc.name,
                                tool_input=tc.arguments, tool_call_id=tc.id))
if not results:
    # 文本路径——只有模型显式 ANSWER/<STOP> 时进入
    ...
```

删除对 `_parse_results` / `_TOOL_SPLIT_RE` / `_ARGS_GREEDY_RE` 等正则的依赖（移到 fallback 专用工具，仅文本路径用）。

**ThinkResult 增加 `tool_call_id` 字段**——下一轮重建多轮历史时必须携带，确保 `assistant.tool_calls` + `role:tool` 回填对接。

**4.3 多轮历史按 OpenAI 协议重建**

`LLMAgentKernel.think` 历史 timeline 部分（`agent_kernel.py:198-204`）改为：

```python
# 上一轮 assistant 的 thought + tool_calls 必须同条 assistant message
prev_assistant_msgs = []  # 暂存 1 条 assistant message 待 append
for kind, item in timeline:
    if kind == "thought":
        # flush 完成上一条 assistant
        if prev_assistant_msgs:
            messages.append(prev_assistant_msgs); prev_assistant_msgs = None
        assistant_msg = {"role": "assistant", "content": f"THOUGHT: {item.thought}"}
        if item.tool_call_ids:   # 新增字段
            assistant_msg["tool_calls"] = [
                {"id": tcid, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                for tcid, name, args in item.tool_calls_info
            ]
        messages.append(assistant_msg)
    else:  # result
        messages.append({"role": "tool", "tool_call_id": item.tool_call_id,
                         "content": f"{item.status}: {item.output or item.error}"})
```

注意：run 多轮中 `assistant.tool_calls` 与紧随其后的多个 `role:tool` 消息**必须配对出现**——provider 严格校验时会拒绝任何 `tool_call_id` 未匹配的 `tool` 消息，反之亦然。

### P1 — 附带修复

**4.4 `_FallbackKernel` 处理**

按用户的取舍："降级不在乎"——建议直接**移除 `_FallbackKernel`**。原因：
- 当前与 `LLMAgentKernel` 90% 重复（system_prompt、timeline、summary 生成完全一致）。
- 唯一差异是不传 `tools=schemas`。在结构化通道打通后，没有理由对模型关闭 function calling。
- 若确有「假降级用于调试」的需求，应通过 `MockLLMClient` 直接预编排结构化 `ChatResponse.tool_calls`，而不是把 LLM 逼回文本协议。

**4.5 `_SERIAL_THINK_PROMPT` 拆分**

- 新增 `AgentPhase.SERIAL_THINK_FN`：当走 function-calling 路径时使用，**移除 `TOOL:/ARGS:` 文本格式说明**，只保留 "Choose A/B/C" 决策骨架。
- 保留 `AgentPhase.SERIAL_THINK_TEXT` 仅 fallback 文本路径用。

避免双重教导。

**4.6 `_ARGS_GREEDY_RE` 解析失败必须可观测**

随 T-3 移除主路径，但 fallback 文本路径仍依赖它。`fallback_kernel._parse_segment`（与 `agent_kernel._parse_segment` 重复）失败分支必须写一个 `_log.warning("[parse] ARGS JSON decode failed for tool=%s raw=%s", tool_name, raw[:200])` —— 至少让 lost-args 不再 silent。

### P2 — 配套

**4.7 测试更新**

`test_fallback_kernel.py`、`test_kernel.py:209-253` 中现有的 `TOOL:/ARGS:` 文本 fixture 用例：
- 归入 fallback 专用测试集（若保留 fallback 路径）。
- 新增主路径用例：`LLMAgentKernel.think` 返回的 `ThinkResult.tool_call_id` 必须等于 Mock 注入的 `ChatResponse.tool_calls[*].id`，多工具时全部保留。
- 新增多轮历史用例：构造一个含 2 轮工具调用的 `RunState`，断言下一次 `messages` 内含 `assistant.tool_calls` 数组与对应 `{role:"tool"}` 消息配对。

---

## 5. 架构层面评注（与 `AGENTS.md` 对齐）

- **L4 受信边界**: `LLMClient` 与 `LLMAgentKernel` 之间的契约应是 OpenAI Function Calling native 结构（`tool_calls` 一等公民），不应在 L4 内部为了"复用 `_parse_results`"而压平为文本。当前实现违反 §2.2「决策权归 Agent，强制权归系统」——结构化 `tool_calls` 是 Agent 决策的载体，把它压成文本再让非受信的正则还原，等于在 Kernel 边界引入一层不可观测的"决策有损压缩"。
- **可观测性 §6.1**: `tool_call_id` 丢失、json.loads 失败 swallow，意味着出错时事件流无法回放完整 LLM ↔ Tool 对话，违反 §5.1 端到端测试"事件链完整性"。建议落入 `RawLLMResponse`-like 事件（与上一份 review K 缺口一并处理）。
- **前后端契约 §4.1**: `ChatResponse` 一旦确立，应同步以 Pydantic v2 模型固化，OpenAPI 生成对应 TypeScript 类型，前端可观测性面板能直接看 provider 原始 `tool_calls` 而非人类改写的文本。

---

## 6. 对照评审检查点

- [x] Root Cause 不是「fallback 用文本」，而是 `LLMClient.chat` 返回字符串
- [x] 主路径与 fallback 同样受害，应升级 P0
- [x] 列出 T-1 ~ T-8 缺口与优先级
- [x] 指出原 review 不准确处（§3）
- [x] 给出可执行修复路径（§4），强调接口契约层先修，fallback 拆解次之
- [x] 对齐 §2.2 受信边界、§6.1 异常可观测、§4.1 前后端统一来源

---

## 附录：关键代码位置

| 文件 | 行号 | 关键问题 |
|---|---|---|
| `harness/core/llm_client.py` | 27-34 | `LLMClient.chat` 抽象签名返回 `str`，结构丢失根因 |
| `harness/core/llm_client.py` | 121-141 | `OpenAILLMClient` 把 `tool_calls` 压平为 `TOOL:/ARGS:` 文本，丢弃 `id` |
| `harness/core/llm_client.py` | 133-134 | `json.loads(raw_args)` 失败 fallback 为原字符串，无 warning |
| `harness/core/agent_kernel.py` | 19-23 | 三个错误倾向的正则 `_THOUGHT_RE` / `_TOOL_SPLIT_RE` / `_ARGS_GREEDY_RE` |
| `harness/core/agent_kernel.py` | 42-48 | `_parse_segment` json 失败静默 `return {}` |
| `harness/core/agent_kernel.py` | 198-204 | 多轮历史用纯文本格式回放，违反 OpenAI `assistant.tool_calls + role:tool` 协议 |
| `harness/core/agent_kernel.py` | 212 | 正确传 `tools=schemas`，但响应被压平 |
| `harness/core/scheduler/fallback_kernel.py` | 78 | 不传 `tools=`，强制模型文本协议 |
| `harness/core/system_prompt.py` | 116-143 | `_SERIAL_THINK_PROMPT` 教文本 `TOOL:/ARGS:` 与 function calling 双重教导 |
| `harness/core/scheduler/loop.py` | 119-121 | `tool_choice` / `tool_calls` 字段已存在于 `AgentThoughtPayload`——基础设施已为结构化预留 |
| `harness/models/events.py` | 73-75, 79 | `ToolCalledPayload` 已有 `tool_call_id` / `tool_calls` 字段——下游已就位，只差 Kernel 侧回填 |