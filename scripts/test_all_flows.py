"""Comprehensive data-flow test: Normal / Error / Monitor flows with real LLM + real tools.

Usage:
  cd D:\\Project\\JAgent
  .\\.venv\\Scripts\\Activate.ps1
  python scripts\test_all_flows.py
"""

# Imports follow the local source-path and dotenv bootstrap below.
# ruff: noqa: E402

import asyncio
import json
import logging
import os
import sys
import traceback

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from harness.core.agent_kernel import LLMAgentKernel, MockAgentKernel
from harness.core.context_manager import ContextManager
from harness.core.scheduler import AgentLoopScheduler, SchedulerConfig, ThinkResult
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition
from harness.monitoring.run_monitor import RunMonitor
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor

# ── Helpers ──────────────────────────────────────────────────────────────


def _dump(label: str, state, events):
    print(f"\n  {'=' * 56}")
    print(f"  {label}")
    print(f"  {'=' * 56}")
    print(f"  Status       : {state.status.value}")
    print(f"  Events       : {len(events)} total (seq 1 → {state.seq})")
    print(f"  Thoughts     : {len(state.thought_history)}")
    print(f"  Tool results : {len(state.tool_results)}")
    print(f"  Feedbacks    : {len(state.feedbacks)}")
    for e in events:
        payload = json.dumps(e.payload, ensure_ascii=False, default=str)
        ik = f" ik={e.idempotency_key[:12]}…" if e.idempotency_key else ""
        print(f"  [{e.seq:2d}] {e.event_type.value:<25s} {payload[:70]}{ik}")


# ── Scenario 1: Normal Flow ──────────────────────────────────────────────


async def scenario_normal(api_key: str, model: str, base_url: str):
    print("\n" + "█" * 58)
    print("█" + "  SCENARIO 1: Normal Flow (real LLM + successful tool)".center(56) + "█")
    print("█" * 58)

    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)
    cm = ContextManager(store, token_limit=200, compression_threshold_ratio=0.5, checkpoint_interval=20)
    monitor = RunMonitor(store, max_tokens=200, token_warning_ratio=0.5)
    monitor.attach()

    from harness.core.llm_client import OpenAILLMClient

    client = OpenAILLMClient(api_key=api_key, model=model, base_url=base_url)
    kernel = LLMAgentKernel(client)

    echo_def = ToolDefinition(
        name="echo",
        description="Echo back whatever message you send. Use this to send messages and confirm outputs.",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string", "description": "Message to echo back"}},
        },
        output_schema={"type": "object"},
        idempotency_key_fields=["msg"],
        side_effects=[SideEffect.WRITE],
        timeout_ms=10000,
        retry_policy=RetryPolicy(),
    )

    async def echo_fn(input_: dict) -> dict:
        return {"echo": input_, "status": "ok"}

    scheduler = AgentLoopScheduler(
        store=store,
        executor=executor,
        kernel=kernel,
        tool_defs=[echo_def],
        tool_fns={"echo": echo_fn},
        config=SchedulerConfig(max_iterations=5),
        context_manager=cm,
        monitor=monitor,
    )

    state = await scheduler.run("normal-flow", "Echo the phrase 'hello world' back to me")
    events = await store.get_events("normal-flow")
    _dump("NORMAL FLOW — RESULT", state, events)

    assert state.status.value == "completed", f"Expected completed, got {state.status.value}"
    has_tool = any(e.event_type.value == "ToolCompleted" for e in events)
    assert has_tool, "Normal flow: expected at least one ToolCompleted event"
    print("  └─ ✅ Normal flow PASSED (completed + tool executed)")
    return state, events


# ── Scenario 2: Error Flow ───────────────────────────────────────────────


async def scenario_error(api_key: str, model: str, base_url: str):
    print("\n" + "█" * 58)
    print("█" + "  SCENARIO 2: Error Flow (real LLM + failing tool)".center(56) + "█")
    print("█" * 58)

    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)
    cm = ContextManager(store, token_limit=200, compression_threshold_ratio=0.5, checkpoint_interval=20)
    monitor = RunMonitor(store, max_tokens=200, token_warning_ratio=0.5)
    monitor.attach()

    from harness.core.llm_client import OpenAILLMClient

    client = OpenAILLMClient(api_key=api_key, model=model, base_url=base_url)
    kernel = LLMAgentKernel(client)

    fail_def = ToolDefinition(
        name="fail_on_demand",
        description="ALWAYS fails when called. Use this tool to test error handling — it never succeeds.",
        input_schema={
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Reason for failure"}},
        },
        output_schema={"type": "object"},
        idempotency_key_fields=["reason"],
        side_effects=[SideEffect.WRITE],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )

    async def fail_fn(input_: dict) -> dict:
        raise RuntimeError(f"Intentional failure: {input_.get('reason', 'no reason')}")

    scheduler = AgentLoopScheduler(
        store=store,
        executor=executor,
        kernel=kernel,
        tool_defs=[fail_def],
        tool_fns={"fail_on_demand": fail_fn},
        config=SchedulerConfig(max_iterations=5, max_consecutive_failures=3),
        context_manager=cm,
        monitor=monitor,
    )

    state = await scheduler.run("error-flow", "Call the fail_on_demand tool to test error handling")
    events = await store.get_events("error-flow")
    _dump("ERROR FLOW — RESULT", state, events)

    has_failed = any(e.event_type.value == "ToolFailed" for e in events)
    assert has_failed, "Error flow: expected at least one ToolFailed event"
    print("  └─ ✅ Error flow PASSED (ToolFailed event present)")
    return state, events


