"""Production entrypoint — assembles all components for a live agent run.

全流程预览（当你 POST /api/v1/runs 时发生的事）:

  ① create_run 端点收到请求
  ② 写入 RunStarted 事件到 Event Store
  ③ start_run() 创建 AgentLoopScheduler 并 asyncio.create_task 启动
  ④ Scheduler 在后台开始 think→act→observe 循环:
       THINK   → MockAgentKernel 返回预定义的 thought + tool_choice
       ACT     → ToolExecutor 执行工具（走 Guardrails + 幂等校验）
       OBSERVE → 结果写回 Event Store
  ⑤ Event Store 每写入一条事件，自动调用 registered callbacks
  ⑥ HarnessAPI.wire_broadcast() 注册的回调把事件推给 WebSocket 客户端
  ⑦ 前端 RunDetail 页面实时收到推过来的事件

运行方式:
    uvicorn harness.api.serve:app --reload --host 0.0.0.0 --port 8000

验证链路:
    # 终端 1: 启动服务
    uvicorn harness.api.serve:app --reload --port 8000

    # 终端 2: 创建 run，观察事件自动产生
    curl -X POST http://localhost:8000/api/v1/runs -H "Content-Type: application/json" -d '{"intent":"count to 3"}'

    # 查看事件流
    curl http://localhost:8000/api/v1/runs/{run_id}/events

    # 浏览器打开 http://localhost:3000 → Run 详情页观察 WebSocket 实时推送
"""

from __future__ import annotations

from harness.api.app import app  # noqa: F401 — uvicorn 通过这个变量找到 ASGI app
from harness.api.deps import HarnessAPI, configure_hapi
from harness.core.agent_kernel import MockAgentKernel
from harness.core.scheduler import ThinkResult
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor

# ── 1. 创建基础设施 ─────────────────────────────────────────

store = EventStore(".harness.db")
executor = ToolExecutor(store)


# ── 2. 注册 Mock Agent ──────────────────────────────────────
# 预定义 3 轮工具调用 + 1 次停止，模拟一个完整的 agent 任务
# 真实运行时这里换成 LLMAgentKernel(LLMClient(api_key=...))

async def echo_tool(input: dict) -> dict:
    """一个简单的回显工具：返回你输入的内容 + 一个时间戳。

    真实工具会读写文件 / 调用 HTTP / 操作浏览器——但执行路径完全相同：
    SchemaGuardrail → 幂等校验 → Sandbox.invoke() → 事件写回。
    """
    import time
    return {"echo": input, "ts": time.time()}

echo_def = ToolDefinition(
    name="echo",
    description="Echo back what you send",
    input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
    output_schema={"type": "object"},
    idempotency_key_fields=["msg"],   # 同样的 msg → 幂等命中，不重复执行
    side_effects=[SideEffect.WRITE],
    timeout_ms=5000,
    retry_policy=RetryPolicy(),
)

# ── 3. 装配 HarnessAPI ──────────────────────────────────────
# 注意：kernel_factory 必须是工厂（每次返回 新  MockAgentKernel），不能是单例。
# MockAgentKernel 内部有 _idx 状态追踪响应进度，共享实例会导致第二个 run
# 的 _idx 越界 → 立即 tool_name=None → 只有 3 个事件就 RunCompleted。
# 详细说明见 harness/core/agent_kernel.py 的类文档注释。

api = HarnessAPI(store=store, executor=executor)
api.kernel_factory = lambda: MockAgentKernel([
    ThinkResult(thought="Step 1: echo hello", tool_name="echo", tool_input={"msg": "hello"}),
    ThinkResult(thought="Step 2: echo world", tool_name="echo", tool_input={"msg": "world"}),
    ThinkResult(thought="Step 3: echo done", tool_name="echo", tool_input={"msg": "done"}),
    ThinkResult(thought="All steps complete", tool_name=None),
])
api.tool_defs = [echo_def]
api.tool_fns = {"echo": echo_tool}

# 注册 EventStore 写入回调：每次新事件入库，自动推给 WebSocket 客户端
api.wire_broadcast()

# 写入 DI（端点上用 Depends(get_hapi) 获取这个实例）
configure_hapi(api)
