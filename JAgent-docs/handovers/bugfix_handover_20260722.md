# Harness v2.1 Bug 修复交接文档

## 【项目背景】

- **项目**: Harness v2.1 Agent-First 任务执行引擎
- **路径**: `D:\Project\JAgent`
- **技术栈**:
  - 后端：Python 3.11、FastAPI、Pydantic v2、aiosqlite/SQLite、pytest、pytest-asyncio
  - 核心架构：事件溯源（Append-Only Event Store）、CQRS、受信组件强制约束（Tool Layer / Scheduler / Event Store）、Agent 决策与系统强制分离
  - 前端：React + TypeScript（本次主要未触碰）
- **项目结构（关键部分）**:
  - `harness/models/events.py` — 事件类型与 Payload Pydantic Model
  - `harness/models/conversation.py` — Conversation 领域模型（同源契约）
  - `harness/core/fold.py` — `fold_events()`，事件流折叠为 `RunState`
  - `harness/core/scheduler/base.py` — Scheduler 生命周期基础设施（pause/resume/cancel/fail）
  - `harness/core/scheduler/loop.py` — 串行 think→act→observe loop
  - `harness/core/scheduler/plan.py` — PlanningExecutorScheduler（Plan→Execute→Revise）
  - `harness/core/context_manager.py` — 上下文压缩 / checkpoint
  - `harness/api/schemas.py` — 非 conversation REST Schema
  - `harness/api/routes.py` — REST endpoints
  - `harness/api/deps.py` — HarnessAPI DI 容器与 run cleanup
  - `harness/storage/event_store.py` — Append-Only Event Store
  - `tests/test_commands.py`, `tests/test_fallback_kernel.py`, `tests/test_context_window.py` — 本次新增验收测试

## 【当前 Bug】

- **主要现象**:
  - 新增测试初始运行：`25 failed, 35 passed`
  - 全量测试初始运行：`25 failed, 660 passed, 2 skipped`
  - 失败集中在：
    1. `RunCommandPayload` / `EventType.RUN_COMMAND` / Scheduler command handling 缺失（Bug P0-05）
    2. `ContextManager.select_compression_window()` 正常/紧急压缩边界错误，导致 `keep_count` 期望 2 实际 3（Bug P1-02）
    3. `TestToolOutputTruncation` 使用 `asyncio.get_event_loop().run_until_complete()`，与 pytest-asyncio auto mode 冲突，全量运行时事件循环已关闭（Bug P1-03）
    4. `tests/test_commands.py` 中部分测试函数内未导入 `RunCommandPayload`，首个导入是函数局部变量，后续测试 `NameError`
- **K3 分析根因**:
  - P0-05：架构/测试计划已定义 RunCommand 控制面，但 L3 Scheduler 基础设施未落地，属于代码滞后于架构。
  - P1-02：压缩边界用 `estimate < emergency_threshold` 判定正常压缩；在 `estimate == token_limit` 且 `emergency_threshold < token_limit` 时误入 emergency。
  - P1-03：测试自身事件循环管理方式与 pytest-asyncio 冲突；应将同步测试改为 `async def`，由 pytest-asyncio 托管事件循环。
  - P0-01/P0-02/P0-03：已开始修复但**尚未完成**，当前代码处于中间态。
- **相关文件**:
  - `harness/models/events.py`
  - `harness/core/fold.py`
  - `harness/core/scheduler/base.py`
  - `harness/core/scheduler/loop.py`
  - `harness/core/scheduler/plan.py`
  - `harness/core/context_manager.py`
  - `harness/api/deps.py`
  - `harness/api/schemas.py`
  - `harness/api/routes.py`
  - `harness/models/conversation.py`
  - `harness/models/__init__.py`
  - `tests/test_commands.py`
  - `tests/test_context_window.py`

## 【已完成修改】

1. **RunCommand 模型与 fold 支持已完成**
   - `harness/models/events.py` 新增：
     ```python
     RUN_COMMAND = "RunCommand"

     class RunCommandPayload(BaseModel):
         command: Literal["hard_abort", "soft_abort", "pause", "resume", "skip_tool"]
         reason: str = ""
         affected_tool: str | None = None
         issued_by: str = "monitor"
     ```
   - 已注册到 `PAYLOAD_MODEL_MAP`。
   - `harness/core/fold.py` 新增：
     ```python
     case EventType.RUN_COMMAND:
         pass
     ```

2. **Scheduler command handling 已完成并通过测试**
   - `harness/core/scheduler/base.py` 新增：
     - `self._last_processed_command_seq: dict[str, int] = {}`
     - `_check_pending_commands(run_id)`
     - `_process_command(run_id, command)`
     - `_latest_command_seq(run_id, command)`
     - `_handle_pending_commands(run_id)`
   - 关键点：command seq 使用 **RUN_COMMAND 事件流内的 1-based ordinal**，不是事件表全局 seq；否则 `RUN_STARTED` 会占用 seq=1，导致新增测试的 processed 语义不成立。
   - `hard_abort` / `soft_abort` 调用 `_fail(run_id, f"Run aborted by command: {command}")`。
   - `_check_pending_commands()` 对 Event Store 读取异常 `try/except` 并返回 `None`，满足 CM-F1 不 crash。

3. **Scheduler loop 已接入 command 检查**
   - `loop.py` 每轮迭代开始处调用 `_handle_pending_commands()`。
   - `plan.py` 的 Plan-Execute-Revise 循环改为 `while True`，在每轮开始检查 terminal / cancel / pending command，避免 command pause 后 scheduler 直接退出。

