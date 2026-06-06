"""Production entrypoint — assembles all components for a live agent run.

全流程预览（当你 POST /api/v1/runs 时发生的事）:

  ① create_run 端点收到请求
  ② 写入 RunStarted 事件到 Event Store
  ③ start_run() 创建 AgentLoopScheduler 并 asyncio.create_task 启动
  ④ Scheduler 在后台开始 think→act→observe 循环:
       THINK   → LLMAgentKernel (Qwen) 或 MockAgentKernel
       ACT     → ToolExecutor 执行工具（走 Guardrails + 幂等校验）
       OBSERVE → 结果写回 Event Store
  ⑤ RunMonitor 监听事件流 → 异常检测 → FeedbackInjected
  ⑥ Event Store 每写入一条事件，自动推给 WebSocket 客户端
  ⑦ 前端 RunDetail 页面实时收到推过来的事件

运行方式:
    uvicorn harness.api.serve:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from harness.api.app import app  # noqa: F401
from harness.api.deps import HarnessAPI, configure_hapi
from harness.core.agent_kernel import LLMAgentKernel, MockAgentKernel
from harness.core.logger import guard_logger
from harness.core.context_manager import ContextManager
from harness.core.llm_client import OpenAILLMClient
from harness.core.scheduler import SchedulerConfig, ThinkResult
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition
from harness.monitoring.run_monitor import RunMonitor
from harness.storage.event_store import EventStore
from harness.tools.browser_tool import BROWSER_DEF, browser_fn
from harness.tools.executor import ToolExecutor
from harness.tools.file_op import FILE_OP_DEF, file_op_fn, set_sandbox_root
from harness.tools.http_request import HTTP_REQUEST_DEF, http_request_fn
from harness.tools.mcp_call import MCP_CALL_DEF, mcp_call_fn

_logger = guard_logger("serve")


class _RoleFormatter(logging.Formatter):
    """Prepends a role tag based on the logger namespace:
      [AGENT  ] — harness.agent.*   (think/act/observe/LLM/execution)
      [GUARD  ] — harness.guard.*   (guardrails/idempotency/breaker/store/context)
      [MONITOR] — harness.monitor.* (monitoring/feedback injection)
    """

    _ROLE_MAP = {
        "harness.agent.": "AGENT",
        "harness.guard.": "GUARD",
        "harness.monitor.": "MONITOR",
    }

    def format(self, record: logging.LogRecord) -> str:
        for prefix, role in self._ROLE_MAP.items():
            if record.name.startswith(prefix):
                record.role = role
                break
        else:
            record.role = record.name.split(".")[-1].upper()[:7]
        return super().format(record)


_handler = logging.StreamHandler()
_handler.setFormatter(_RoleFormatter(
    fmt="%(asctime)s [%(role)-7s] %(message)s",
    datefmt="%H:%M:%S",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

# 独立控制三类日志等级（默认均为 INFO）：
#   logging.getLogger("harness.agent").setLevel(logging.DEBUG)
#   logging.getLogger("harness.guard").setLevel(logging.WARNING)
#   logging.getLogger("harness.monitor").setLevel(logging.DEBUG)

# ── 加载 .env ──────────────────────────────────────────────

env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

USE_REAL_LLM = "LLM_API_KEY" in os.environ

# ── 1. 创建基础设施 ─────────────────────────────────────────

store = EventStore(".harness.db")
executor = ToolExecutor(store)

# file_op 沙箱目录
sandbox_root = Path(__file__).parent.parent / ".sandbox_tmp"
sandbox_root.mkdir(exist_ok=True)
set_sandbox_root(str(sandbox_root.resolve()))


# ── 2. 装配 Agent Kernel ────────────────────────────────────

if USE_REAL_LLM:
    _logger.info("Real LLM mode: using %s", os.environ.get("LLM_MODEL_NAME", "?"))
    client = OpenAILLMClient(
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ.get("LLM_MODEL_NAME", "qwen3.7-max-preview"),
        base_url=os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    api = HarnessAPI(store=store, executor=executor)
    api.kernel_factory = lambda: LLMAgentKernel(client)
    api.tool_defs = [HTTP_REQUEST_DEF, FILE_OP_DEF, BROWSER_DEF, MCP_CALL_DEF]
    api.tool_fns = {
        "http_request": http_request_fn,
        "file_op": file_op_fn,
        "browser": browser_fn,
        "mcp_call": mcp_call_fn,
    }
    api.scheduler_config = SchedulerConfig(max_iterations=20)

    monitor = RunMonitor(store)
    monitor.attach()
    api.monitor = monitor
else:
    _logger.info("Mock mode: using MockAgentKernel with echo tool")

    async def echo_tool(input: dict) -> dict:
        import time
        return {"echo": input, "ts": time.time()}

    echo_def = ToolDefinition(
        name="echo",
        description="Echo back what you send",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object"},
        idempotency_key_fields=["msg"],
        side_effects=[SideEffect.WRITE],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )

    api = HarnessAPI(store=store, executor=executor)
    api.kernel_factory = lambda: MockAgentKernel([
        *[ThinkResult(thought=f"iteration_{i}", tool_name="echo", tool_input={"msg": f"msg_{i}"})
          for i in range(105)],
        ThinkResult(thought="All 105 iterations complete", tool_name=None),
    ])
    api.tool_defs = [echo_def]
    api.tool_fns = {"echo": echo_tool}
    api.scheduler_config = SchedulerConfig(max_iterations=150)


# ── 3. 装配 ContextManager ─────────────────────────────────

cm = ContextManager(store, llm_client=None, token_limit=1000, checkpoint_interval=10)
api.context_manager = cm


# ── 4. 注册广播 + 写入 DI ──────────────────────────────────

api.wire_broadcast()
configure_hapi(api)
