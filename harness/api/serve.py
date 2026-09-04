"""Production entrypoint — assembles all components for a live agent run.

全流程预览（当你 POST /api/v1/runs 时发生的事）:

  ① create_run 端点收到请求
  ② 写入 RunStarted 事件到 Event Store
  ③ start_run() 创建 PlanningExecutorScheduler 并 asyncio.create_task 启动
  ④ Scheduler 在后台开始 plan→execute→revise 循环:
       PLAN    → Planner (LLM) 生成 DAG Plan
       EXECUTE → DagExecutor 并行执行步骤
       REVISE  → Planner 检查结果，决定是否继续
  ⑤ RunMonitor 监听事件流 → 异常检测 → FeedbackInjected
  ⑥ Event Store 每写入一条事件，自动推给 WebSocket 客户端
  ⑦ 前端 RunDetail 页面实时收到推过来的事件

运行方式:
    uvicorn harness.api.serve:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from harness.api.app import app  # noqa: F401
from harness.api.deps import HarnessAPI, configure_hapi
from harness.api.loop import configure_event_loop_policy
from harness.core.context_manager import ContextManager
from harness.core.llm_client import MockLLMClient, OpenAILLMClient
from harness.core.logger import guard_logger
from harness.core.scheduler import SchedulerConfig
from harness.models.tools import SideEffect
from harness.monitoring import LangfuseTracer
from harness.monitoring.run_monitor import RunMonitor
from harness.storage.event_store import EventStore
from harness.tools.base import BaseTool
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

# Windows: 必须在 uvicorn 创建任何 event loop 之前设置 Proactor policy，
# 否则 reload 模式下 loop 建立早于 app import，Docker/playwright 子进程会抛
# NotImplementedError（JAGENT-2026-P1-13 Bug 5 复发）。该调用在模块 import 时执行。
configure_event_loop_policy()

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


_fmt = _RoleFormatter(
    fmt="%(asctime)s [%(role)-7s] %(message)s",
    datefmt="%H:%M:%S",
)

# S11 (问题十): Windows 控制台统一 UTF-8 — 日志文件与事件存储是权威，
# 终端只是显示层。stdout/stderr 按 UTF-8 重配置，避免中文意图乱码。
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

# Console output
_handler = logging.StreamHandler()
_handler.setFormatter(_fmt)

# File output with rotation
_log_dir = Path(os.environ.get("HARNESS_LOG_DIR", "data/logs"))
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    filename=str(_log_dir / "harness.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(_fmt)

# Only log harness.* namespaces to file (skip uvicorn/3rd-party noise)
_file_handler.addFilter(logging.Filter("harness"))

logging.basicConfig(level=logging.INFO, handlers=[_handler, _file_handler], force=True)

# 独立控制三类日志等级（默认均为 INFO）：
#   logging.getLogger("harness.agent").setLevel(logging.DEBUG)
#   logging.getLogger("harness.guard").setLevel(logging.WARNING)
#   logging.getLogger("harness.monitor").setLevel(logging.DEBUG)

# ── 加载 .env ──────────────────────────────────────────────

env_path = Path(__file__).parent.parent.parent / ".env"
if env_path.exists():
    # utf-8-sig 自动剥离 UTF-8 BOM（\ufeff）。Windows 记事本保存 .env 常带 BOM，
    # 若按普通 utf-8 读取，BOM 会粘在第一个 key（如 LLM_API_KEY）上导致
    # USE_REAL_LLM 判定失败、静默降级到 Mock 模式。
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

USE_REAL_LLM = "LLM_API_KEY" in os.environ


class _EchoTool(BaseTool):
    """Mock 模式占位工具 — 经 register_tool 走与 real 工具一致的受信 invoker 路径。

    ADR-010 D-07：工具注册唯一公开入口为 register_tool(BaseTool)。mock 模式不再
    直调私有 _register 注入裸 callable，避免绕过契约合成与依赖注入（D-03）。
    """

    name = "echo"
    description = "Echo back what you send"
    input_schema = {"type": "object", "properties": {"msg": {"type": "string"}}}
    output_schema = {"type": "object"}
    idempotency_key_fields = ["msg"]
    side_effects = [SideEffect.WRITE]
    timeout_ms = 5000

    async def run(self, input: dict) -> dict:
        import time

        return {"echo": input, "ts": time.time()}

# ── 1. 创建基础设施 ─────────────────────────────────────────

store = EventStore(os.environ.get("HARNESS_DB_PATH", ".harness.db"))
executor = ToolExecutor(store)

# ── 2. 装配工具定义 + LLM ──────────────────────────────────

api = HarnessAPI(store=store, executor=executor)

if USE_REAL_LLM:
    _logger.info("Real LLM mode: using %s", os.environ.get("LLM_MODEL_NAME", "?"))
    client = OpenAILLMClient(
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ.get("LLM_MODEL_NAME", "qwen3.7-max-preview"),
        base_url=os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )
    api.scheduler_config = SchedulerConfig(
        max_iterations=20,
        # Q-07 全局 run 预算（唯一 deadline）。默认 0=禁用会令 revise/plan 阶段的
        # LLM 调用无任何超时兜底（qwen3.7-max 高延迟下单次响应可达 100s+，3 次
        # revise 可无界拖长，见 P1-13 15.8）。默认 10min，watchdog 到期强制
        # RunFailed("run_timed_out")，避免 run 无限等待 LLM。
        run_timeout_ms=int(os.environ.get("HARNESS_RUN_TIMEOUT_MS", "600000")),
    )
    monitor = RunMonitor(store)
    monitor.attach()
    api.monitor = monitor
else:
    _logger.info("Mock mode: MockLLMClient with echo tool")

    client = MockLLMClient(
        responses=[
            json.dumps(
                {
                    "steps": [
                        {"id": "s1", "tool": "echo", "input": {"msg": "hello"}, "dependencies": []},
                        {"id": "s2", "tool": "echo", "input": {"msg": "world"}, "dependencies": ["s1"]},
                    ]
                }
            ),
            json.dumps({"steps": []}),
        ]
    )
    api.scheduler_config = SchedulerConfig(max_iterations=150)

# 注册工具到 ToolRegistry（ADR-010 D-07：register_tool 为唯一公开入口）。
# real 模式 4 个工具 + mock 模式 echo 均为 BaseTool，统一经 register_tool 注册，
# 走同一受信 invoker 路径（D-03 依赖注入）；生产代码不再直调私有 _register。
registry = ToolRegistry()
if USE_REAL_LLM:
    from harness.tools.browser_tool import BrowserTool
    from harness.tools.file_op import FileOpTool
    from harness.tools.http_request import HttpRequestTool
    from harness.tools.mcp_call import McpCallTool

    for tool in (FileOpTool(), HttpRequestTool(), BrowserTool(), McpCallTool()):
        registry.register_tool(tool)
else:
    registry.register_tool(_EchoTool())
api.registry = registry
# ADR-010 §8.1：BaseScheduler 构造签名不变，tool_defs/tool_fns 从 registry 派生。
api.tool_defs = registry.list_tool_defs()
api.tool_fns = registry.list_tool_fns()
api.llm_client = client


# ── 3. 装配 ContextManager ─────────────────────────────────

cm = ContextManager(store, llm_client=client if USE_REAL_LLM else None, token_limit=3000, checkpoint_interval=10)
api.context_manager = cm


# ── 4. 初始化 LangfuseTracer ─────────────────────────────────

tracer = LangfuseTracer()
api.tracer = tracer
_logger.info("Langfuse tracing: %s", "ENABLED" if tracer.enabled else "DISABLED")


# ── 5. 注册广播 + 写入 DI ─────────────────────────────────────

api.wire_broadcast()
configure_hapi(api)


# ── 6. 启动入口（S11 问题九：reload 目录限定源码，排除运行时产物）──────


def main() -> None:
    """Development entrypoint — `python -m harness.api.serve`.

    使用 `--reload-dir` 限定源码目录（harness + frontend/src），
    排除 data/、*.db、日志、workspace、缓存，避免 reload 监听风暴
    （reload_stderr.log 每秒 `1 change detected` 的根因）。
    """
    import atexit

    import uvicorn

    # ADR-010 D-08: 进程退出时统一关闭工具层共享资源。
    def _shutdown_tools() -> None:
        import asyncio

        from harness.tools import close_tools

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return
        if loop.is_running():
            loop.create_task(close_tools())
        else:
            loop.run_until_complete(close_tools())

    atexit.register(_shutdown_tools)
    uvicorn.run(
        "harness.api.serve:app",
        reload=True,
        reload_dirs=["harness", "frontend/src"],
        host="0.0.0.0",
        port=int(os.environ.get("HARNESS_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
