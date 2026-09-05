# Harness v2.1 — 多轮对话全量测试计划

> **版本**: v1.0
> **测试负责人**: Test Engineer
> **基线**: 341 项测试，当前全通过
> **测试策略**: 分层测试（单元→集成→E2E）+ 契约测试 + 故障注入 + 回归
> **预估新增用例**: 100~120 项（分 4 Phase 交付）

---

## 目录

- [1. 测试策略总览](#1-测试策略总览)
- [2. 测试环境与基础设施](#2-测试环境与基础设施)
- [3. Phase 1 — 多轮对话后端测试](#3-phase-1--多轮对话后端测试)
  - [3.1 单元测试](#31-单元测试)
  - [3.2 集成测试](#32-集成测试)
  - [3.3 E2E 测试](#33-e2e-测试)
  - [3.4 契约测试](#34-契约测试)
- [4. Phase 2 — 执行控制 + Fallback 测试](#4-phase-2--执行控制--fallback-测试)
  - [4.1 单元测试](#41-单元测试)
  - [4.2 集成测试](#42-集成测试)
  - [4.3 故障注入测试](#43-故障注入测试)
- [5. Phase 3 — 上下文管理测试](#5-phase-3--上下文管理测试)
  - [5.1 单元测试](#51-单元测试)
  - [5.2 组件测试](#52-组件测试)
- [6. Phase 4 — 前端 UI 测试](#6-phase-4--前端-ui-测试)
  - [6.1 组件测试](#61-组件测试)
  - [6.2 集成测试](#62-集成测试)
  - [6.3 E2E 测试](#63-e2e-测试)
- [7. 回归测试](#7-回归测试)
- [8. 覆盖率目标](#8-覆盖率目标)
- [9. 测试执行计划](#9-测试执行计划)
- [A. 附录 — 测试用例清单](#a-附录--测试用例清单)

---

## 1. 测试策略总览

### 1.1 分层结构

```
┌──────────────────────────────────────────────────────┐
│  E2E 测试 (scripts/test_*.py)                        │
│  完整对话链: send_message → Run → WS event → 渲染    │
├──────────────────────────────────────────────────────┤
│  集成测试 (tests/test_*.py)                          │
│  EventStore + API + Scheduler + ContextManager 交互  │
├──────────────────────────────────────────────────────┤
│  单元测试 (tests/test_*.py)                          │
│  模型序列化 / 上下文构建 / 截断函数 / fold / 命令解析  │
├──────────────────────────────────────────────────────┤
│  契约测试 (OpenAPI Schema 验证)                      │
│  前后端共享数据结构一致性检查                          │
└──────────────────────────────────────────────────────┘
```

### 1.2 测试金字塔参数

| 层 | 执行速度 | Mock 策略 | 每 Phase 新增数 | 关键关注点 |
|----|---------|-----------|----------------|-----------|
| 单元测试 | <10ms/用例 | 全 Mock（无 EventStore I/O） | 30~40 | 纯函数逻辑 |
| 集成测试 | <100ms/用例 | EventStore = `:memory:` SQLite | 40~50 | 多组件交互 |
| E2E 测试 | ~5s/场景 | MockAgentKernel + MockLLMClient | 10~15 | 完整事件链 |
| 契约测试 | <5s | OpenAPI Schema vs Pydantic | 5~10 | 前后端类型一致 |

### 1.3 测试文件映射

```
tests/
├── test_conversation.py          # Phase 1 — 对话模型 + CRUD + 上下文注入
├── test_conversation_api.py      # Phase 1 — API 端点 (httpx)
├── test_commands.py              # Phase 2 — RunCommand + Scheduler 响应
├── test_fallback_kernel.py       # Phase 2 — FallbackKernel tools API
├── test_context_window.py        # Phase 3 — 动态窗口 + 截断
├── conftest.py                   # 共享 fixtures（新增 conversation 相关）
├── test_event_store.py           # 新增 conversation 表/列测试
├── test_fold.py                  # 新增 CONVERSATION_* 事件 fold
└── test_api.py                   # 新增对话端点测试

scripts/
├── test_conversation_e2e.py      # Phase 1 E2E: 完整多轮对话流
├── test_commands_e2e.py          # Phase 2 E2E: 命令注入与响应
└── test_conversation_frontend.py # Phase 4 E2E: 前端渲染流
```

---

## 2. 测试环境与基础设施

### 2.1 测试配置

```
[pytest.ini_options]  # pyproject.toml
asyncio_mode = auto           # 已有
testpaths = ["tests"]         # 已有
新增:
markers = [
    "slow: 需要较长时间执行的测试",
    "e2e: 端到端测试（需要真实组件装配）",
    "fault_injection: 故障注入测试",
    "frontend: 前端组件测试",
]
addopts = "--strict-markers"  # 防止拼写错误
```

### 2.2 共享 Fixtures（conftest.py 扩展）

```python
# conftest.py 新增

@pytest.fixture
async def store():
    """In-memory EventStore"""
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.fixture
async def conversation_store(store):
    """EventStore with conversations table initialized"""
    # 执行 migration
    await store.execute_query(
        """CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            message_count INTEGER NOT NULL DEFAULT 0
        )"""
    )
    return store


@pytest.fixture
def sample_conversation_id():
    return "conv-test-001"


@pytest.fixture
def sample_tool_defs():
    return [
        ToolDefinition(
            name="echo",
            description="Echo input",
            input_schema={},
            output_schema={},
            idempotency_key_fields=[],
            side_effects=[],
            timeout_ms=5000,
            retry_policy=RetryPolicy(max_retries=0),
        )
    ]


@pytest.fixture
def mock_kernel():
    return MockAgentKernel(["<STOP>"])
```

### 2.3 测试数据生成器

```python
# tests/helpers.py（新建）
"""测试数据生成器"""

def make_conversation_event(conv_id, seq, event_type, **extra):
    return Event(
        run_id=conv_id,
        seq=seq,
        event_type=event_type,
        payload={
            "conversation_id": conv_id,
            **extra
        },
        created_at=float(seq),
    )

def make_conversation_messages(count=3):
    """生成 count 轮对话消息（user→assistant 交替）"""
    events = [make_conversation_event("conv", 1, EventType.CONVERSATION_STARTED, title="Test")]
    for i in range(count):
        events.append(make_conversation_event(
            "conv", 2 + i*2, EventType.CONVERSATION_MESSAGE,
            role="user", content=f"User message {i+1}", run_id=f"run-{i}"
        ))
        events.append(make_conversation_event(
            "conv", 3 + i*2, EventType.CONVERSATION_MESSAGE,
            role="assistant", content=f"Assistant response {i+1}", run_id=f"run-{i}"
        ))
    return events
```

---

## 3. Phase 1 — 多轮对话后端测试

### 3.1 单元测试

#### 文件: `tests/test_conversation.py`（新建，约 150 行）

##### 3.1.1 数据模型序列化

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| C-U1 | `Conversation` 模型完整字段创建 | `conversation_id="c1"`, `title="Test"` | 所有字段正确赋值 | P0 |
| C-U2 | `Conversation` 默认值正确 | 只传必填字段 | `status=ACTIVE`, `message_count=0` | P0 |
| C-U3 | `ConversationMessageItem` 序列化 | `seq=1`, `role="user"`, `content="hi"` | JSON 包含全部字段 | P0 |
| C-U4 | `ConversationStatus` 枚举序列化 | `ACTIVE` | `"active"` | P1 |
| C-U5 | `ConversationDetail` 嵌套模型 | conversation + messages list | 结构正确 | P1 |
| C-U6 | `CreateConversationRequest` 字段可选 | 空请求体 | `title=None` 可接受 | P1 |

##### 3.1.2 事件 Payload 序列化

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| C-U7 | `ConversationStartedPayload` 序列化 | `conversation_id="c1"`, `title="Test"` | JSON 字段匹配 | P0 |
| C-U8 | `ConversationMessagePayload` 序列化 | `conversation_id="c1"`, `run_id="r1"`, `role="user"`, `content="hi"` | JSON 字段匹配 | P0 |
| C-U9 | `ConversationEndedPayload` 序列化 | `conversation_id="c1"` | JSON 字段匹配 | P1 |
| C-U10 | `EventType` 枚举含新类型 | — | `CONVERSATION_STARTED/MESSAGE/ENDED` 存在 | P0 |
| C-U11 | `PAYLOAD_MODEL_MAP` 注册新类型 | 3 种新事件 | 查表返回对应 Payload 模型 | P0 |

##### 3.1.3 对话上下文构建

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| C-U12 | 空对话 → 空上下文 | `messages=[]` | `""` | P1 |
| C-U13 | 1 轮对话 → 含 user+assistant | 1 user + 1 assistant | 包含 `[user]` 和 `[assistant]` | P0 |
| C-U14 | 3 轮对话 → 包含全部 6 条 | 3 user + 3 assistant | 6 行文本 | P0 |
| C-U15 | 5 轮对话 → 截断为最近 3 轮 | 5 user + 5 assistant | 只包含最近 3 轮（6 条） | P0 |
| C-U16 | 长内容截断 → 500 chars | 每个 content 1000 chars | 每个 `content[:500]` | P1 |
| C-U17 | 上下文格式正确 | — | 格式为 `[user] msg\n[assistant] msg` | P0 |

##### 3.1.4 Event Store 对话查询

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| C-U18 | `upsert_conversation` 插入新记录 | new Conversation | 插入成功 | P0 |
| C-U19 | `upsert_conversation` 更新已有记录 | 相同 ID，不同 title | UPDATE 而非 INSERT | P0 |
| C-U20 | `list_conversations` 空表 | 空 | `[]` | P0 |
| C-U21 | `list_conversations` 多记录排序 | 3 条，不同 updated_at | 按 updated_at 降序 | P0 |
| C-U22 | `list_conversations` 过滤 archived | 1 active + 1 archived | 只返回 active | P1 |
| C-U23 | `get_conversation` 存在 | valid ID | 返回 Conversation | P0 |
| C-U24 | `get_conversation` 不存在 | invalid ID | `None` | P0 |
| C-U25 | `delete_conversation` 软删除 | 存在记录 | `status=archived`, `updated_at` 更新 | P0 |

##### 3.1.5 fold 对话事件兼容性

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| C-U26 | `CONVERSATION_*` 事件在 fold 中被跳过 | `[RunStarted, ConversationMessage, RunCompleted]` | fold 只处理 Run-level 事件 | P0 |
| C-U27 | `CONVERSATION_*` 事件不污染 RunState | `[RunStarted, ConversationStarted, RunCompleted]` | RunState 字段不受影响 | P0 |
| C-U28 | fold 遇到未知事件类型不崩溃 | 混合已知 + 对话事件 | `fold_events()` 正常返回 | P1 |

### 3.2 集成测试

#### 文件: `tests/test_conversation_api.py`（新建，约 250 行）

##### 3.2.1 对话 CRUD API

| # | 测试用例 | 请求 | 期望响应 | 验证点 |
|---|---------|------|---------|--------|
| C-I1 | POST /api/v1/conversations — 创建 | `{}` | `201` + `conversation_id` | Event Store 有 `ConversationStarted` |
| C-I2 | POST /api/v1/conversations — 自定义标题 | `{"title":"My Chat"}` | `201` + `title="My Chat"` | conversations 表记录正确 |
| C-I3 | GET /api/v1/conversations — 空列表 | — | `200` + `{"conversations":[], "total":0}` | — |
| C-I4 | GET /api/v1/conversations — 有数据 | — | `200` + 列表含创建记录 | 按 updated_at 降序 |
| C-I5 | GET /api/v1/conversations/{id} — 存在 | — | `200` + `conversation` + `messages` | 消息列表正确 |
| C-I6 | GET /api/v1/conversations/{id} — 不存在 | fake_id | `404` | — |
| C-I7 | DELETE /api/v1/conversations/{id} | — | `200` + `{"success": true}` | 标记为 archived |
| C-I8 | PATCH /api/v1/conversations/{id} — 更新标题 | `{"title":"New"}` | `200` | conversations 表 title 更新 |

##### 3.2.2 发送消息

| # | 测试用例 | 前置条件 | 验证点 | 优先级 |
|---|---------|---------|--------|--------|
| C-I9 | POST messages → 创建 Run | 对话存在 | 返回 `run_id`, `conversation_id` | P0 |
| C-I10 | 同对话第 2 条消息 → 上下文注入 | 对话已有 1 轮 | `RunStarted.intent` 包含前一轮摘要 | P0 |
| C-I11 | 同对话第 4 条消息 → 只注入最近 3 轮 | 对话已有 3 轮 | intent 只含第 2-3 轮 | P0 |
| C-I12 | Run 完成后自动写 assistant 消息 | Run 执行完成 | Event Store 有 `ConversationMessage(role=assistant)` | P0 |
| C-I13 | Run 失败时也写 assistant 消息 | Run 失败 | 同上，content 含错误描述 | P0 |
| C-I14 | 对话不存在时发消息 | 无效 conversation_id | `404` | P0 |

##### 3.2.3 存量兼容

| # | 测试用例 | 前置 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| C-I15 | POST /api/v1/runs 无 conversation_id | — | 行为不变，`conversation_id=None` | P0 |
| C-I16 | POST /api/v1/runs 带 conversation_id | 对话存在 | Run 关联到对话 | P1 |
| C-I17 | GET /api/v1/runs/{id} 返回 conversation_id | Run 关联对话 | 响应含 `conversation_id: str` | P0 |
| C-I18 | 存量 341 项测试全通过 | — | 无回归 | P0 |

##### 3.2.4 WebSocket 扩展

| # | 测试用例 | 验证点 | 优先级 |
|---|---------|--------|--------|
| C-I19 | WS 事件含 `conversation_id` 字段 | 所有 Run 事件携带 | P0 |
| C-I20 | WS 订阅 conversation_id 维度 | 收到关联 Run 的事件 | P1 |

### 3.3 E2E 测试

#### 文件: `scripts/test_conversation_e2e.py`（新建）

##### 完整多轮对话流

| # | 场景 | 步骤 | 验证点 | 优先级 |
|---|------|------|--------|--------|
| C-E1 | 3 轮连续对话 | ① 创建对话 → ② 发"查天气" → ③ 等 Run 完成 → ④ 发"用中文总结" → ⑤ 等 Run 完成 → ⑥ 发"那明天呢" | 第②步 Run 独立，第④步 intent 含③的结果，第⑥步 intent 含③⑤的结果 | P0 |
| C-E2 | 跨对话隔离 | 对话 A 有 2 轮，对话 B 有 1 轮 | 对话 B 的第 2 条消息不应含对话 A 的内容 | P0 |
| C-E3 | 对话列表恢复 | 创建 3 个对话 → GET 列表 | 列表含全部 3 个，按时间降序 | P1 |

### 3.4 契约测试

| # | 测试用例 | 验证内容 | 优先级 |
|---|---------|---------|--------|
| C-C1 | OpenAPI Schema 含对话端点 | `paths` 中有 `/api/v1/conversations` 相关路径 | P0 |
| C-C2 | 请求/响应 Model 生成 TypeScript 类型 | 前端自动生成的类型正确 | P0 |
| C-C3 | `EventType` 前后端枚举同步 | 前端 enum 包含后端新类型 | P1 |

---

## 4. Phase 2 — 执行控制 + Fallback 测试

### 4.1 单元测试

#### 文件: `tests/test_commands.py`（新建，约 200 行）

##### 4.1.1 RunCommand 事件模型

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| CM-U1 | `RunCommandPayload` 合法命令 | `command="hard_abort"`, `reason="test"` | 序列化正确 | P0 |
| CM-U2 | `RunCommandPayload` 非法命令 | `command="invalid"` | Pydantic 校验失败 | P0 |
| CM-U3 | `RunCommandPayload` 默认值 | 只传必填 | `affected_tool=None`, `issued_by="monitor"` | P0 |
| CM-U4 | `RunCommandPayload` 所有 5 种命令 | each literal | 全部可序列化 | P1 |
| CM-U5 | `EventType.RUN_COMMAND` 在 PAYLOAD_MODEL_MAP 中 | — | `RUN_COMMAND → RunCommandPayload` | P0 |

##### 4.1.2 Scheduler 命令检查

| # | 测试用例 | 前置 | 验证 | 优先级 |
|---|---------|------|------|--------|
| CM-U6 | `_check_pending_commands` 无命令 | 事件流不含 RUN_COMMAND | 返回 `None` | P0 |
| CM-U7 | `_check_pending_commands` 有一个 hard_abort | 含 1 条 RUN_COMMAND | 返回 `"hard_abort"` | P0 |
| CM-U8 | `_check_pending_commands` 多个命令只处理最新的 | seq 10 和 seq 20 各一条 | 返回 seq 20 的命令 | P0 |
| CM-U9 | `_check_pending_commands` 处理过的命令不再返回 | 已处理 seq 20 | 返回 `None` | P0 |
| CM-U10 | `_check_pending_commands` 跳过已处理的命令 | `_last_processed_command_seq=10`, 事件流有 seq 5 和 seq 15 | 只返回 seq 15 | P0 |
| CM-U11 | `_check_pending_commands` 从空事件流调用 | 仅有非 RUN_COMMAND 事件 | 返回 `None` | P1 |

##### 4.1.3 FallbackKernel 解析

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| FK-U1 | `_parse_response` 含 tool_calls | mock response 有 tool_calls | 返回 `ThinkResult(tool_name=..., tool_args=...)` | P0 |
| FK-U2 | `_parse_response` 无 tool_calls | mock response 纯文本 | 返回 `ThinkResult(tool_name=None)` | P0 |
| FK-U3 | `build_tool_schemas` 给 FallbackKernel | tool_defs 列表 | 返回 OpenAI tools 格式 | P0 |
| FK-U4 | FallbackKernel 不依赖正则解析 | 工具输出含 `ARGS:` 等关键字 | 正常解析（不触发旧正则） | P1 |
| FK-U5 | FallbackKernel 接口兼容 | `think(intent, state, feedback=None)` | 与旧接口一致 | P0 |

### 4.2 集成测试

##### 4.2.1 Scheduler 命令处理

| # | 测试用例 | 步骤 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| CM-I1 | hard_abort → Run 立即终止 | ① Run 执行中 → ② 写入 RUN_COMMAND(hard_abort) → ③ 等待 | Run 状态为 FAILED，error="Hard aborted by monitor" | P0 |
| CM-I2 | soft_abort → 等待当前工具完成 | 同上 | 当前工具写入 TOOL_COMPLETED 后才终止 | P0 |
| CM-I3 | pause → Run 切换到 PAUSED | 同上 | Run 状态为 PAUSED，可 resume | P0 |
| CM-I4 | pause → resume → 继续执行 | pause 后写 resume | Run 继续执行 | P0 |
| CM-I5 | skip_tool → 跳过当前工具 | mock 一个长耗时工具 | 工具不执行，直接执行下一工具 | P1 |
| CM-I6 | lower_parallel → 减少并行度 | DAG 执行中写入 | 后续层并行度减少 | P1 |
| CM-I7 | 不存在的命令 → 忽略 | 写入未知 command | Run 继续正常执行 | P1 |

##### 4.2.2 Monitor 自动熔断

| # | 测试用例 | 步骤 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| CM-I8 | 连续 5 次 ToolFailed → hard_abort | Mock 5 次工具失败 | Run 在第 5 次失败后自动终止 | P0 |
| CM-I9 | Token 超 120% → soft_abort | 模拟巨大 token 消耗 | Run 在完成当前工具后终止 | P0 |
| CM-I10 | 执行时间超限 → hard_abort | 配置 max_duration=1s | Run 超时终止 | P1 |
| CM-I11 | 循环检测 6 次重复 → hard_abort | 连续 6 次相同签名 | Run 终止 | P1 |

##### 4.2.3 FallbackKernel 集成

| # | 测试用例 | 步骤 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| FK-I1 | FallbackKernel 走 tools API 路径 | MockLLMClient 返回含 tool_calls 的响应 | Scheduler 正常执行工具 | P0 |
| FK-I2 | FallbackKernel 降级路径不变 | LLM 返回 `<STOP>` | Scheduler 正常结束 Run | P0 |
| FK-I3 | FallbackKernel 含 feedback 参数 | 传入 feedback="注意失败率" | feedback 显示在 System Prompt 中 | P1 |

### 4.3 故障注入测试

| # | 测试用例 | 方法 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| CM-F1 | 写入 RUN_COMMAND 时 Event Store 断连 | Mock store.append_event 抛异常 | Scheduler 不崩溃，跳过命令检查 | P0 |
| CM-F2 | 同时收到 hard_abort 和 pause | 写入 2 条 RUN_COMMAND | hard_abort 优先 | P1 |
| CM-F3 | Scheduler 在处理命令时崩溃 | 在命令处理函数中抛异常 | `_check_pending_commands` 被 try/except 包裹 | P1 |
| CM-F4 | Monitor 熔断与 Operator 命令冲突 | Monitor 发 hard_abort，同时 operator 发 pause | 后写入者生效 | P2 |

---

## 5. Phase 3 — 上下文管理测试

### 5.1 单元测试

#### 文件: `tests/test_context_window.py`（新建，约 120 行）

##### 5.1.1 动态窗口计算

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| CW-U1 | 空状态 → 最小窗口 | `state` 无 thought/tool_results | `window=1` | P0 |
| CW-U2 | 少量 thought → 大窗口 | 2 个 thought，每个 100 tokens | `window >= 10` | P0 |
| CW-U3 | 大量 tool result → 小窗口 | 10 个 result，每个占 800 tokens | `window <= 2` | P0 |
| CW-U4 | token 预算变化影响窗口 | `max_tokens=4000` vs `max_tokens=16000` | 后者窗口更大 | P0 |
| CW-U5 | 窗口至少为 1 | 极端大 token 场景 | `window=1` | P0 |
| CW-U6 | `_compute_dynamic_window` 参数边界 | 0 或负值 | 返回 `1` | P1 |

##### 5.1.2 工具结果截断

| # | 测试用例 | 输入 | 期望输出 | 优先级 |
|---|---------|------|---------|--------|
| CW-U7 | http_request body >2000 chars | 3000 chars body | `body` 字段截断为 `2000 chars + "..."` | P0 |
| CW-U8 | http_request body <2000 chars | 500 chars body | 不截断 | P0 |
| CW-U9 | browser text_content >1000 chars | 2000 chars 文本 | `text_content` 截断为 1000 chars | P0 |
| CW-U10 | file_op content >500 chars | 1000 chars | `content` 截断为 500 chars | P0 |
| CW-U11 | 未知工具使用默认规则 | `custom_tool` 大输出 | 使用 `__default__` 规则 (1000 chars) | P0 |
| CW-U12 | SOFT_ERROR 截断 error 字段 | 大段错误文本 | `error` 截断为 500 chars | P1 |
| CW-U13 | 截断后保留关键字段 | http_request | 保留 `status_code` + `body` | P1 |
| CW-U14 | `truncate_tool_output` 输入 None | 工具返回 None | 返回 `{"error": "None"}` | P1 |
| CW-U15 | 截断规则表不可变 | 修改 `TRUNCATION_RULES` | 使用 deep copy | P1 |

### 5.2 组件测试

| # | 测试用例 | 方法 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| CW-C1 | think() 使用动态窗口 | MockAgentKernel 传入 state 有 10 轮 | 传给 LLM 的消息只有 `window` 轮 | P0 |
| CW-C2 | think() 集成截断 | tool_results 含大输出 | 传给 LLM 的输出已被截断 | P0 |
| CW-C3 | 截断不影响 Event Store | 大输出存入 Event Store | Event Store 保留完整数据 | P0 |
| CW-C4 | 动态窗口 + 截断协同 | 10 轮大输出 + 5 轮小输出 | 窗口动态调整，输出截断 | P1 |
| CW-C5 | 窗口变化不破坏已有测试 | 运行全部测试 | 回归通过 | P0 |

---

## 6. Phase 4 — 前端 UI 测试

### 6.1 组件测试

**框架**: Vitest + @testing-library/react

| # | 测试用例 | 组件 | 场景 | 验证点 | 优先级 |
|---|---------|------|------|--------|--------|
| FS-1 | `MessageBubble` 渲染 user 消息 | MessageBubble | `role="user"`, `content="hi"` | 显示用户头像 + "hi" | P0 |
| FS-2 | `MessageBubble` 渲染 assistant 消息 | MessageBubble | `role="assistant"`, `content="回答"` | 显示 Agent 头像 + "回答" | P0 |
| FS-3 | `ThinkingPanel` 默认折叠 | ThinkingPanel | 渲染思考内容 | 初始为折叠状态 | P0 |
| FS-4 | `ThinkingPanel` 可展开/折叠 | ThinkingPanel | 点击标题 | 展开/收起动画 | P0 |
| FS-5 | `ToolCallCard` 显示实时状态 | ToolCallCard | `status="running"` → `status="completed"` | 状态从"搜索中..."变为"✅ 完成" | P0 |
| FS-6 | `ToolCallCard` 显示失败 | ToolCallCard | `status="failed"` | 显示"❌ 失败 + 错误信息" | P0 |
| FS-7 | `ConfirmationCard` 确认/拒绝按钮 | ConfirmationCard | Approve/Deny 点击 | 调用回调函数 | P0 |
| FS-8 | `FinalAnswer` Markdown 渲染 | FinalAnswer | `content="# Hello\n**bold**"` | 渲染为 HTML | P1 |
| FS-9 | `PendingIndicator` 显示排队数 | PendingIndicator | `count=3` | 显示"还有 3 条消息等待执行" | P1 |
| FS-10 | `ConversationSidebar` 显示对话列表 | ConversationSidebar | 3 个对话 | 列表显示 + 排序 | P0 |
| FS-11 | `ConversationSidebar` 搜索 | ConversationSidebar | 输入搜索词 | 过滤列表 | P1 |
| FS-12 | `ConversationDrawer` 输入框始终可用 | ConversationDrawer | Run 执行中输入 | 输入框不 disabled | P0 |

### 6.2 集成测试

| # | 测试用例 | 场景 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| FI-1 | 发送消息 → 显示在消息列表中 | 用户输入 "hi" → 提交 | 消息列表中显示用户消息 | P0 |
| FI-2 | 收到 WS 事件 → 更新消息列表 | WebSocket 推送 agent 回答 | 消息列表追加 assistant 消息 | P0 |
| FI-3 | 排队输入 → 自动发送 | Run 运行时输入 → 等前一条完成 | 后一条自动发出 | P0 |
| FI-4 | 排队输入 → 取消排队 | 用户添加排队消息后点击取消 | 排队队列移除 | P1 |
| FI-5 | 切换对话 → 显示对应消息 | 从对话 A 切换到对话 B | 消息列表刷新为 B 的内容 | P0 |
| FI-6 | 创建新对话 → 清空消息列表 | 点击"+"按钮 | 消息列表为空 | P0 |

### 6.3 E2E 测试

| # | 测试用例 | 步骤 | 验证点 | 优先级 |
|---|---------|------|--------|--------|
| FE-1 | 完整前端对话流 | ① 打开页面 → ② 输入 "查天气" → ③ 等回答 → ④ 输入 "用中文总结" | ② 创建新对话，回答正确，④ 引用前一轮结果 | P0 |
| FE-2 | 刷新页面恢复对话 | 同上后 F5 | 对话列表存在，可继续聊天 | P0 |
| FE-3 | 多对话切换 | 创建 2 个对话，各发 1 条 | 切换时内容正确 | P0 |
| FE-4 | Run 运行时输入排队 | 发一个长任务，立即再发一条 | 第二条显示排队状态，自动执行 | P1 |

---

## 7. 回归测试

### 7.1 回归范围

| Phase | 受影响组件 | 回归文件 | 最低通过数 |
|-------|-----------|---------|-----------|
| Phase 1 | EventStore, API, fold, Scheduler | `test_event_store.py`, `test_api.py`, `test_fold.py`, `test_scheduler.py`, `test_kernel.py` | 341 项 |
| Phase 2 | Scheduler, Monitor, AgentKernel | `test_scheduler.py`, `test_monitoring.py`, `test_kernel.py` | 341 项 |
| Phase 3 | AgentKernel, ContextManager | `test_kernel.py`, `test_context_manager.py`, `test_scheduler.py` | 341 项 |
| Phase 4 | 前端（独立，不涉及后端） | 前端测试 | 不回溯后端 |

### 7.2 回归执行策略

- 每次提交后运行全部存量测试 + 新增 Phase 测试
- CI 配置 `pytest tests/ -v --tb=short`（全量运行）
- 单 Phase 交付前运行 3 次全量回归，确认 0 flaky

### 7.3 已知 flaky 测试标记

在 CI 报告中标记以下场景（若出现）：

```
@pytest.mark.flaky(reruns=3)
场景: 并发写入 seq 冲突时重试路径
原因: asyncio 调度不确定性
```

（当前项目无 flaky 标记，保持该状态。）

---

## 8. 覆盖率目标

### 8.1 覆盖率指标

| 组件 | 当前覆盖率 | 目标覆盖率 | 测量工具 |
|------|-----------|-----------|---------|
| `harness/models/conversation.py` | — | 95%+ | pytest-cov |
| `harness/storage/event_store.py` | ~85% | 90%+ | pytest-cov |
| `harness/api/routes.py`（对话部分） | — | 90%+ | pytest-cov |
| `harness/core/scheduler.py`（命令部分） | ~80% | 85%+ | pytest-cov |
| `harness/core/agent_kernel.py`（窗口+截断） | ~75% | 85%+ | pytest-cov |
| `harness/core/scheduler/fallback_kernel.py` | — | 90%+ | pytest-cov |

### 8.2 覆盖盲区提醒

| 盲区 | 风险 | 缓解 |
|------|------|------|
| Event Store 迁移代码（ALTER TABLE） | 生产环境迁移失败 | 手动测试 + 迁移脚本单元测试 |
| 前端 WebSocket 异常重连 | 断连后事件丢失 | 前端集成测试覆盖 |
| Scheduler 命令处理并发 | 多命令竞态 | 故障注入测试 CM-F3 |
| 跨对话上下文隔离 | 对话 A 污染对话 B | E2E 测试 C-E2 |

---

## 9. 测试执行计划

### 9.1 执行顺序

```
Phase 1 单元测试 ──→ Phase 1 集成测试 ──→ Phase 1 E2E ──→ 全量回归
         ↓
Phase 2 单元测试 ──→ Phase 2 集成测试 ──→ Phase 2 故障注入 ──→ 全量回归
         ↓
Phase 3 单元测试 ──→ Phase 3 组件测试 ──→ 全量回归
         ↓
Phase 4 组件测试 ──→ Phase 4 集成测试 ──→ Phase 4 E2E ──→ 全量回归
```

### 9.2 测试命令

```bash
# 全量测试（开发期）
pytest tests/ -v --tb=short

# 指定 Phase 测试
pytest tests/test_conversation.py tests/test_conversation_api.py -v

# 带覆盖率
pytest tests/ --cov=harness --cov-report=term-missing

# E2E 测试（需完整环境）
python scripts/test_conversation_e2e.py

# 前端测试
cd frontend && npx vitest run

# 契约测试（OpenAPI 类型检查）
python scripts/generate_openapi.py  # 生成 OpenAPI schema
```

### 9.3 Phase 交付闸门

| 闸门 | Phase 1 | Phase 2 | Phase 3 | Phase 4 |
|------|---------|---------|---------|---------|
| 新增单元测试通过 | ≥25 项 | ≥15 项 | ≥15 项 | ≥12 项 |
| 新增集成测试通过 | ≥15 项 | ≥15 项 | ≥5 项 | ≥6 项 |
| E2E 测试通过 | ≥3 项 | — | — | ≥4 项 |
| 存量回归通过 | 341 项 | 341+Δ | 341+Δ | 不涉及 |
| 覆盖率达标 | 95% (conversation.py) | 85% (scheduler 命令) | 85% (agent_kernel) | 前端自定义指标 |

---

## A. 附录 — 测试用例清单

### Phase 1: 多轮对话后端（41 项）

| 模块 | 文件 | 用例数 | 覆盖范围 |
|------|------|--------|---------|
| 数据模型 | `test_conversation.py` | 6 | Conversation/CreateRequest/Detail 序列化 |
| 事件模型 | `test_conversation.py` | 5 | Payload 序列化 + PAYLOAD_MODEL_MAP |
| 上下文构建 | `test_conversation.py` | 6 | 摘要构建/截断/格式 |
| EventStore 查询 | `test_conversation.py` | 8 | CRUD + 排序 + 过滤 |
| fold 兼容 | `test_conversation.py` | 3 | 对话事件不污染 RunState |
| API CRUD | `test_conversation_api.py` | 8 | 全部 6 个端点 |
| API send_message | `test_conversation_api.py` | 6 | 上下文注入 + assistant 消息 + 404 |
| 存量兼容 | `test_conversation_api.py` | 4 | 无 conversation_id 行为不变 |
| WS 扩展 | `test_conversation_api.py` | 2 | conversation_id 字段 |
| E2E | `scripts/test_conversation_e2e.py` | 3 | 3 轮对话 + 隔离 + 列表 |

### Phase 2: 执行控制 + Fallback（35 项）

| 模块 | 文件 | 用例数 | 覆盖范围 |
|------|------|--------|---------|
| RunCommand 模型 | `test_commands.py` | 5 | Payload 序列化 + 校验 |
| 命令检查 | `test_commands.py` | 7 | _check_pending_commands 全路径 |
| Scheduler 命令 | `test_commands.py` | 7 | 5 种命令处理 |
| Monitor 熔断 | `test_commands.py` | 4 | 自动触发条件 |
| 故障注入 | `test_commands.py` | 4 | EventStore 异常 + 并发 |
| FallbackKernel | `test_fallback_kernel.py` | 5 | 解析 + 集成 |

### Phase 3: 上下文管理（20 项）

| 模块 | 文件 | 用例数 | 覆盖范围 |
|------|------|--------|---------|
| 动态窗口 | `test_context_window.py` | 6 | 窗口计算全路径 |
| 工具截断 | `test_context_window.py` | 9 | 各工具截断规则 |
| 组件集成 | `test_context_window.py` | 5 | think() 集成 |

### Phase 4: 前端 UI（22 项）

| 模块 | 文件 | 用例数 | 覆盖范围 |
|------|------|--------|---------|
| 组件测试 | 各 `.test.tsx` | 12 | MessageBubble/ThinkingPanel/ToolCallCard 等 |
| 集成测试 | 前端测试 | 6 | 发送/WS/排队/切换 |
| E2E | `scripts/test_conversation_frontend.py` | 4 | 完整前端流 |

**合计新增**: ~118 项（Phase 1: 41 + Phase 2: 35 + Phase 3: 20 + Phase 4: 22）

---

*本测试计划基于 PRD_v1.0.md、conversation_dev_plan.md、现有测试模式和架构文档编写。*
*用例编号规则: C=Conversation, CM=Command, CW=ContextWindow, FK=FallbackKernel, FS=Frontend, FI=FrontendIntegration, FE=FrontendE2E, 后缀 U=Unit, I=Integration, E=E2E, F=Fault, C=Component.*