4. **Compression window 边界已修复**
   - `harness/core/context_manager.py`：
     ```python
     overflow_threshold = max(self.emergency_threshold, self.token_limit)
     if estimate <= overflow_threshold:
         # normal compression, keep_count=2
     ```
   - 效果：`token_limit=1000, estimate=1000` 走 normal；`token_limit=500, estimate=2000` 走 emergency。

5. **新增测试自身的两处问题已修正**
   - `tests/test_commands.py` 顶部导入新增 `RunCommandPayload`。
   - `tests/test_context_window.py` 的 3 个 `TestToolOutputTruncation` 测试从手动 `asyncio.get_event_loop().run_until_complete()` 改为 `async def` + `await`，未改断言。

6. **验证结果**
   - 已完成一次定向验证：
     ```bash
     .\.venv\Scripts\python.exe -m pytest tests/test_commands.py tests/test_fallback_kernel.py tests/test_context_window.py -v --tb=short
     ```
   - 结果：`60 passed`
   - 注意：这是在后续 P0-01/P0-02/P0-03 API 修改**之前**的结果；当前工作树尚未重新跑全量。

## 【当前中间态风险】

- `harness/api/routes.py` 当前是**不完整状态**：
  - `schemas.py` 中重复 conversation schema 已删除。
  - `routes.py` 顶部已改成只从 `harness.api.schemas` 导入非 conversation schema。
  - 但后续代码仍引用 `ConversationResponse`、`ConversationListResponse`、`ConversationDetailResponse`、`CreateConversationRequestModel` 等旧名字。
  - 一次准备把 `harness.models.conversation` 导入扩展为完整 domain model 导入的 edit 被用户暂停中断，因此 **routes.py 现在大概率 import/decorator 阶段就会 NameError**。
- `harness/api/deps.py` 已把 `_write_assistant_message()` 的 `except Exception: pass` 改成 `_log.exception(...)`。
- `harness/models/conversation.py` 已新增：
  - `DeleteConversationResponse`
  - `UpdateConversationResponse`
- `harness/models/__init__.py` 已导出上述两个 response model。

## 【修改方向】

- **K3 建议下一步先修复 `harness/api/routes.py` 的 conversation domain model 同源导入与 response_model**：
  1. 从 `harness.models.conversation` 导入：
     ```python
     from harness.models.conversation import (
         Conversation,
         ConversationDetail,
         ConversationListResponse,
         ConversationMessageItem,
         CreateConversationRequest,
         CreateConversationResponse,
         DeleteConversationResponse,
         SendMessageRequest,
         SendMessageResponse,
         UpdateConversationRequest,
         UpdateConversationResponse,
         _build_conversation_context,
     )
     ```
  2. 替换 routes 中旧名字：
     - `ConversationResponse` → `Conversation`
     - `ConversationMessageResponse` → `ConversationMessageItem`
     - `ConversationDetailResponse` → `ConversationDetail`
     - `CreateConversationRequestModel` → `CreateConversationRequest`
     - `SendMessageRequestModel` → `SendMessageRequest`
     - `UpdateConversationRequestModel` → `UpdateConversationRequest`
  3. 为 4 个 conversation endpoint 补 `response_model`：
     - `POST /api/v1/conversations` → `response_model=CreateConversationResponse`
     - `POST /api/v1/conversations/{conversation_id}/messages` → `response_model=SendMessageResponse`
     - `DELETE /api/v1/conversations/{conversation_id}` → `response_model=DeleteConversationResponse`
     - `PATCH /api/v1/conversations/{conversation_id}` → `response_model=UpdateConversationResponse`

- **注意点**:
  - 不要恢复 `harness/api/schemas.py` 中的重复 conversation class；Bug P0-01 要求同源定义。
  - `Conversation.status` 是 `ConversationStatus` Enum，但它是 `str, Enum`，现有测试 `assert c.status == "active"` 仍可通过。
  - 修改 routes 后必须先跑 `tests/test_conversation.py` 和 `tests/test_api.py`，再跑新增三件套，最后全量。
  - P0-04（events 表 conversation_id 列）尚未开始；如继续，需要先设计迁移兼容，避免影响 `list_runs()` 和存量 `.harness.db`。

## 【请 GLM-5.2 执行】

1. **第一步：恢复 routes.py 可导入状态**
   - 按上方导入块补齐 `harness.models.conversation` 导入。
   - 全局替换旧 conversation schema 引用到 domain model。
   - 给 4 个 endpoint 补 `response_model`。

2. **第二步：运行快速验证**
   ```bash
   .\.venv\Scripts\python.exe -m pytest tests/test_conversation.py tests/test_api.py -v --tb=short
   .\.venv\Scripts\python.exe -m pytest tests/test_commands.py tests/test_fallback_kernel.py tests/test_context_window.py -v --tb=short
   ```

3. **第三步：运行全量测试**
   ```bash
   .\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short
   ```
   - 期望：所有存量回归 passed，新增 60 项 passed。
   - 当前已知基线：修复前 `660 passed, 2 skipped`；新增测试修复后应达到 `685 passed, 2 skipped` 左右（若未新增额外测试）。

4. **第四步：如继续处理剩余 Bug 报告**
   - P0-04 `Event_RunId_Shared_Column`：需为 `events` 表增加 nullable `conversation_id` 列 + index + 迁移逻辑，并让 `get_events_for_conversation()` 同时兼容旧数据（`run_id = conversation_id`）和新数据（`conversation_id = ...`）。注意不要让 conversation 事件污染 `list_runs()`。
   - 所有修改必须遵守 AGENTS.md：受信组件行为不依赖 Agent 配合；禁止修改存量测试；禁止改已有通过测试的断言。
