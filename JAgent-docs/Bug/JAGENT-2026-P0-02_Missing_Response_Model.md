# Bug: P0-2 Conversation API 端点缺少 response_model

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P0-02 |
| 严重级别 | P0 必须修复 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/api/routes.py` |
| 影响范围 | OpenAPI 文档、前端 TypeScript 类型自动生成 |
| 违反规范 | AGENTS.md §4.1, §6.2 |

## 现象

以下 4 个 conversation API 端点未设置 `response_model` 装饰器参数：

| 端点 | 行号 | 当前返回 |
|---|---|---|
| `POST /api/v1/conversations` | :369 | `{conversation_id, title, created_at}` dict |
| `POST /api/v1/conversations/{id}/messages` | :451 | `{run_id, conversation_id, seq}` dict |
| `DELETE /api/v1/conversations/{id}` | :494 | `{success: bool}` dict |
| `PATCH /api/v1/conversations/{id}` | :507 | `{success: bool}` dict |

## 根因

实现时遗漏了 `response_model` 参数，直接用 `dict` 返回。

## 为什么现有机制没拦住

OpenAPI 文档生成工具不强制要求 `response_model`（未设置时生成空 schema），无 CI 检查。

## 修复方案

1. 为 `POST /conversations` 添加 `response_model=CreateConversationResponse`（已在 models 中定义，含 `conversation_id, title, created_at`）
2. 为 `POST /{id}/messages` 添加 `response_model=SendMessageResponse`（已在 models 中定义，含 `run_id, conversation_id, seq`）
3. 为 `DELETE /{id}` 新增 `DeleteConversationResponse(BaseModel)` 模型类（仅 `success: bool`），添加 `response_model`
4. 为 `PATCH /{id}` 新增 `UpdateConversationResponse(BaseModel)` 模型类（仅 `success: bool`），添加 `response_model`

## 测试用例

1. `GET /api/v1/openapi.json` 中 `/api/v1/conversations` POST 的 `responses.200.content.schema` 正确显示 `CreateConversationResponse` 字段
2. 前端 `npm run generate-api:types` 生成的 TypeScript 类型包含以上 4 个端点的响应类型
