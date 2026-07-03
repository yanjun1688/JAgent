# Bug: P0-1 API Schema 与 Domain Model 重复定义

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P0-01 |
| 严重级别 | P0 必须修复 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/api/schemas.py:79-118` vs `harness/models/conversation.py:14-66` |
| 影响范围 | 前后端契约、OpenAPI 自动生成、可维护性 |
| 违反规范 | AGENTS.md §4.1 "前后端共享的数据结构必须同源定义" |

## 现象

`harness/api/schemas.py` 定义了 7 个与 `harness/models/conversation.py` 字段高度重复的 Schema 类：

- `ConversationResponse` ≈ `Conversation`（仅 `status` 类型从 Enum 退化为 `str`）
- `ConversationMessageResponse` = `ConversationMessageItem`
- `ConversationDetailResponse` = `ConversationDetail`
- `ConversationListResponse` = `ConversationListResponse`
- `CreateConversationRequestModel` = `CreateConversationRequest`
- `SendMessageRequestModel` = `SendMessageRequest`
- `UpdateConversationRequestModel` = `UpdateConversationRequest`

## 根因

实现时沿用了 `routes.py` 中存量 endpoint 的 pattern（如 `CreateConversationRequestModel` 继承 `BaseModel`），而非复用已有 domain model。

## 为什么现有机制没拦住

- AGENTS.md §4.1 明确要求同源定义，但无自动化检查（如 lint rule）
- 代码审查时未发现重复

## 修复方案

从 `harness/models/conversation.py` 直接导入模型到 `schemas.py`（或直接删掉 API Schema 类，在 routes.py 中用 domain model 做 `response_model`）。

## 测试用例

1. 删除 schemas.py 中重复类后，测试全部通过
2. `npm run generate-api:types` 生成的 `schema.ts` 中 conversation 类型与 Python 模型一致
