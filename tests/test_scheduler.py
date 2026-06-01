"""Integration tests for Agent Loop Scheduler (L3)."""

import asyncio

import pytest

from harness import (
    AgentLoopScheduler,
    EventStore,
    EventType,
    MockAgentKernel,
    RetryPolicy,
    RunStatus,
    SchedulerConfig,
    SideEffect,
    ThinkResult,
    ToolDefinition,
    ToolExecutor,
)


@pytest.fixture
def http_tool_def():
    return ToolDefinition(
        name="http_request",
        description="Make an HTTP request",
        idempotency_key_fields=["url", "method"],
        side_effects=[SideEffect.EXTERNAL],
        timeout_ms=5000,
        retry_policy=RetryPolicy(max_retries=1),
    )


@pytest.fixture
def search_tool_def():
    return ToolDefinition(
        name="search",
        description="Search for information",
        idempotency_key_fields=["query"],
        side_effects=[],
    )


# ── 3.1 Scheduler main loop ───────────────────────────────────


async def http_tool(input):
    return {"status": 200, "body": "ok"}


async def search_tool(input):
    return {"results": ["a", "b"]}


@pytest.mark.asyncio
async def test_scheduler_completes_simple_task(store: EventStore):
    kernel = MockAgentKernel(
        [
            ThinkResult(
                thought="I should make an HTTP request",
                tool_name="http_request",
                tool_input={"url": "https://api.example.com", "method": "GET"},
            ),
            ThinkResult(thought="Task is complete", tool_name=None),
        ]
    )
    executor = ToolExecutor(store)
    tool_defs = [
        ToolDefinition(
            name="http_request",
            description="Make HTTP request",
            idempotency_key_fields=["url"],
            side_effects=[SideEffect.EXTERNAL],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        ),
    ]
    tool_fns = {"http_request": http_tool}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)

    state = await scheduler.run("run-1", "Call the API")
    assert state.status == RunStatus.COMPLETED
    assert state.intent == "Call the API"
    events = await store.get_events("run-1")
    event_types = [e.event_type for e in events]
    assert EventType.RUN_STARTED in event_types
    assert EventType.AGENT_THOUGHT in event_types
    assert EventType.TOOL_CALLED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert EventType.RUN_COMPLETED in event_types


@pytest.mark.asyncio
async def test_scheduler_multiple_tool_calls(store: EventStore):
    kernel = MockAgentKernel(
        [
            ThinkResult(thought="Step 1: search", tool_name="search", tool_input={"query": "X"}),
            ThinkResult(
                thought="Step 2: http", tool_name="http_request", tool_input={"url": "https://x.com", "method": "GET"}
            ),
            ThinkResult(thought="Done", tool_name=None),
        ]
    )
    executor = ToolExecutor(store)
    tool_defs = [
        ToolDefinition(
            name="search",
            description="Search",
            idempotency_key_fields=["query"],
            side_effects=[],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        ),
        ToolDefinition(
            name="http_request",
            description="HTTP",
            idempotency_key_fields=["url"],
            side_effects=[SideEffect.EXTERNAL],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        ),
    ]
    tool_fns = {"search": search_tool, "http_request": http_tool}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)
    state = await scheduler.run("run-1", "Search and fetch")

    assert state.status == RunStatus.COMPLETED
    events = await store.get_events("run-1")
    # RunStarted + 2×(AgentThought+ToolCalled+ToolCompleted) + AgentThought(stop) + RunCompleted = 9
    assert len(events) == 9


# ── 3.2 Auto event writing ─────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_agent_thought_written(store: EventStore):
    kernel = MockAgentKernel([ThinkResult(thought="All done", tool_name=None)])
    executor = ToolExecutor(store)
    tool_defs = [
        ToolDefinition(
            name="noop",
            description="Nothing",
            idempotency_key_fields=[],
            side_effects=[],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        )
    ]
    tool_fns = {"noop": lambda i: None}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)
    await scheduler.run("run-1", "Just think")

    events = await store.get_events("run-1")
    agent_thoughts = [e for e in events if e.event_type == EventType.AGENT_THOUGHT]
    assert len(agent_thoughts) == 1
    assert agent_thoughts[0].payload["thought"] == "All done"
    assert agent_thoughts[0].payload["tool_choice"] is None


