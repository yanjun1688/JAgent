# JAGENT-2026-P0-06 用户直接看到内部执行状态 + 多轮会话上下文重复注入

> **严重等级**: P0（严重）  
> **状态**: ✅ 已修复 / 已补回归测试  
> **发现日期**: 2026-08-07  
> **报告人**: QA（黑盒测试工程师）  
> **关联日志**: `data/logs/harness.log`（2026-08-07 15:14–15:26）

---

## 1. 背景

在按黑盒用例进行真实 LLM 验证时，QA 发现两个互为表里的严重问题：

1. **用户直接看到内部执行状态**：Run 失败后，前端/聊天界面把形如 `DAG execution: 1/1 step(s) completed, 1 tool call(s). Steps not achieved: s1. Task terminated.` 的内部遥测字符串直接返回给用户。这是内部实现细节（DAG 步骤数、工具调用数、step ID、终止标记）的裸透传。

2. **多轮会话上下文重复注入 LLM**：在对话中发送 follow-up 消息时，系统把整段会话历史拼进 `intent`，并在 classify/plan/revise/answer 每个 LLM 调用节点重复发送，导致 prompt 膨胀、响应变慢、日志失真。

两个问题共同说明：当前**用户-facing 输出层**与**内部执行/调度层**没有清晰的边界，内部状态既直接暴露给用户，又被错误地喂给 LLM 作为"用户意图"。

---

## 2. 实际现象

### 2.1 用户看到内部遥测字符串

Run 失败后，聊天返回内容类似：

```
DAG execution: 1/1 step(s) completed, 1 tool call(s). Steps not achieved: s1. Task terminated.
```

这段内容包含：
- 内部 DAG 执行计数（`1/1 step(s) completed`）
- 内部工具调用计数（`1 tool call(s)`）
- 内部 step ID（`s1`）
- 内部终止标记（`Task terminated.`）

这不是自然语言的用户反馈，而是系统内部状态摘要的直接拼接。

### 2.2 同一对话触发多个 Run

| 时间 | Run ID | Conversation ID | 触发方式 | 日志中 intent |
|------|--------|-----------------|----------|---------------|
| 15:14:57 | `e24a7c32` | `conv_161d225bd544` | 新会话首条 | 正常用户意图 |
| 15:19:15 | `0892b3b8` | `conv_161d225bd544` | 同一对话 follow-up | `Previous conversation:` |
| 15:20:36 | `5732596a` | `conv_161d225bd544` | 同一对话 follow-up | `Previous conversation:` |
| 15:22:38 | `1e412396` | `conv_1cdd31fb224b` | 新会话 | 正常用户意图 |
| 15:23:14 | `55b9fd57` | `conv_3abc8e214ee3` | 新会话 | 正常用户意图 |
| 15:23:55 | `400e4e25` | `conv_81919f63e74a` | 新会话 | 正常用户意图 |
| 15:24:31 | `2b6aff6e` | `conv_3abc8e214ee3` | 同一对话 follow-up | `Previous conversation:` |

### 2.3 intent 被会话历史淹没

当 `conversation_id` 非空且会话已有消息时，`send_message` 与 `create_run` 都会执行：

```python
intent = f"Previous conversation:\n{ctx}\n\nCurrent request: {body.message}"
```

结果日志中 intent 被截断为：

```
15:24:32 [AGENT  ] [classify] intent=Previous conversation:
15:24:32 [AGENT  ] [lifecycle] Plan-Execute-Revise loop START for run=2b6aff6e intent=Previous conversation:
15:24:32 [AGENT  ] [plan] phase=plan len=10232 intent=Previous conversation:
```

虽然底层实际携带了 `Current request: ...`，但日志中完全不可见，给 QA 与运维排查带来误导。

### 2.4 每个 LLM 调用节点都触发一次完整上下文

以 run `2b6aff6e` 为例，单条 follow-up 消息触发了：

