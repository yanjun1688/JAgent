# JAGENT-2026-P1-10 API 分页参数缺少边界校验

## 状态

已修复（2026-08-11 质量门禁回归通过）

## 发现方式

接口测试 `tests/test_api_contract_robustness.py`。

## 影响

非法分页值未被统一拒绝，可能产生空页、Python 负切片语义或不一致查询结果；这会放大数据访问范围与分页契约的不确定性。

## 复现

```http
GET /api/v1/workspaces?limit=0
GET /api/v1/runs/abc/events?offset=-1
GET /api/v1/runs/abc/events?from_seq=-1
```

## 实际结果

以上请求均返回 `200`。其中 workspace 的 `limit` 参数没有约束且当前接口未实际使用，Run events 的 `offset` 是未知查询参数而被静默忽略，`from_seq=-1` 被当作从头读取。

## 预期结果

- `limit` 应为正整数并实际参与分页，或从契约中移除。
- Run events 应明确 `from_seq >= 0`、`limit >= 1` 的约束。
- 未声明的分页参数应被拒绝，或 OpenAPI 明确其忽略语义。

## 根因定位

- `harness/api/routes.py:160-167` 的 workspace events 未给 `limit`、`offset` 添加 `Query` 约束。
- `harness/api/routes.py:308-323` 的 Run events 未给 `from_seq`、`limit` 添加 `Query` 约束，且没有检查未知参数。

## 测试证据

`TestRequestBoundaries.test_public_pagination_does_not_accept_negative_or_zero_ranges` 的 3 个场景失败。
