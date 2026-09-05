# Planner Tool Filtering Review

> **日期**: 2026-07-22
> **分支**: review/alignment-check
> **范围**: Planner 模块与 Tool Registry / MCP Tool Discovery 的整体逻辑
> **定位**: Root Cause 分析 + Bug / 设计缺陷切分 + 修改优先级
> **约定**: 本文不修改代码，仅作结论与建议

---

## 0. 问题现象

用户输入：

> 打开 Google，查询成都十五日天气。

Intent Classifier 判定 `needs_tools=True` 进入 Planner。

但 Planner Prompt 最终生成的 `Available Tools` 只有：

```
- file_op
```

LLM 输出：

```json
{ "intent": "...", "steps": [] }
```

随后框架直接进入 Answer Phase，Executor / MCP / Browser Tool 均未被调用。

但 MCP Server 启动日志正常：

```
Connected MCP server 'memory': 9 tools
MCP discovery: 9 tools available via mcp_call
```

问题发生在 Planner 构建 Prompt 之前。

---

## 1. Root Cause 定位

**结论**：故障主因在 `_filter_tools_by_intent`（`harness/core/planner.py:427-439`）。

### 证据链（按当前代码）

1. `harness/api/serve.py:135` Real LLM 模式注册的 Tool 仅 4 个：
   - `http_request`
   - `file_op`
   - `browser`
   - `mcp_call`

2. MCP 子工具（你日志中看到的 `memory / fetch / ...` 9 个）**并未**作为一阶 Tool 进入 Registry：
   - `harness/models/mcp_config.py:17` 默认 `auto_register_tools: False`。
   - `harness/tools/mcp_manager.py:112` `_register_tools` 受该开关控制。
   - `harness/api/app.py:77` MCP 子工具只是被拼接进 `mcp_call.description`，作为该包装器描述的一部分。

   即：Planner 面对的工具集始终只有 4 个一阶工具。

3. `harness/core/planner.py:441-444`：
   ```python
   tool_defs = self.registry.list_tool_defs()      # 4 个
   if intent and len(tool_defs) > 2:                # 4 > 2 命中
       tool_defs = self._filter_tools_by_intent(intent, tool_defs)
   ```

4. `_filter_tools_by_intent`（`harness/core/planner.py:427-439`）逻辑：
   - 遍历每个 `td`。
   - `file_op` 命中 `ALWAYS_INCLUDE = {"file_op"}` 直接 `relevant.append`。
   - 其余工具走 `_extract_tool_keywords` + `any(kw in intent_lower)`。
   - `return relevant if relevant else tool_defs` 的 fallback：

     由于 `file_op` 永远让 `relevant` 至少有 1 个元素，**`else` 分支永不触发**。

   最终：只要其他工具的关键词不命中 intent，Planner Prompt 就只剩 `file_op`。

这是 **P0 Bug**。

---

## 2. 次要但同样致命的问题

### Bug B（P0）— PLAN Prompt 示例引用了不存在的工具

`harness/core/system_prompt.py:61-67` Example 1 / 2 让 LLM 调用：

```
browser_search
```

Registry 中 `browser_search` 根本不存在。唯一相关的是 `browser`，且其 `action` 枚举只有
`navigate / click / type / extract / screenshot`，没有 `search`。

后果链：
1. 即使修好 Filter，把 `browser` 喂给 LLM；
2. LLM 受示例引导很可能输出 `"tool": "browser_search"`；
3. `PlanGuardrail.validate`（`planner.py:124`）以 `unknown tool 'browser_search'` 打回；
4. 重试 2 次后仍可能退回空 steps，最终进 Answer Phase。

属于**埋好的雷**，必须与 Root Cause 同步修复。

---

### Bug C（P0 级架构异味）— 硬编码 `ALWAYS_INCLUDE = {"file_op"}`

`harness/core/planner.py:430`。

Planner 是非受信组件，应与具体工具名解耦。此处硬编码具体工具名，等于让 Planner 隐式「知道」上层装配细节，违反受信边界（参考 `AGENTS.md` §10 中「禁止前后端各自维护独立的数据结构定义」的精神延伸）。

---

### Bug D（P2）— 调试残留

`harness/core/planner.py:228`：
```python
print(self.registry.list_tool_defs())
```
裸 `print` 进了主流程，每次 `plan()` 都会打到 stdout。

---

## 3. 对五点怀疑的回应

### ① Tool Filtering — 确认 Root Cause

你的分析正确。Filter 的 fallback 被 `file_op` 永久屏蔽，是本次故障的真正根因。

### ② Keyword Matching 是否足够鲁棒 — 不够，且是结构性问题

当前匹配策略：`kw in intent_lower`（子串包含）。

