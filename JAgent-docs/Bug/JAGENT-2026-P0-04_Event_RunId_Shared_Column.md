# Bug: P0-4 Conversation 事件与 Run 事件共享 run_id 列空间

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P0-04 |
| 严重级别 | P0 必须修复 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/storage/event_store.py:475-476` |
| 影响范围 | 数据模型歧义、后续 Phase 查询逻辑 |
| 违反规范 | conversation_dev_plan.md §2.3 "events 表新增 conversation_id 列" |

## 现象

`get_events_for_conversation(self, conversation_id)` 直接调用 `get_events(conversation_id)`，将 conversation_id 填入 events 表的 `run_id` 列：

```python
async def get_events_for_conversation(self, conversation_id: str) -> list[Event]:
    return await self.get_events(conversation_id)
```

这意味着：
1. Conversation 事件（`ConversationStarted`, `ConversationMessage`）和 Run 事件（`RunStarted`, `AgentThought`）都在 `run_id` 列中，无法区分
2. 无法通过 SQL 查询"属于某 conversation 的所有 Run 事件"
3. `list_runs()` 使用 `WHERE run_id LIKE 'run-%'` 过滤，Conversation 事件的 `run_id = conversation_id` 以 `conv_` 开头，暂时不污染 Run 列表——但这是靠命名前缀规避，不稳健

## 根因

Dev plan §2.3 设计了 `events.conversation_id` 列为 NULLABLE，但实际实现时未执行 ALTER TABLE，直接复用了 `run_id`。

## 为什么现有机制没拦住

- Dev plan 与实现之间的架构审查遗漏了数据模型变更

## 修复方案

按 dev_plan §2.3 设计执行：

```sql
ALTER TABLE events ADD COLUMN conversation_id TEXT;
CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id);
```

修改 `append_event` 签名可选接收 `conversation_id`，Run 事件写入时填入。Conversation 级别事件保持 `run_id = conversation_id`（或也填入 `conversation_id`）。

## 兼容性注意

存量 events 表无 `conversation_id` 列，ALTER TABLE 后存量数据 `conversation_id` 为 NULL，可通过 fold 重建补填。

## 测试用例

1. 创建 conversation → 发消息 → run 结束。查询 `WHERE conversation_id = '<conv_id>'` 返回所有关联事件（ConversationStarted + 用户消息 + RunStarted + AgentThought + assistant 消息）
2. 两个不同 conversation 下的 Run 事件在 `conversation_id` 列上正确隔离
