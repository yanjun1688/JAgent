# JAGENT-2026-P1-09 Run 读取接口对不存在资源返回空成功

## 状态

已修复（2026-08-11 质量门禁回归通过）

## 发现方式

接口测试 `tests/test_api_contract_robustness.py`。

## 影响

调用方查询错误的 `run_id` 时得到 `200` 和空资源集合，破坏 REST 资源语义，也会让前端误显示“该 Run 没有事件/工具轨迹”，而不是提示资源不存在。

## 复现

```http
GET /api/v1/runs/missing/events
GET /api/v1/analysis/runs/missing/timeline
GET /api/v1/analysis/runs/missing/tool-traces
```

## 实际结果

- Run events 返回 `200`、`{"events": [], "total": 0}`。
- timeline 返回 `200`、空 timeline。
- tool-traces 返回 `200`、空 `tool_traces`。

## 预期结果

三个接口均应返回结构化 `404`，与 `GET /api/v1/runs/{run_id}` 和 analysis detail 的行为一致。

## 根因定位

- `harness/api/routes.py:308-323` 未检查事件流为空。
- `harness/analysis/service.py:248-271` 空事件直接返回空 TimelineResponse。
- `harness/analysis/service.py:273-359` 空事件直接返回空 ToolTracesResponse。

## 测试证据

`TestResourceRobustness.test_unknown_run_reads_return_404` 中 3 个参数化场景失败。