1. `classify` LLM 调用（15:24:31）
2. `plan` LLM 调用（15:24:32）
3. `revise` LLM 调用（15:25:10、15:25:25、15:25:51、15:26:06、15:26:22 等）
4. `answer` LLM 调用（无，因最终 RUN_FAILED）

每个阶段的 system prompt 都包含 `## Original User Intent Previous conversation:\n[user] ...\n[assistant] ...`，即同一段会话上下文在单次 Run 内被反复发送。

### 2.5 超长 Prompt 与慢响应直接相关

日志中多次出现 **10k+ chars** 的 prompt：

```
15:24:32 [AGENT  ] [LLM] Sending 1 messages (10406 chars) to qwen3.7-flash-2026-07-15
15:24:32 [AGENT  ] [LLM]   msg[0] role=system (10232 chars) You are a task planner...
```

同一次 Run 内，这些 10k+ 的 prompt 会在 plan 和每次 revise 中重复发送。日志中也出现多次超长响应时间：

| 时间 | 调用 | 响应耗时 |
|------|------|----------|
| 15:15:56 | revise | 49061 ms |
| 15:17:53 | answer | 51139 ms |
| 15:24:33 | revise | 56203 ms |
| 15:23:38 | revise | 50280 ms |

prompt 越长，LLM 的解码/注意力计算越慢；多次 revise 又把这段长 prompt 重复发出去，进一步放大了总耗时。

### 2.6 并发 Run 的 LLM 请求在日志中无法区分

黑盒测试时同时启动了多个 Run（`e24a7c32`、`1e412396`、`55b9fd57`、`400e4e25`、`2b6aff6e` 等），它们的 LLM 调用在日志中交错出现：

```
15:23:37 [AGENT  ] [LLM] Sending 1 messages (10932 chars) to qwen3.7-flash-2026-07-15
15:23:38 [AGENT  ] [LLM] Sending 2 messages (12654 chars) to qwen3.7-flash-2026-07-15
15:24:01 [AGENT  ] [LLM] Sending 1 messages (10291 chars) to qwen3.7-flash-2026-07-15
```

这些日志行**没有携带 run_id**，从 LLM 层无法直接判断：
- 这条请求属于哪个 Run；
- 哪个请求先发出、哪个后发出；
- 不同 Run 的 prompt 是否被串扰。

因此当多个会话同时请求 LLM 时，给人的直观感受是"所有请求混在一起、无法区分"。

---

## 3. 影响评估

| 维度 | 影响 |
|------|------|
| **用户体验（P0）** | 用户直接看到 `DAG execution: ... Steps not achieved: s1. Task terminated.` 等内部状态字符串，无法理解，严重破坏产品可信度。 |
| **信息泄露风险（P0）** | 内部 step ID、工具调用计数、DAG 结构等实现细节通过聊天界面暴露，可能被利用或误导用户。 |
| **成本** | 同一会话上下文在 classify/plan/revise/answer 中被重复发送，token 浪费随会话长度和 revise 轮次线性增长。 |
| **延迟** | prompt 因包含完整会话历史而膨胀，单次 LLM 调用耗时显著增加；revise 多次循环使长 prompt 被反复发送，是本次黑盒测试响应慢的重要原因之一。 |
| **可观测性** | 日志 `intent=Previous conversation:` 截断后无法区分真实请求；LLM 层日志缺少 run_id，多 Run 并发时无法把请求与 Run 关联。 |
| **正确性风险** | LLM 的注意力可能被冗长的 `Previous conversation` 稀释，导致对 `Current request` 的理解偏差；测试中观察到的"重复执行之前失败任务"可能与此有关。 |
| **架构约束** | AGENTS.md 要求受信边界清晰、事件溯源可还原。当前对话上下文在 API 层被写进 `RunStartedPayload.intent`，用户-facing 输出又直接取 `RunFailedPayload.result_summary`，两层边界均被破坏。 |

