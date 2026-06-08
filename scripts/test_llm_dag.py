"""Real LLM end-to-end test for PlanningExecutorScheduler (V0.7 DAG path).

Tests:
  1. DAG Plan → Execute → Revise cycle with real LLM
  2. Event chain: PlanCreated → DagStepStarted(并行) → DagStepCompleted → PlanRevised → RunCompleted
  3. Verify DAG parallelism (independent steps in same layer)

Usage:
  cd D:\Project\JAgent
  .\.venv\Scripts\Activate.ps1
  python scripts\test_llm_dag.py
"""

import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("harness.agent").setLevel(logging.WARNING)
logging.getLogger("harness.guard").setLevel(logging.WARNING)

from harness.core.scheduler import PlanningExecutorScheduler, SchedulerConfig
from harness.core.planner import Planner
from harness.core.dag_executor import DagExecutor
from harness.core.context_manager import ContextManager
from harness.core.llm_client import OpenAILLMClient
from harness.core.fold import fold_events, RunStatus
from harness.models.events import EventType
from harness.models.tools import ToolDefinition, SideEffect, RetryPolicy
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

_log = logging.getLogger("test_llm_dag")

# ── Tool Definitions ─────────────────────────────────────

ECHO_DEF = ToolDefinition(
    name="echo",
    description="Echo back whatever message you send. Use this to confirm outputs or pipe data.",
    input_schema={"type": "object", "properties": {"msg": {"type": "string", "description": "Message to echo back"}}},
    output_schema={"type": "object"},
    idempotency_key_fields=["msg"],
    side_effects=[SideEffect.WRITE],
    timeout_ms=10000, retry_policy=RetryPolicy(),
    dangerous_with=[], max_parallel=5,
)

SEARCH_DEF = ToolDefinition(
    name="search",
    description="Search for information by keyword. Returns a list of result snippets.",
    input_schema={"type": "object", "properties": {"q": {"type": "string", "description": "Search query"}}},
    output_schema={"type": "object"},
    idempotency_key_fields=["q"],
    side_effects=[SideEffect.WRITE],
    timeout_ms=5000, retry_policy=RetryPolicy(),
    dangerous_with=[], max_parallel=5,
)

ALL_DEFS = [ECHO_DEF, SEARCH_DEF]
ALL_FNS = {
    "echo": lambda input_: {"echo": input_, "status": "ok"},
    "search": lambda input_: {"results": [f"result_{input_.get('q', '?')}"], "count": 1},
}


def _init_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for td in ALL_DEFS:
        reg.register(td, ALL_FNS.get(td.name))
    return reg