致命点：

- **语言错配**：intent 为中文（"打开 Google，查询成都十五日天气"），而 tool `name / description / 参数描述` 全是英文。`browser` 的关键词 `navigate / click / url / web / extract / screenshot` 与「打开/google/查询/成都/天气/十五日」**零交集**。
- **同义词盲区**：用户说「打开」，工具描述「navigate」；用户说「查」，工具描述「search/fetch」。子串匹配对任何同义都无能为力。
- **`mcp_call` 命名错位**：`mcp_call` 的关键词是 `mcp / call / server / invoke / external / memory / fetch / store`，与用户意图（"打开 google"）不命中 → 被过滤。

### ③ MCP Tool 是否被过滤掉 — 是

但准确说法是：**注册到 Registry 的只有 `mcp_call`（包装器）**。9 个 MCP 子工具通过 `app.py:77` 拼到 `mcp_call.description` 里，但 Filter 看的是 `mcp_call` 的关键词，子工具中文意图（"google/天气"）没出现在 `mcp_call` 的英文 token 里，于是 `mcp_call` 被丢，9 个 MCP 挂毯全失败。

### ④ Planner 与 Registry 是否同步 — 是

`_build_tool_descriptions`（`planner.py:442`）调用 `self.registry.list_tool_defs()`，即拿 Registry 当前快照，没有其它缓存。问题确实发生在 Prompt Builder 阶段，不是 Registry 同步问题。

### ⑤ 是否应该做 Tool Filtering — 当前规模下弊大于利

从框架层面看，Planner 做硬过滤违反 Harness v2.1 的边界精神：

| 维度 | 关键字硬过滤 | 全量 + LLM |
|---|---|---|
| 决策权归属 | 系统（受信）替 Agent 决策 | LLM（非受信）决策，系统 guardrail 校验 |
| 可扩展性 | 加一个工具就要会想到改 keyword | 加工具即对 LLM 可见 |
| 跨语言 / 同义词 | 极脆 | LLM 原生多语、同义、隐式 RAG |
| 失败模式 | 静默丢工具，难复现 | LLM 显式拒绝，事件可观测 |

业界对照（Claude Code / Codex / Gemini CLI / OpenAI Responses API / LangGraph / AutoGen）：

- **Claude Code / Codex / Cursor / Gemini CLI**：把全部工具 schema 喂给 LLM，由模型自行选择。没有「planner 前 filter」。
- **OpenAI Responses API / Function Calling**：一次 `tools=[...]` 全量提交，分类 / 选择交给模型。
- **LangGraph**：ReAct 节点把 tools 全量暴露给模型；只有节点级 "路由" 概念，而非 keyword filter。
- **AutoGen**：同理，全量工具暴露。

只有在工具数 × schema 大到撑爆上下文才需要 retrieval。业界公认的演进路径：

```
全量 →（>N~50 条时）→ Embedding Retrieval → Reranker → Top-K → 交给 LLM
```

中间插一层 keyword substring 硬过滤，是「想优化但用错了工具」。

本项目当前规模：4 个一阶工具 + 9 个被 `mcp_call` 包装的 MCP 子工具。**应该直接全量**，不需要任何 filter。

附加建议（与 v2.1 受信边界契合）：`MCPConnectionConfig.auto_register_tools` 默认 `False` 值得商榷。若打开为 `True`，把每个 MCP 子工具注册成一阶 Tool，Planner 就能直接 `browser_search` / `fetch` / `memory_store`，而不是教 LLM 学习 `mcp_call(server_name, tool_name, arguments)` 间接寻址——这才是 LLM 最易出错的环节。

---

## 4. 对用户分析的纠错

整体方向 **100% 正确**，唯一需澄清：

> "我怀疑由于 `_extract_tool_keywords` 无法提取 MCP Tool 的有效关键词，最终导致 MCP Tool 全部被过滤。"

**此点不准确**。`_extract_tool_keywords` 提取逻辑本身没问题，能正确从 `mcp_call` 的 description（含附加的 9 个 MCP 子工具列表）抽到 `mcp / call / memory / fetch / store / ...` 等英文 token。

真正的失败原因是 **「抽到的英文 token 不出现在中文 intent 中」**，是语言 / 语义错配，而不是「抽不出关键词」。

链条更准确的描述：
- 提取 → ✓ 正常（能抽到 token）
- 匹配 → ✗ 失败（中文 intent vs 英文 token + 子串包含 = 必然失败）
- fallback → ✗ 被 `file_op` 抢占 evitability，永远不触发

即 *匹配策略* + *fallback 被屏蔽* 两件错叠加，而非 *提取* 错。

---

## 5. 修改建议（按优先级）

### P0 — 直接修掉触发本次故障的链路