---

## 4. 可能的根因

### 4.1 会话上下文在错误的位置被注入

当前实现把 `_build_conversation_context()` 放在 `harness/api/routes.py` 的 `create_run` 与 `send_message` 中，把历史消息直接拼进 `RunStartedPayload.intent`。

- `create_run` 行 118：
  ```python
  intent = f"Previous conversation:\n{ctx}\n\nCurrent request: {body.intent}"
  ```
- `send_message` 行 476：
  ```python
  intent = f"Previous conversation:\n{ctx}\n\nCurrent request: {body.message}"
  ```

这导致 `RunStarted` 事件中的 `intent` 不再是"用户本次请求"，而是"会话摘要 + 当前请求"的混合体。

### 4.2 各 LLM 阶段共享同一个被污染的 intent

Scheduler 把 `RunStartedPayload.intent` 作为 `intent` 参数传给：

- `_classify_intent(run_id, intent)`
- `planner.plan(run_id, intent, ...)`
- `planner.revise(..., intent, ...)`
- `answer(..., intent, ...)`

因此，拼接后的长文本在每个阶段都被完整发送。

### 4.3 日志截断策略未区分关键字段

`harness/core/scheduler/plan.py` 中 classify 日志只取 `truncated[:80]`：

```python
_sched_ctrl.info("[classify] intent=%s needs_tools=%s raw=%s", truncated[:80], ...)
```

当 intent 以 "Previous conversation:" 开头时，80 字符刚好只包含前缀，导致当前请求不可见。

### 4.4 失败 Run 的用户-facing 消息直接取内部 result_summary

`harness/core/scheduler/base.py` 的 `_fail()` 方法把内部遥测拼接成 `result_summary`：

```python
summary = (
    f"DAG execution: {completed}/{tr} step(s) completed, {tr} tool call(s). "
    f"{error}. Task terminated."
)
```

然后 `RunFailedPayload(final_error=error, ..., result_summary=summary)` 被写入事件。

`harness/core/fold.py` 把 `result_summary` 折叠进 `state.summary`：

```python
case EventType.RUN_FAILED:
    state.last_error = p.final_error
    if p.result_summary:
        state.summary = p.result_summary
```

`harness/api/deps.py` 的 `_write_assistant_message()` 又把 `state.summary` 直接作为 assistant 消息内容写回 conversation：

```python
content = "Task completed."
if state.summary:
    content = state.summary
elif state.last_error:
    content = f"Error: {state.last_error}"
```

于是用户在前端看到的正是 `_fail()` 里拼的那串内部状态。

### 4.5 revise 阶段未对上下文做增量压缩

每次 revise 都把完整的 `Original User Intent` 重新塞进 system prompt，而不是仅发送"当前请求 + 本轮反馈"。随着 revise 轮次增加，prompt 重复内容越来越多。

### 4.6 LLM 客户端缺乏并发隔离与请求追踪

1. **LLM 日志无 run_id 标识**

   `harness/core/llm_client.py` 的日志只打印模型名、消息数、字符数，不打印 run_id：

   ```python
   _logger.info("[LLM] Sending %d messages (%.0f chars) to %s",
                len(messages), sum(len(str(m)) for m in messages), self.model)
   ```

   多个 Run 并发时，无法从 `[LLM]` 日志直接判断请求归属。

2. **每个 LLM 调用新建 httpx 客户端**

   ```python
   async with httpx.AsyncClient(timeout=120.0) as client:
       resp = await client.post(...)
   ```

   这导致每个请求都创建新的连接池，无法复用 TCP/HTTP2 连接。并发量稍大时：
   - 连接建立开销叠加；
   - 无法利用连接复用；
   - 给人的感觉像是"所有请求挤在一个通道里排队"。