# ── 3.3 Loop termination ───────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_tool_fails_run(store: EventStore):
    kernel = MockAgentKernel([ThinkResult(thought="try unknown", tool_name="ghost_tool", tool_input={})])
    executor = ToolExecutor(store)
    tool_defs = [
        ToolDefinition(
            name="known",
            description="Known",
            idempotency_key_fields=[],
            side_effects=[],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        )
    ]
    tool_fns = {"known": lambda i: None}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)
    state = await scheduler.run("run-1", "Bad call")

    assert state.status == RunStatus.FAILED
    assert "Unknown tool" in (state.last_error or "")


@pytest.mark.asyncio
async def test_circuit_breaker_on_consecutive_failures(store: EventStore):
    def failing_tool(input):
        raise RuntimeError("always fails")

    tool_def = ToolDefinition(
        name="fail_tool",
        description="Always fails",
        idempotency_key_fields=[],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )
    tool_fns = {"fail_tool": failing_tool}

    # 6 failures — exceeds max_consecutive_failures=5
    think_results = [ThinkResult(thought=f"Try {i}", tool_name="fail_tool", tool_input={}) for i in range(6)]
    kernel = MockAgentKernel(think_results)
    executor = ToolExecutor(store)
    config = SchedulerConfig(max_consecutive_failures=3)

    scheduler = AgentLoopScheduler(store, executor, kernel, [tool_def], tool_fns, config=config)
    state = await scheduler.run("run-1", "Test circuit breaker")

    assert state.status == RunStatus.FAILED
    assert "Circuit breaker" in (state.last_error or "")


@pytest.mark.asyncio
async def test_max_iterations_fails(store: EventStore):
    # Keep calling a tool forever
    results = [ThinkResult(thought="Keep going", tool_name="noop", tool_input={}) for _ in range(10)]
    kernel = MockAgentKernel(results)
    executor = ToolExecutor(store)
    tool_def = ToolDefinition(
        name="noop",
        description="Nop",
        idempotency_key_fields=[],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )
    tool_fns = {"noop": lambda i: None}

    config = SchedulerConfig(max_iterations=3)
    scheduler = AgentLoopScheduler(store, executor, kernel, [tool_def], tool_fns, config=config)
    state = await scheduler.run("run-1", "Infinite loop")

    assert state.status == RunStatus.FAILED
    assert "max iterations" in (state.last_error or "")