1. **移除或短路 `_filter_tools_by_intent`**  ✅ **已采纳（本次执行）**
   - 决策见 `fix_prompt_for_ai.md §8.3`：直接删除 `planner.py:443-444` 入口过滤分支，`_build_tool_descriptions` 始终返回 `registry.list_tool_defs()` 全部工具的 name+description+完整 input_schema（非仅名字）。
   - 同时删除 `_filter_tools_by_intent` / `_extract_tool_keywords` / `ALWAYS_INCLUDE` 硬编码。
   - 暂不加 50 工具阈值；当前规模无压力。

2. **修 PLAN Prompt 的示例**  ✅ **已采纳（本次执行，按抽象占位版本）**
   - 决策见 `fix_prompt_for_ai.md §8.5`：**不绑定具体工具名**，PLAN 三个示例改为抽象占位（如 `A→B→C`，`s1(X) → s2(Y), depends_on=["s1"]`）。示例优先表达 plan 结构（independent/dependent/dataflow），避免误导 LLM 用固定常量。

3. **删除 `planner.py:228` 的裸 `print`**  ✅ **已采纳（本次执行，C-6）**，直接删除。

### P1 — 架构层面收口

4. **移除硬编码 `ALWAYS_INCLUDE={"file_op"}`**
   - 若 P0.1 已彻底删 filter，这个 set 自然消失。
   - 若保留 filter 仅作 "必选工具保险" 语义，应改为 `ToolDefinition` 上的 `always_included: bool` 字段，由注册方声明，而非 Planner 写死。

5. **审视 `MCPConnectionConfig.auto_register_tools` 默认值**
   - 当前 `False`，结果是 Planner 看不到 MCP 子工具的语义、只能看到 `mcp_call` 包装器。建议改成 `True`，让 MCP 子工具成为一阶 Tool，对齐 `AGENTS.md` §4.1 的「前后端工具契约统一来源」。
   - 注意：开启后要处理 MCP 子工具的 `idempotency_key_fields`、`guardrails`、`requires_confirmation` 默认值（目前 `_register_tools` 全置空 / 默认，需要按 §6.3 检查一遍）。

### P2 — 长期 RAG / 可观测性

6. **为未来多工具场景准备 Tool Retrieval 路径**
   - 维护一份 `tool_embedding`，PLAN 前用用户 intent 做 embedding 取 Top-K（≥5 个），再喂给 LLM。
   - 这条路径与「硬 keyword filter」是两回事，不要混淆。

7. **把 Filter 决策变成可观测事件**
   - 如果硬过滤将来仍保留，应在日志中记录 `dropped tools: [browser, mcp_call]  reason=no_keyword_match`。
   - Silent drop 是本次故障难排查的根因之一。`AGENTS.md` §6.1 要求受信组件异常不能泄漏，但 Planner 是非受信，应该把决策过程当成 LoggedStep 写入 Event Store，便于回放。

---

## 6. 评审检查点

- [x] 本次故障定位：P0 Bug 在 `_filter_tools_by_intent` + `ALWAYS_INCLUDE`
- [x] Bug / 设计缺陷切分：B / C / P1 Driver 项目均为缺陷，非纯实现细节
- [x] 已指出用户分析中需要修正的部分（§3.③、§4）
- [x] 给出优先级 P0 / P1 / P2
- [x] 已给架构层建议（全量 / Embedding Retrieval / `auto_register_tools`）

建议先按 P0 三条打补丁，再回头同步刷新 `JAgent-docs/` 中描述 Planner Tool 暴露策略的章节（按 `AGENTS.md` §3.4 三步流程：报告差异 → 修文档 → 实现）。

---

## 附：关键代码位置

| 文件 | 行号 | 说明 |
|---|---|---|
| `harness/core/planner.py` | 228 | 裸 `print` 调试残留 |
| `harness/core/planner.py` | 430 | `ALWAYS_INCLUDE = {"file_op"}` 硬编码 |
| `harness/core/planner.py` | 427-439 | `_filter_tools_by_intent` 故障主点 |
| `harness/core/planner.py` | 441-444 | `_build_tool_descriptions` 入口过滤分支 |
| `harness/core/system_prompt.py` | 61-67 | PLAN Prompt 示例引用不存在的 `browser_search` |
| `harness/api/serve.py` | 135 | Real LLM 模式仅注册 4 个一阶工具 |
| `harness/api/app.py` | 77 | MCP 子工具仅拼接到 `mcp_call.description` |
| `harness/models/mcp_config.py` | 17 | `auto_register_tools: False` 默认值 |
| `harness/tools/mcp_manager.py` | 112 | `_register_tools` 受 `auto_register_tools` 控制 |