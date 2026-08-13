"""Demo script: shows the complete Harness data flow with all logging."""

# Imports follow the local source-path bootstrap below.
# ruff: noqa: E402

import asyncio
import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    stream=sys.stdout,
    datefmt="%H:%M:%S",
)

sys.path.insert(0, ".")

from harness.core.agent_kernel import MockAgentKernel
from harness.core.context_manager import ContextManager
from harness.core.scheduler import SchedulerConfig, ThinkResult
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition
from harness.monitoring.run_monitor import RunMonitor
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


async def main():
    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)

    monitor = RunMonitor(store, max_tokens=50, token_warning_ratio=0.5)
    monitor.attach()

    cm = ContextManager(store, token_limit=100, compression_threshold_ratio=0.5, checkpoint_interval=5)

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

    async def echo_fn(input: dict) -> dict:
        import time

        return {"echo": input, "ts": time.time()}

    responses = [
        ThinkResult(thought="搜索文件", tool_name="echo", tool_input={"msg": "hello"}),
        ThinkResult(thought="分析结果", tool_name="echo", tool_input={"msg": "world"}),
        ThinkResult(thought="任务完成"),
    ]

    from harness.core.scheduler import AgentLoopScheduler

    kernel = MockAgentKernel(responses)
    scheduler = AgentLoopScheduler(
        store=store,
        executor=executor,
        kernel=kernel,
        tool_defs=[echo_def],
        tool_fns={"echo": echo_fn},
        config=SchedulerConfig(max_iterations=10),
        context_manager=cm,
        monitor=monitor,
    )

    print("\n" + "=" * 60)
    print("  Harness 完整数据流演示")
    print("=" * 60)

    result = await scheduler.run("demo-run", "搜索文件并告诉我内容")

    print("\n" + "=" * 60)
    print("  最终状态")
    print("=" * 60)
    print(f"  status         : {result.status.value}")
    print(f"  seq            : {result.seq}")
    print(f"  thought_history: {len(result.thought_history)} 条")
    print(f"  tool_results   : {len(result.tool_results)} 条")
    print(f"  feedbacks      : {len(result.feedbacks)} 条")
    print(f"  summary        : {result.summary}")

    print("\n" + "=" * 60)
    print("  所有事件")
    print("=" * 60)
    events = await store.get_events("demo-run")
    for e in events:
        print(
            f"  seq={e.seq:2d}  type={e.event_type.value:<25s}  "
            f"payload={json.dumps(e.payload, ensure_ascii=False)[:60]}"
        )

    await store.close()


asyncio.run(main())