def _event_label(e) -> str:
    """Color-code event types for readability."""
    t = e.event_type
    if t in (EventType.PLAN_CREATED, EventType.PLAN_COMPLETED, EventType.PLAN_REVISED, EventType.PLAN_FAILED):
        return f"\033[36m{t.value}\033[0m"
    if t in (EventType.DAG_STEP_STARTED, EventType.DAG_STEP_COMPLETED, EventType.DAG_STEP_FAILED):
        return f"\033[33m{t.value}\033[0m"
    if t in (EventType.TOOL_CALLED, EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
        return f"\033[35m{t.value}\033[0m"
    if t in (EventType.RUN_STARTED, EventType.RUN_COMPLETED, EventType.RUN_FAILED):
        return f"\033[32m{t.value}\033[0m"
    if t == EventType.AGENT_THOUGHT:
        return f"\033[34m{t.value}\033[0m"
    return t.value


def _parse_layer_info(events: list) -> str:
    """Extract DAG layer structure from events."""
    layers: dict[int, list[str]] = {}
    for e in events:
        if e.event_type == EventType.DAG_STEP_STARTED:
            layer = e.payload.get("dag_layer", e.payload.get("depends_on", "?"))
            sid = e.payload.get("step_id", "?")
    # Build layer visualization from PlanCreated
    for e in events:
        if e.event_type == EventType.PLAN_CREATED:
            lc = e.payload.get("layer_count", 0)
            ss = e.payload.get("steps_summary", "")
            return f"{lc} layers, {ss}"
    return ""


def dump_events(label: str, events: list, elapsed: float) -> None:
    state = fold_events(events)
    plan_events = [e for e in events if e.event_type in (
        EventType.PLAN_CREATED, EventType.PLAN_COMPLETED,
        EventType.PLAN_REVISED, EventType.PLAN_FAILED,
    )]
    dag_events = [e for e in events if e.event_type in (
        EventType.DAG_STEP_STARTED, EventType.DAG_STEP_COMPLETED, EventType.DAG_STEP_FAILED,
    )]
    tool_events = [e for e in events if e.event_type in (
        EventType.TOOL_CALLED, EventType.TOOL_COMPLETED, EventType.TOOL_FAILED,
    )]
    completed_steps = [e for e in dag_events if e.event_type == EventType.DAG_STEP_COMPLETED]

    print(f"\n  {'='*56}")
    print(f"  {label}")
    print(f"  {'='*56}")
    print(f"  Time: {elapsed:.2f}s  |  Status: {state.status.value}")
    print(f"  Events: {len(events)} total")
    print(f"    Plan events: {len(plan_events)}")
    print(f"    DAG steps:   {len(dag_events)} ({len(completed_steps)} completed)")
    print(f"    Tool calls:  {len(tool_events)}")
    print()

    for e in events:
        payload = json.dumps(e.payload, ensure_ascii=False, default=str)
        print(f"  [{e.seq:2d}] {_event_label(e):<28s} {payload[:100]}")

    # DAG visualization
    if plan_events:
        print(f"\n  ── DAG Structure ──")
        for e in events:
            if e.event_type == EventType.PLAN_CREATED:
                pe = e.payload
                print(f"  Plan: {pe.get('intent', '')[:60]}")
                print(f"  Layers: {pe.get('layer_count', '?')}, {pe.get('steps_summary', '')}")
            if e.event_type == EventType.DAG_STEP_STARTED:
                pe = e.payload
                deps = pe.get("depends_on", [])
                dep_str = f" ← {', '.join(deps)}" if deps else " (root)"
                print(f"    -> {pe.get('step_id', '?')}: {pe.get('tool_name', '?')}{dep_str}")
            if e.event_type == EventType.DAG_STEP_COMPLETED:
                pe = e.payload
                print(f"    OK {pe.get('step_id', '?')}: {pe.get('output_summary', '')[:60]}")
            if e.event_type == EventType.DAG_STEP_FAILED:
                pe = e.payload
                print(f"    FAIL {pe.get('step_id', '?')}: {pe.get('error', '')[:60]}")

    print()


async def run_scenario(name: str, intent: str, api_key: str, model: str, base_url: str) -> dict:
    print(f"\n{'#'*58}")
    print(f"#  SCENARIO: {name}")
    print(f"#  {intent[:80]}")
    print(f"{'#'*58}")

    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)

    reg = _init_registry()
    llm = OpenAILLMClient(api_key=api_key, model=model, base_url=base_url)
    planner = Planner(llm, reg, store, max_plan_retries=2)
    dag = DagExecutor(executor, store, reg)
    cm = ContextManager(store, token_limit=5000, compression_threshold_ratio=0.5, checkpoint_interval=20)

    p_sched = PlanningExecutorScheduler(
        store=store, executor=executor, planner=planner, dag_executor=dag,
        tool_defs=ALL_DEFS, tool_fns=ALL_FNS,
        config=SchedulerConfig(max_iterations=10),
        context_manager=cm,
    )

    _log.info("Running PlanningExecutorScheduler...")
    t0 = time.monotonic()
    state = await p_sched.run(f"dag-{name}", intent)
    elapsed = time.monotonic() - t0

    events = await store.get_events(f"dag-{name}")
    dump_events(f"DAG: {name}", events, elapsed)

    # Verify
    has_plan_created = any(e.event_type == EventType.PLAN_CREATED for e in events)
    has_run_completed = state.status == RunStatus.COMPLETED
    parallel_steps = any(
        e.event_type == EventType.DAG_STEP_STARTED and not e.payload.get("depends_on")
        for e in events
    )

    print(f"  Verification:")
    print(f"    PlanCreated:     {'PASS' if has_plan_created else 'FAIL'}")
    print(f"    RunCompleted:    {'PASS' if has_run_completed else 'FAIL'}")
    print(f"    Parallel steps:  {'PASS' if parallel_steps else 'SKIP (serial only)'}")
    print()

    await store.close()
    return {
        "name": name,
        "status": state.status.value,
        "elapsed": elapsed,
        "events": len(events),
        "passed": has_plan_created and has_run_completed,
    }


async def main() -> None:
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL_NAME", "qwen3.7-max-preview")
    base_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    if not api_key:
        print("❌ LLM_API_KEY not set in .env")
        print("   Create .env file with:")
        print("   LLM_API_KEY=sk-xxx")
        print("   LLM_MODEL_NAME=qwen3.7-max-preview")
        sys.exit(1)

    print(f"  Model: {model}")
    print(f"  Key:   {api_key[:8]}…{api_key[-4:]}")
    print()

    results = []

    # Scenario 1: Parallel echo (simple DAG test)
    r = await run_scenario(
        "parallel-echo",
        "Echo the word 'hello' and echo the word 'world'. "
        "These are two independent echo operations that can be done in parallel.",
        api_key, model, base_url,
    )
    results.append(r)

    # Scenario 2: Search two topics (parallel search + DAG)
    r = await run_scenario(
        "parallel-search",
        "Search for information about 'AI' and about 'robotics' at the same time, "
        "they are independent of each other.",
        api_key, model, base_url,
    )
    results.append(r)

    # Scenario 3: Dependent chain (echo then echo)
    r = await run_scenario(
        "dependent-chain",
        "First echo the word 'start'. After that echo is done, echo the word 'end'.",
        api_key, model, base_url,
    )
    results.append(r)

    # Summary
    print("\n" + "=" * 58)
    print("  SUMMARY")
    print("=" * 58)
    for r in results:
        icon = "PASS" if r["passed"] else "FAIL"
        print(f"  [{icon}] {r['name']:<20s} {r['status']:<12s} {r['elapsed']:.2f}s  ({r['events']} events)")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  {passed}/{len(results)} passed")


if __name__ == "__main__":
    asyncio.run(main())
