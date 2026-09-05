# JAgent Bug Summary

> 统一 Bug 索引。点击 Bug ID 可跳转到对应报告；本文件只做分类和导航，不替代各 Bug 的详细说明。

## 统计

| 分类 | 数量 |
|---|---:|
| P0 | 7 |
| P1 | 12 |
| 合计 | 19 |

## P0

| Bug | 标题 | 状态 |
|---|---|---|
| [P0-01](./JAGENT-2026-P0-01_Schema_Duplication.md) | API Schema 与 Domain Model 重复定义 | 已修复 |
| [P0-02](./JAGENT-2026-P0-02_Missing_Response_Model.md) | API 缺少 Response Model | 已修复 |
| [P0-03](./JAGENT-2026-P0-03_Silent_Exception_Swallow.md) | 静默吞异常 | 已修复 |
| [P0-04](./JAGENT-2026-P0-04_Event_RunId_Shared_Column.md) | Conversation 与 Run 事件共享 run_id 列空间 | 已修复 |
| [P0-05](./JAGENT-2026-P0-05_Missing_RunCommand_Model.md) | 缺少 RunCommand Model | 已修复 |
| [P0-06](./JAGENT-2026-P0-06_User_Facing_Internal_Status_Leak_and_LLMPromptBloat.md) | 用户侧泄漏内部状态与 LLM Prompt 膨胀 | 已修复 |
| [P0-07](./JAGENT-2026-P0-07_Frontend_Conversation_Run_Isolation.md) | 前端会话之间 Run 实时渲染串线 | 已修复 |

## P1 历史问题

| Bug | 标题 | 状态 |
|---|---|---|
| [P1-01](./JAGENT-2026-P1-01_Missing_Truncation_Rules.md) | 缺少工具输出截断规则 | 已修复 |
| [P1-02](./JAGENT-2026-P1-02_Compression_Window_Mismatch.md) | 上下文压缩窗口不一致 | 已修复 |
| [P1-03](./JAGENT-2026-P1-03_Event_Loop_Conflict_Truncation_Tests.md) | 截断测试事件循环冲突 | 已修复 |
| [P1-04](./JAGENT-2026-P1-04_Logging_Fmtkv_Arity_Crash.md) | 日志 fmtkv 参数数量导致崩溃 | 已修复 |
| [P1-05](./JAGENT-2026-P1-05_Answer_Phase_Fabrication.md) | Answer 阶段内容编造 | 已修复 |
| [P1-06](./JAGENT-2026-P1-06_SoftError_SelfHeal_Loop_NonConvergence.md) | Soft-error 自愈循环不收敛 | 已修复 |

## P1 本轮接口与集成测试发现

| Bug | 标题 | 状态 |
|---|---|---|
| [P1-08](./JAGENT-2026-P1-08_API_Unknown_Run_Mutation_Silent_Success.md) | 不存在 Run 的写操作静默成功 | 已修复 |
| [P1-09](./JAGENT-2026-P1-09_API_Run_Read_Empty_Response.md) | 不存在 Run 的读接口返回空成功 | 已修复 |
| [P1-10](./JAGENT-2026-P1-10_API_Pagination_Boundary_Not_Validated.md) | API 分页参数缺少边界校验 | 已修复 |
| [P1-11](./JAGENT-2026-P1-11_Checked_In_OpenAPI_Invalid_UTF8.md) | 已提交 OpenAPI 文件不是有效 UTF-8 | 已修复 |
| [P1-12](./JAGENT-2026-P1-12_Query_Feedback_Scoped_SQL_Rejects_Valid_Query.md) | feedback 查询被 ScopedEventStore 错误拒绝 | 已修复 |
| [P1-13](./JAGENT-2026-P1-13_Blackbox_Real_LLM_Workspace_Completion_and_Environment.md) | 真实 LLM Workspace 黑盒测试异常（classify 绕过 / 完成门脱钩 / DAG / watchdog / 环境） | 部分修复，08-13 复核发现遗留问题 |

## 测试关联

| 测试范围 | 结果 |
|---|---|
| 全项目后端测试 | 952 passed, 2 skipped |
| 接口测试专项 | 156 passed, 2 skipped |
| 后端集成测试专项 | 25 passed |

失败用例均已在对应 Bug 报告中记录复现路径和根因定位。当前项目未使用独立的 pytest `integration` marker，专项结果来自测试文件和集成测试矩阵的显式执行。
