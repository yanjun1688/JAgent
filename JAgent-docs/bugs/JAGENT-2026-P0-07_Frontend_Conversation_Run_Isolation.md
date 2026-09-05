# JAGENT-2026-P0-07 前端会话之间 Run 实时渲染串线

> **严重等级**: P0（会话隔离与用户可观测性错误）  
> **状态**: 已修复，已完成前后端隔离回归验证  
> **发现日期**: 2026-08-07  
> **报告人**: QA（黑盒测试）  
> **影响范围**: 前端聊天页、Run WebSocket、实时思考/工具调用渲染

---

## 1. 问题摘要

前端聊天页面没有将实时 Agent 执行状态严格绑定到当前会话和当前 `run_id`。

同时发现同一聊天页还会直接展示内部完成门错误，例如：

```text
Steps not achieved: s1
```

该信息属于 Scheduler 内部诊断，不应作为用户-facing 消息显示。

用户在会话 A 发起任务后切换到会话 B，仍然可以在 B 中看到 A 的：

- Agent 思考状态；
- 工具调用卡片；
- 工具执行结果或错误；
- 暂停、确认和运行中状态；
- 实时事件数量与连接状态。

这会造成跨会话执行信息泄漏，并使用户误以为 B 正在执行 A 的任务。

---

## 2. 复现步骤

1. 打开前端聊天页。
2. 在会话 A 发送一个需要较长时间执行的任务，例如：
   ```text
   请访问 https://httpbingo.org/delay/10，然后告诉我返回结果。
   ```
3. 在 A 仍显示 Agent 思考或工具执行时，切换到会话 B。
4. 观察会话 B 的聊天区域和实时面板。

### 实际结果

会话 B 继续显示会话 A 的 Agent 思考、工具调用和实时执行状态。

### 预期结果

- 会话 B 只显示 B 自己的历史消息和 B 自己的 Run 状态。
- B 没有正在执行的 Run 时，不应显示 ThinkingPanel、ToolCallCard 或确认卡片。
- A 的实时事件不得出现在 B 的渲染树中。
- 切回 A 后，只有在 A 的 Run 仍然有效且订阅恢复成功时，才显示 A 的实时状态。

---

## 3. 当前代码证据

### 3.1 切换会话没有清理临时 Run 状态

`frontend/src/pages/ChatPage.tsx` 的 `loadConversation()` 只更新：

- `activeConvId`；
- 会话标题；
- `messages`。

它没有同步清理或切换：

- `activeRunId`；
- `activeRunStatus`；
- `timelineEvents`；
- `isExecuting`；
- `queue`；
- `thoughtOpen`。

因此切换到 B 后，`activeRunId` 仍然是 A 的 Run ID。

### 3.2 实时组件由残留的 activeRunId 驱动

聊天页使用：

```tsx
useRunWebSocket(activeRunId)
```

并以 `activeRunId` 是否存在决定是否渲染：

- `ThinkingPanel`；
- `ToolCallCard`；
- `ConfirmationCard`。

只要 A 的 `activeRunId` 没有被清除，B 就会继续渲染 A 的实时状态。

### 3.3 全局 Run Store 缺少事件归属防线

`frontend/src/stores/runStore.ts` 使用单一全局状态：

```text
activeRunId
runStatus
events
```

`addEvent()` 只按 `seq` 排序，不校验：

```text
event.run_id === state.activeRunId
```

这使得任何误到达当前组件的其他 Run 事件都可能进入当前渲染数据集。

### 3.4 Hook 只按 run_id 变化清理事件

`frontend/src/hooks/useRunWebSocket.ts` 仅在 `runId` 变化时调用 `setActiveRun(runId)` 清空事件。

会话切换但 `activeRunId` 未变化时，Hook 不会重置，原 Run 的订阅和事件流继续存在。

### 3.5 失败消息直接读取内部 final_error

`frontend/src/pages/ChatPage.tsx` 在 Run 失败时使用：

```tsx
allEvents.find((e) => e.event_type === 'RunFailed')?.payload.final_error
```

`final_error` 是内部完成门/调度错误字段，可能包含：

- `Steps not achieved: s1`；
- 内部 step ID；
- 工具或 Scheduler 错误细节。

