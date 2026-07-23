# Bug: P0-5 Missing RunCommand Model + Scheduler Command Handling

| 属性 | 值 |
|------|-----|
| Bug ID | JAGENT-2026-P0-05 |
| 严重级别 | P0 必须修复 |
| 发现日期 | 2026-07-22 |
| 所在文件 | `harness/models/events.py`, `harness/core/scheduler/base.py` |
| 影响范围 | Phase 2 执行控制 + Fallback 测试全部失败 (21 项) |
| 违反规范 | AGENTS.md §3.1 分层实现 — L3 Scheduler 缺少命令处理基础设施 |

## 现象

测试计划 Phase 2 中 21 项测试全部失败，原因：

1. `RunCommandPayload` 模型不存在于 `harness/models/events.py`
2. `EventType.RUN_COMMAND` 枚举值不存在
3. `PAYLOAD_MODEL_MAP` 中无 `RUN_COMMAND` 映射
4. `BaseScheduler._check_pending_commands()` 方法不存在
5. `BaseScheduler._process_command()` 方法不存在
6. `BaseScheduler._last_processed_command_seq` 属性不存在

## 根因

测试计划 `test_plan_v1.0.md` §4 描述的 RunCommand 事件模型和 Scheduler 命令处理机制尚未实现。测试用例基于架构设计编写，但对应代码未落地。

## 为什么现有机制没拦住

- 架构文档定义了 RunCommand 语义，但代码实现滞后
- 无自动化检查确保测试计划与代码同步

## 修复方案

需在以下文件添加实现：

1. `harness/models/events.py`:
   - 添加 `RUN_COMMAND = "RunCommand"` 到 `EventType`
   - 添加 `RunCommandPayload(BaseModel)` 含 `command: Literal["hard_abort", "soft_abort", "pause", "resume", "skip_tool"]`, `reason: str`, `affected_tool: str | None`, `issued_by: str = "monitor"`
   - 注册到 `PAYLOAD_MODEL_MAP`

2. `harness/core/scheduler/base.py`:
   - 添加 `_last_processed_command_seq: dict[str, int] = {}` 到 `BaseScheduler.__init__`
   - 实现 `_check_pending_commands(run_id)` — 读取事件流中最新未处理的 RUN_COMMAND
   - 实现 `_process_command(run_id, command)` — 根据命令类型执行对应操作

## 测试用例

修复后运行 `pytest tests/test_commands.py -v`，21 项测试应全部通过。