# ── Scenario 3: Monitor Flow ─────────────────────────────────────────────


async def scenario_monitor():
    print("\n" + "█" * 58)
    print("█" + "  SCENARIO 3: Monitor Flow (sequential failures → feedback)".center(56) + "█")
    print("█" * 58)

    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)
    cm = ContextManager(store, token_limit=200, compression_threshold_ratio=0.8, checkpoint_interval=20)
    monitor = RunMonitor(store, max_tokens=50, token_warning_ratio=0.8)
    monitor.attach()

    fail_def = ToolDefinition(
        name="always_fail",
        description="This tool always fails. It is designed for testing error monitoring.",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object"},
        idempotency_key_fields=["msg"],
        side_effects=[SideEffect.WRITE],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )

    async def always_fail_fn(input_: dict) -> dict:
        raise RuntimeError(f"Intentional failure: {input_}")

    # Mock kernel pre-programmed: 3 consecutive failures → stop
    responses = [
        ThinkResult(
            thought="Let me test the failing tool", token_count=5, tool_name="always_fail", tool_input={"msg": "test1"}
        ),
        ThinkResult(thought="Let me try again", token_count=5, tool_name="always_fail", tool_input={"msg": "test2"}),
        ThinkResult(thought="One more attempt", token_count=5, tool_name="always_fail", tool_input={"msg": "test3"}),
        ThinkResult(thought="Too many failures, stopping now", token_count=5),
    ]
    kernel = MockAgentKernel(responses)

    scheduler = AgentLoopScheduler(
        store=store,
        executor=executor,
        kernel=kernel,
        tool_defs=[fail_def],
        tool_fns={"always_fail": always_fail_fn},
        config=SchedulerConfig(max_iterations=10, max_consecutive_failures=5),
        context_manager=cm,
        monitor=monitor,
    )

    state = await scheduler.run("monitor-flow", "Test monitoring feedback injection")
    events = await store.get_events("monitor-flow")
    _dump("MONITOR FLOW — RESULT", state, events)

    feedback_events = [e for e in events if e.event_type.value == "FeedbackInjected"]
    assert len(feedback_events) >= 1, f"Monitor flow: expected ≥1 FeedbackInjected event, got {len(feedback_events)}"
    print(f"  └─ ✅ Monitor flow PASSED ({len(feedback_events)} FeedbackInjected event(s))")
    return state, events


# ── Main ─────────────────────────────────────────────────────────────────


async def main():
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL_NAME", "qwen3.7-max-preview")
    base_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    if not api_key:
        os.path.join(os.path.dirname(__file__), os.pardir, ".env")
        print("❌ LLM_API_KEY not set in .env — create .env with:")
        print("   LLM_API_KEY=sk-xxx")
        print("   LLM_MODEL_NAME=qwen3.7-max-preview")
        sys.exit(1)

    print(f"  LLM: {model} @ {base_url}")
    print(f"  Key: {api_key[:8]}…{api_key[-4:]}")
    print()

    results = {}

    try:
        print("─" * 58)
        results["normal"] = await scenario_normal(api_key, model, base_url)
    except Exception as e:
        print(f"  ❌ Normal flow FAILED: {e}")
        traceback.print_exc()
        results["normal"] = None

    try:
        results["error"] = await scenario_error(api_key, model, base_url)
    except Exception as e:
        print(f"  ❌ Error flow FAILED: {e}")
        traceback.print_exc()
        results["error"] = None

    try:
        results["monitor"] = await scenario_monitor()
    except Exception as e:
        print(f"  ❌ Monitor flow FAILED: {e}")
        traceback.print_exc()
        results["monitor"] = None

    print("\n" + "=" * 58)
    print("  SUMMARY")
    print("=" * 58)
    for name, result in results.items():
        status = "✅ PASSED" if result else "❌ FAILED"
        r = result[1] if result else []
        n = len(r)
        t = sum(1 for e in r if e.event_type.value in ("ToolCompleted", "ToolFailed"))
        f = sum(1 for e in r if e.event_type.value == "FeedbackInjected")
        print(f"  {name:<12s} {status}  ({n} events, {t} tool results, {f} feedbacks)")


if __name__ == "__main__":
    asyncio.run(main())
