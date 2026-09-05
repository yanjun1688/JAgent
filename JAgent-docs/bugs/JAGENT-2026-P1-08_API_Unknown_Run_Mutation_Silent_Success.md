# JAGENT-2026-P1-08 API 对不存在 Run 静默成功

## 状态

已修复（2026-08-11 质量门禁回归通过）

## 发现方式

接口测试 `tests/test_api_contract_robustness.py`。

## 影响

对不存在的 `run_id` 执行确认、反馈或删除时，接口返回成功；调用方无法区分“操作已生效”和“资源不存在”。删除路径还会触发后台资源清理任务，测试中在数据库关闭后产生 `Cannot operate on a closed database` 异步异常日志。

## 复现

```http
POST /api/v1/runs/missing/confirm
{"confirmation_id":"c1","confirmed":true}

POST /api/v1/runs/missing/feedback
{"text":"please retry"}

DELETE /api/v1/runs/missing
```

## 实际结果

- `confirm`: `200 {"success": true}`，并写入 `ConfirmationReceived`。
- `feedback`: `200`，并写入 `FeedbackInjected`。
- `delete`: `200 {"success": true}`，随后调用 `cleanup_run_resources`。

## 预期结果

三个接口均应在确认 Run 事件流存在后执行操作；不存在时返回结构化 `404`，且不写入事件、不启动清理异步任务。

## 根因定位

- `harness/api/routes.py:395` 的 `confirm_run` 未检查 `events` 是否为空。
- `harness/api/routes.py:459-475` 的 `operator_feedback` 未检查 Run 是否存在。
- `harness/api/routes.py:494-503` 的 `delete_run` 对空事件流仍返回成功并清理资源。

## 测试证据

`TestResourceRobustness.test_confirmation_and_feedback_are_not_accepted_for_unknown_run`、`test_delete_unknown_run_is_not_reported_as_success` 失败。