后端事件模型已经提供专用于用户输出的 `user_facing_message`，但聊天页没有读取该字段，导致内部错误直接进入 assistant 消息。

---

## 4. 根因

1. **会话状态与 Run 状态没有建立一一对应关系**：`activeConvId` 与 `activeRunId` 是两个独立状态，切换会话时没有原子切换。
2. **临时事件状态没有 conversation/run 维度**：`timelineEvents` 和全局 `runStore.events` 没有按会话或 Run 分区。
3. **WebSocket 事件入口缺少受信过滤**：前端假设订阅端点永远只返回当前 Run 的事件，没有再次验证事件的 `run_id`。
4. **渲染条件只判断是否存在 Run**：没有判断该 Run 是否属于当前会话。
5. **用户输出字段选择错误**：失败消息使用内部 `final_error`，没有使用 `user_facing_message`。

---

## 5. 影响评估

| 维度 | 影响 |
|---|---|
| 会话隔离 | A 的执行过程显示在 B 中，违反会话边界 |
| 信息泄漏 | 工具输入、输出、错误和确认信息可能跨会话展示 |
| 内部状态泄漏 | 用户可看到 `Steps not achieved: s1` 等 Scheduler 完成门细节 |
| 用户体验 | 用户无法判断当前看到的状态属于哪个任务 |
| 操作风险 | 用户可能在 B 中误操作 A 的确认、暂停或恢复流程 |
| 可观测性 | UI 状态与后端 Run 状态不一致，排查困难 |

---

## 6. 修复要求

修复必须在前端状态边界实现，不依赖 Agent 或后端返回正确行为：

- 当前会话切换时，原 Run 的实时渲染状态必须立即卸载或切换到对应 Run。
- `activeRunId` 必须验证属于当前 `conversation_id`。
- WebSocket 事件进入 Store 前必须校验 `event.run_id` 与订阅 Run ID 一致。
- `timelineEvents`、思考面板、工具卡片和确认卡片必须只消费当前 Run 的事件。
- 切换会话时不得保留上一会话的 `isExecuting`、队列、确认状态和实时事件。
- 需要覆盖并发场景：A 仍运行时切换到 B，再切回 A；A、B 连续快速提交任务。

---

## 7. 验收标准

- [ ] A 运行期间切换到 B，B 不显示 A 的思考、工具调用、确认或错误事件。
- [ ] B 没有活动 Run 时，`ThinkingPanel`、`ToolCallCard`、`ConfirmationCard` 均不渲染。
- [ ] WebSocket 收到错误 `run_id` 的事件时，事件被丢弃并记录可诊断日志。
- [ ] A 运行期间切换到 B 再切回 A，A 的事件按 `seq` 正确续接，不显示 B 的事件。
- [ ] A、B 同时运行时，两个会话的实时事件完全隔离。
- [ ] 确认、暂停、恢复操作只能携带当前会话对应的 `run_id`。
- [ ] Run 失败时只渲染 `user_facing_message`，不得渲染 `final_error`、`result_summary` 或内部 `Steps not achieved: ...`。
- [ ] 前端失败消息回归测试覆盖 `final_error="Steps not achieved: s1"` 时仍只显示用户友好文本。
- [ ] 新增前端回归测试覆盖会话切换和错误 Run 事件过滤。

## 8. 相关文件

- `frontend/src/pages/ChatPage.tsx`
- `frontend/src/hooks/useRunWebSocket.ts`
- `frontend/src/stores/runStore.ts`
- `frontend/src/components/chat/ThinkingPanel.tsx`
- `frontend/src/components/chat/ToolCallCard.tsx`
- `frontend/src/components/chat/ConfirmationCard.tsx`

## 9. 修复验收记录（2026-08-12）

- 前端切换会话时原子清理 active run、事件、队列、确认和执行状态。
- Store 和 WebSocket 入口按 `run_id` 过滤事件，并按 `seq` 排序。
- 后端 WebSocket 建立连接前通过当前 tenant 的 ScopedEventStore 验证 Run；未知或跨租户 Run 以 4404 拒绝。
- 广播按事件 tenant 过滤，避免同一 run_id 在不同租户间串流。
- 失败消息只使用 `user_facing_message`。
- 前端回归测试、后端全量测试和构建均通过。