# ── 3.4 Pause / resume ────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_on_confirmation_needed(store: EventStore):
    dangerous_def = ToolDefinition(
        name="delete_file",
        description="Delete",
        idempotency_key_fields=["path"],
        side_effects=[SideEffect.DELETE],
        requires_confirmation=True,
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )

    def delete_fn(input):
        return {"deleted": input["path"]}

    kernel = MockAgentKernel(
        [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
    )
    executor = ToolExecutor(store)
    tool_defs = [dangerous_def]
    tool_fns = {"delete_file": delete_fn}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)

    # Run in background, pause will wait for resume
    task = asyncio.create_task(scheduler.run("run-1", "Delete file"))

    # Give it time to enter pause
    await asyncio.sleep(0.5)

    events = await store.get_events("run-1")
    event_types = [e.event_type for e in events]
    assert EventType.RUN_PAUSED in event_types
    assert EventType.CONFIRMATION_REQUESTED in event_types

    # Verify state is paused
    from harness.core.fold import RunStatus, fold_events

    state = fold_events(events)
    assert state.status == RunStatus.PAUSED
    assert state.pause_reason == "waiting_confirmation"
    assert len(state.pending_confirmations) == 1

    # Cancel the task (it's hanging waiting for resume)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_resume_writes_run_resumed_and_completes_execution(store: EventStore):
    dangerous_def = ToolDefinition(
        name="delete_file",
        description="Delete",
        idempotency_key_fields=["path"],
        side_effects=[SideEffect.DELETE],
        requires_confirmation=True,
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )

    def delete_fn(input):
        return {"deleted": input["path"]}

    kernel = MockAgentKernel(
        [
            ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"}),
            ThinkResult(thought="Done", tool_name=None),
        ]
    )
    executor = ToolExecutor(store)
    tool_defs = [dangerous_def]
    tool_fns = {"delete_file": delete_fn}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)

    # Run in background
    task = asyncio.create_task(scheduler.run("run-1", "Delete file"))

    # Wait for pause
    await asyncio.sleep(0.5)

    events = await store.get_events("run-1")
    assert EventType.RUN_PAUSED in [e.event_type for e in events]
    assert EventType.CONFIRMATION_REQUESTED in [e.event_type for e in events]

    # Extract confirmation_id and write ConfirmationReceived(confirmed=true)
    cf_payload = None
    for e in events:
        if e.event_type == EventType.CONFIRMATION_REQUESTED:
            cf_payload = e.payload
            break
    assert cf_payload is not None

    from harness import ConfirmationReceivedPayload

    await store.append_event(
        "run-1",
        EventType.CONFIRMATION_RECEIVED,
        ConfirmationReceivedPayload(
            confirmation_id=cf_payload["confirmation_id"],
            confirmed=True,
            operator_id="op-test",
        ).model_dump(),
    )

    # Resume
    await scheduler.resume("run-1")

    # Wait for completion
    await asyncio.wait_for(task, timeout=5.0)

    # Verify full event chain
    all_events = await store.get_events("run-1")
    event_types = [e.event_type for e in all_events]

    assert EventType.RUN_STARTED in event_types
    assert EventType.AGENT_THOUGHT in event_types
    assert EventType.CONFIRMATION_REQUESTED in event_types
    assert EventType.RUN_PAUSED in event_types
    assert EventType.CONFIRMATION_RECEIVED in event_types
    assert EventType.RUN_RESUMED in event_types  # ← P1 fix verification
    assert EventType.TOOL_CALLED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert EventType.RUN_COMPLETED in event_types

    from harness.core.fold import RunStatus, fold_events

    state = fold_events(all_events)
    assert state.status == RunStatus.COMPLETED
    assert len(state.tool_results) == 1
    assert state.tool_results[0].status == "completed"
    assert len(state.pending_confirmations) == 0


# ── 3.6 Integration: event stream replay ──────────────────────


@pytest.mark.asyncio
async def test_event_stream_replay_produces_consistent_state(store: EventStore):
    kernel = MockAgentKernel(
        [
            ThinkResult(
                thought="Step 1", tool_name="http_request", tool_input={"url": "https://x.com", "method": "GET"}
            ),
            ThinkResult(thought="Done", tool_name=None),
        ]
    )
    executor = ToolExecutor(store)
    tool_defs = [
        ToolDefinition(
            name="http_request",
            description="HTTP",
            idempotency_key_fields=["url"],
            side_effects=[SideEffect.EXTERNAL],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        )
    ]
    tool_fns = {"http_request": http_tool}

    scheduler = AgentLoopScheduler(store, executor, kernel, tool_defs, tool_fns)
    await scheduler.run("run-1", "Replay test")

    events = await store.get_events("run-1")
    from harness.core.fold import fold_events

    state1 = fold_events(events)
    state2 = fold_events(events)
    assert state1.status == state2.status
    assert state1.intent == state2.intent
    assert len(state1.thought_history) == len(state2.thought_history)
    assert len(state1.tool_calls) == len(state2.tool_calls)
    assert len(state1.tool_results) == len(state2.tool_results)


# ── 3.x Guardrail blocked counts as failure ───────────────────


@pytest.mark.asyncio
async def test_guardrail_blocked_counts_toward_circuit_breaker(store: EventStore):
    def guarded_fn(input):
        return {"ok": True}

    tool_def = ToolDefinition(
        name="guarded",
        description="Has guardrail",
        idempotency_key_fields=[],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )

    # No "x" in input — schema guardrail will fail
    results = [ThinkResult(thought="Try", tool_name="guarded", tool_input={"y": 1}) for _ in range(4)]
    kernel = MockAgentKernel(results)
    executor = ToolExecutor(store)
    config = SchedulerConfig(max_consecutive_failures=3)

    scheduler = AgentLoopScheduler(store, executor, kernel, [tool_def], {"guarded": guarded_fn}, config=config)
    state = await scheduler.run("run-1", "Guardrail test")
    assert state.status == RunStatus.FAILED
    assert "Circuit breaker" in (state.last_error or "")
