# JAGENT-2026-P1-12 feedback 统一查询被 ScopedEventStore 错误拒绝

## 状态

已修复（2026-08-11 质量门禁回归通过）

## 发现方式

后端跨组件集成测试 `tests/test_backend_integration.py` 的统一 query 全类型分发场景。

## 影响

调用 `GET /api/v1/query?type=feedback` 时，即使数据库和请求均有效，也会抛出未转换的 `ValueError`，导致接口返回 500，破坏统一查询接口的类型分发契约。

## 复现

```http
GET /api/v1/query?type=feedback
```

## 实际结果

`harness.storage.scoped.ScopedEventStore.execute_query` 抛出：

```text
ValueError: Scoped analysis only permits SELECT queries over events
```

## 预期结果

应返回 `200`，响应封装为 `{ "type": "feedback", "data": [], "meta": ... }`；存在反馈事件时返回租户范围内的反馈记录。

## 根因定位

`harness/api/query.py:514-526` 使用多行 SQL，`FROM events` 与 `WHERE` 之间是换行；`harness/storage/scoped.py:144` 的 SQL 允许性检查要求文本中存在带空格的 `" from events "`，因此合法 SQL 被拒绝。

## 测试证据

`TestAnalysisQueryIntegration.test_unified_query_dispatches_every_declared_type[feedback]` 失败。