3. **无并发控制或请求隔离机制**

   `OpenAILLMClient` 是单例共享给所有 Run，内部没有：
   - 连接池大小限制；
   - 并发请求数限制（semaphore）；
   - 请求级超时/重试策略；
   - 请求 ID / run_id 透传。

   多个 Run 的请求在 LLM 层看起来就是同一模型、同一客户端、同一连接模式发起的无差别流量。

---

## 5. 复现步骤

### 复现 1：用户看到内部状态

1. 启动服务器。
2. 发送一个必然失败的意图，例如：
   > 读取 data/不存在的文件 quarterly_report.csv 的内容，然后统计它有多少行
3. 等待 Run 失败。
4. 观察聊天返回：会出现 `DAG execution: ... Steps not achieved: ... Task terminated.`

### 复现 2：会话上下文重复注入

1. 创建新会话并发送首条消息（如"帮我读取 README.md..."）。
2. 在该会话中继续发送第二条、第三条消息（如"检查 example.com/nonexistent-page-xyz 是否返回 404"）。
3. 观察日志：
   - 每条 follow-up 都产生新的 `RunStarted`。
   - `[classify]`、`[plan]`、`[revise]` 日志中 `intent` 均显示为 `Previous conversation:`。
   - 每个 LLM 调用的 system prompt 都包含完整历史。

### 复现 3：并发 Run 的 LLM 请求不可区分

1. 同时打开两个浏览器标签/会话，分别发送不同意图。
2. 观察 `data/logs/harness.log` 中的 `[LLM]` 日志。
3. 可见多条 `[LLM] Sending ... to qwen3.7-flash-2026-07-15` 交错出现，但日志中**没有 run_id**，无法把请求与具体 Run 关联。

---

## 6. 预期行为

### 6.1 用户-facing 输出

| 场景 | 预期 |
|------|------|
| Run 成功 | 返回自然语言总结（已完成），不含内部 step ID、工具调用计数等。 |
| Run 失败 | 返回用户可理解的失败原因，例如"未能读取指定文件，任务已终止"，而不是 `DAG execution: ... Task terminated.`。 |
| 内部遥测 | `result_summary` 应仅用于日志、监控、debug UI，不应直接作为聊天消息。 |

### 6.2 事件溯源与 intent

| 阶段 | 预期 |
|------|------|
| **事件溯源** | `RunStartedPayload.intent` 应仅保存用户本次真实请求，不应将会话历史混入。 |
| **上下文管理** | 会话历史应作为 Agent Kernel 的上下文窗口管理的一部分，按需在调用 LLM 时组装，而不是在 API 层写死进 intent。 |
| **日志** | 关键日志应至少输出 `Current request:` 部分，或单独记录 `current_request` 字段，避免被截断成无意义的 `Previous conversation:`。 |
| **成本** | classify/plan/answer 等阶段不应重复发送相同的长上下文；revise 阶段可只发送本轮失败的摘要而非完整历史。 |

---

## 7. 建议修复方向

### 7.1 拆分用户-facing 总结与内部遥测

1. `RunFailedPayload` 保留 `final_error`（内部用）和 `result_summary`（日志/监控用）。
2. 新增独立的用户-facing 失败说明生成逻辑：
   - 优先由 answer 阶段 LLM 根据事件流生成自然语言失败说明；
   - 若 answer 阶段不可用，则由一个受控模板生成，例如 `"任务未能完成：{user_friendly_error}。"`，模板中不得包含 step ID、工具调用计数等内部字段。
3. `_write_assistant_message()` 不再直接使用 `state.summary`/`state.last_error`，而是使用专门的用户-facing 字段。

### 7.2 拆分 intent 与 conversation_context

1. `RunStartedPayload` 新增 `current_request: str` 字段，保存用户本次原始请求。
2. `conversation_context` 不写入 `RunStarted`，而由 Scheduler/Agent Kernel 在需要时读取。

### 7.3 上下文按需注入

- classify 阶段可能只需要 `current_request`，不需要完整历史。
- plan/revise/answer 阶段由 Agent Kernel 组装历史，但应避免在每个 revise 轮次中重复完整内容。

### 7.4 日志增强

- classify/plan/revise 日志同时输出 `current_request[:80]` 与 `context_len`，而不是只输出被污染后的 intent 前缀。

### 7.5 单条消息单 Run 保障

- 在 API 层添加幂等键或前端请求去重，避免一次用户点击产生多个 Run（如日志中同一对话短时间内出现 0892b3b8 与 5732596a）。

### 7.6 LLM 客户端并发与可观测性改造

1. **持久化 httpx 客户端**
   - `OpenAILLMClient` 启动时创建 `httpx.AsyncClient(limit=..., timeout=...)`，整个进程生命周期复用，而不是每次 `chat()` 新建客户端。

2. **请求日志带 run_id**
   - `LLMClient.chat()` 增加可选 `run_id` 参数；
   - 日志输出形如 `[LLM] [run=2b6aff6e] Sending 1 messages ...`；
   - 便于把并发请求与具体 Run 关联。

3. **增加并发控制（可选）**
   - 根据后端容量配置 `asyncio.Semaphore`，避免无限制并发压垮 provider 或本地连接池。

4. **请求级超时与重试**
   - 区分连接超时、读取超时；
   - 对 provider 5xx/429 实现指数退避重试。

---

## 8. 相关文件

- `harness/api/routes.py`（行 98-148 `create_run`，行 459-499 `send_message`）
- `harness/api/deps.py`（行 112-154 `_write_assistant_message`）
- `harness/core/scheduler/base.py`（行 353-376 `_fail`）
- `harness/core/scheduler/plan.py`（`_classify_intent` 与日志输出）
- `harness/core/fold.py`（`RUN_FAILED` 折叠逻辑）
- `harness/models/conversation.py`（行 76 `_build_conversation_context`）
- `harness/models/events.py`（`RunStartedPayload` / `RunFailedPayload` schema）
- `data/logs/harness.log`

---

## 9. QA 备注

- 本 Bug 定为 **P0**，因为用户-facing 输出直接暴露内部执行状态，已构成可用性事故与潜在信息泄露，不能因"开发阶段"而忽略。
- 修复需要架构层调整：明确区分内部遥测（`result_summary`/`last_error`）与用户-facing 输出，并将会话上下文管理从 API 层的 intent 拼接迁移到 Agent Kernel 的上下文窗口管理中。
- 建议在修复后补充以下测试：
  - 契约测试：`RunStartedPayload.intent` 不包含 `Previous conversation:` 前缀。
  - 行为测试：失败 Run 的 assistant 消息中不得出现 `DAG execution:`、`Steps not achieved:`、`Task terminated.`、`tool call(s)` 等内部字段。
  - 性能测试：follow-up 消息的单次 Run 总 prompt token 不应随历史消息数线性膨胀。

## 10. 修复验收记录（2026-08-07）

- `RunStartedPayload.current_request` 与 `intent` 只保存本次用户请求，会话历史不再写入事件 intent。
- 会话上下文通过 scheduler/planner 的独立参数按需注入，初始 plan 或 analysis answer 各最多注入一次，不进入 classify/revise 的 intent。
- `RunFailedPayload.user_facing_message` 与内部 `result_summary` 分离，Conversation assistant 消息不再透传 DAG、step、tool count 或终止标记。
- `TOOL_*`、Guardrail、Confirmation 事件补齐 `step_id`，完成语义链路和用户输出链路均可审计。
- LLM 客户端支持 `run_id` 请求日志、持久化 `httpx.AsyncClient`、信号量并发隔离。
- API 支持 `client_request_id` 幂等去重，避免同一用户点击创建多个 Run。
- 新增 `tests/test_p0_06_status_and_prompt_boundary.py` 回归测试。
