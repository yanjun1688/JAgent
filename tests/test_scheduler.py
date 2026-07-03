"""Integration tests for Agent Loop Scheduler (L3)."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from harness import (
    AgentLoopScheduler,
    ContextManager,
    EventStore,
    EventType,
    MockAgentKernel,
    RetryPolicy,
    ExecState,
    StepResult,
    RunStatus,
    SchedulerConfig,
    SideEffect,
    ThinkResult,
    ToolDefinition,
    ToolExecutor,
)
from harness.core.scheduler import BaseScheduler
from harness.monitoring.run_monitor import RunMonitor


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
    except (asyncio.CancelledError, Exception):
        pass


# ── State chain fix tests ───────────────────────────────────────


class TestPlanStateChain:
    """Verify _execute_plan uses passed-in state, not internal _refresh_state."""

    @pytest.mark.asyncio
    async def test_plan_uses_passed_state_for_context_manager(self, store: EventStore):
        """Verify _execute_plan uses the passed state.seq for context_manager,
        not a freshly-refreshed state."""
        from harness.core.dag_executor import DagExecutor
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.models.plan import DagPlan, DagStep
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        cm = ContextManager(store, token_limit=10000, compression_threshold_ratio=0.5, checkpoint_interval=1)
        sched = PlanningExecutorScheduler(
            store, executor, planner, dag, [], {},
            context_manager=cm,
        )

        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        ])
        planner.plan = AsyncMock(return_value=plan)
        planner.last_raw_response = "mock"

        # execute_layer succeeds
        async def _exec(run_id, p, plan_id, layer, layer_idx, layers, all_results):
            for sid in layer:
                all_results[sid] = StepResult(step_id=sid, exec_state=ExecState.COMPLETED, output={})
            return True

        dag.execute_layer = AsyncMock(side_effect=_exec)
        dag.build_dag_status_text = Mock(return_value="")
        planner.revise = AsyncMock(return_value=DagPlan(intent="test", steps=[]))
        planner.generate_answer = AsyncMock(return_value="Done")

        state = await sched.run("run-chain-1", "test chain")
        assert state.status == RunStatus.COMPLETED
        events = await store.get_events("run-chain-1")
        assert any(e.event_type == EventType.CONTEXT_CHECKPOINTED for e in events), (
            "Context manager should have checkpointed during plan execution"
        )

    @pytest.mark.asyncio
    async def test_feedback_consumed_after_plan_revised(self, store: EventStore):
        """After PlanRevised, consumed feedbacks should not appear in next _get_feedback_text."""
        from harness.core.dag_executor import DagExecutor
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.models.plan import DagPlan, DagStep
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        monitor = RunMonitor(store, max_tokens=10, token_warning_ratio=0.5)
        monitor.attach()

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        sched = PlanningExecutorScheduler(
            store, executor, planner, dag, [], {},
            monitor=monitor,
        )

        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        ])
        planner.plan = AsyncMock(return_value=plan)

        # First: plan with feedback → execute_layer fails → revise succeeds
        async def _exec(run_id, p, plan_id, layer, layer_idx, layers, all_results):
            for sid in layer:
                all_results[sid] = StepResult(step_id=sid, exec_state=ExecState.FAILED, error="test fail")
            return False

        dag.execute_layer = AsyncMock(side_effect=_exec)
        dag.build_dag_status_text = Mock(return_value="")
        planner.revise = AsyncMock(return_value=DagPlan(intent="test", steps=[]))
        planner.generate_answer = AsyncMock(return_value="Done")
        planner.last_raw_response = "mock"

        # Pre-inject a feedback by triggering token warning via a long thought
        await store.append_event("run-chain-2", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        await store.append_event("run-chain-2", EventType.AGENT_THOUGHT, {
            "thought": "word " * 200, "token_count": 1,
        })

        # Verify feedback is in store before scheduler runs
        pre_events = await store.get_events("run-chain-2")
        from harness.core.fold import fold_events
        pre_state = fold_events(pre_events)
        initial_fb_count = len(pre_state.feedbacks)
        assert initial_fb_count >= 1, "Should have token warning feedback before scheduler runs"

        # Now run scheduler — it will use the existing events and produce PlanRevised
        # which should mark the feedback as consumed
        state = await sched.run("run-chain-2", "test chain")
        # Should have completed (empty plan after revise)
        assert state.status in (RunStatus.COMPLETED, RunStatus.FAILED)

        final_events = await store.get_events("run-chain-2")
        final_state = fold_events(final_events)

        # Check that PlanRevised was written
        assert any(e.event_type == EventType.PLAN_REVISED for e in final_events), (
            f"Expected PLAN_REVISED in events: {[e.event_type.value for e in final_events]}"
        )

        # After PlanRevised, all feedbacks should be consumed
        for fb in final_state.feedbacks:
            assert fb.consumed_at_seq is not None, (
                f"All feedbacks should have consumed_at_seq after PlanRevised. "
                f"Feedback '{fb.feedback_text}' has consumed_at_seq={fb.consumed_at_seq}"
            )



# ── 3.x CRITICAL bug regression tests ────────────────────────────


class TestStaticPlanUnboundLocalError:
    """Verify fix: PlanSuspended on layer 0 with failures does NOT raise UnboundLocalError."""

    @pytest.mark.asyncio
    async def test_confirmation_and_failure_in_first_layer(self, store: EventStore):
        """PlanSuspended + layer_failures non-empty → ok = False assigned → no UnboundLocalError.

        Trigger: DAG layer 0 has steps [s1(confirm), s2(failed)].
        execute_layer raises PlanSuspended (s1 needs confirmation).
        s2 already failed in dag_executor (DAG_STEP_FAILED written, result stored).
        After confirmations, layer_failures = [s2] → ok was never assigned
        → bug triggered UnboundLocalError. Fix sets ok = False.
        """
        from harness.core.dag_executor import DagExecutor, PlanSuspended
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.models.plan import DagPlan, DagStep
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {})
        sched.config.pause_timeout_ms = 999999

        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={"msg": "a"}),
            DagStep(id="s2", tool="echo", input={"msg": "b"}),
        ])
        planner.plan = AsyncMock(return_value=plan)
        planner.generate_answer = AsyncMock(return_value="Done")
        planner.last_raw_response = "mock"

        # execute_layer raises PlanSuspended (s1 needs confirmation)
        # s2 result is already written as failed by dag_executor internals
        # We simulate: the exception is raised, and s2 is already in results as failed
        async def _exec(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
            all_results["s2"] = StepResult(step_id="s2", exec_state=ExecState.FAILED, error="fail")
            raise PlanSuspended(confirmations=[("s1", "cid-1")])

        dag.execute_layer = AsyncMock(side_effect=_exec)
        dag.build_dag_status_text = Mock(return_value="")

        # retry_step: first call needs_confirmation, second call completed
        retry_calls = 0

        async def _retry(run_id, plan, step_id, results):
            nonlocal retry_calls
            retry_calls += 1
            if retry_calls == 1:
                return StepResult(step_id=step_id, exec_state=ExecState.PENDING,
                                  confirmation_id="cid-1")
            return StepResult(step_id=step_id, exec_state=ExecState.COMPLETED, output={})

        dag.retry_step = AsyncMock(side_effect=_retry)

        # Run in background — resume to progress through confirmations
        run_id = "run-unbound"
        task = asyncio.create_task(sched.run(run_id, "test"))
        await asyncio.sleep(0.2)

        # Resume to complete the confirmation
        await sched.resume(run_id)
        await asyncio.sleep(0.1)
        await sched.resume(run_id)  # second resume for the second confirmation retry
        await asyncio.sleep(0.2)

        try:
            state = await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            state = None

        # The point: no UnboundLocalError was raised.
        # If the bug were still present, the task would have crashed.
        events = await store.get_events(run_id)
        assert any(e.event_type == EventType.PLAN_REVISED for e in events) or \
               any(e.event_type == EventType.RUN_FAILED for e in events), \
               "Expected PLAN_REVISED or RUN_FAILED (confirm → layer failure → revise/fallback)"

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestMaxIterationsPlanningExecutor:
    """Verify max_iterations is enforced for PlanningExecutorScheduler."""

    @pytest.mark.asyncio
    async def test_max_iterations_enforced(self, store: EventStore):
        """With max_iterations=2 and infinite revisit, should fail with exceeded."""
        from harness.core.dag_executor import DagExecutor
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.models.plan import DagPlan, DagStep
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        config = SchedulerConfig(max_iterations=2)
        sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {}, config=config)

        # plan always returns steps → not empty → loop continues
        plan = DagPlan(intent="test", steps=[DagStep(id="s1", tool="echo", input={})])
        planner.plan = AsyncMock(return_value=plan)
        planner.last_raw_response = "mock"

        # execute_layer always fails → triggers revise → revise returns new plan → loop
        async def _exec(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
            for sid in layer:
                all_results[sid] = StepResult(step_id=sid, exec_state=ExecState.FAILED, error="fail")
            return False

        dag.execute_layer = AsyncMock(side_effect=_exec)
        dag.build_dag_status_text = Mock(return_value="")
        planner.revise = AsyncMock(return_value=plan)  # same plan → loop continues

        state = await sched.run("run-max-iters", "test")
        assert state.status == RunStatus.FAILED
        err = (state.last_error or "").lower()
        assert "max iterations" in err or "self-heal exceeded" in err


class TestCancelDuringDagConfirmation:
    """Verify cancel during DAG confirmation loop terminates quickly."""

    @pytest.mark.asyncio
    async def test_cancel_during_static_confirmation_loop(self, store: EventStore):
        """Cancel during DAG static confirmation loop → immediate termination."""
        from harness.core.dag_executor import DagExecutor, PlanSuspended
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.models.plan import DagPlan, DagStep
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {})
        sched.config.pause_timeout_ms = 999999

        plan = DagPlan(intent="test", steps=[DagStep(id="s1", tool="echo", input={})])
        planner.plan = AsyncMock(return_value=plan)
        planner.last_raw_response = "mock"

        # execute_layer raises PlanSuspended, enters confirmation loop
        dag.execute_layer = AsyncMock(
            side_effect=PlanSuspended(confirmations=[("s1", "cid-1")])
        )
        dag.build_dag_status_text = Mock(return_value="")

        # retry_step keeps returning confirmation_needed (infinite loop without cancel)
        dag.retry_step = AsyncMock(return_value=StepResult(
            step_id="s1", exec_state=ExecState.PENDING, confirmation_id="cid-1",
        ))

        run_id = "run-cancel-dag-confirm"
        task = asyncio.create_task(sched.run(run_id, "test"))
        await asyncio.sleep(0.2)

        # Now cancel — should terminate the confirmation loop
        await sched.cancel(run_id)
        await asyncio.sleep(0.3)

        events = await store.get_events(run_id)
        assert any(e.event_type == EventType.RUN_FAILED for e in events), (
            f"Expected RUN_FAILED after cancel, got: {[e.event_type.value for e in events]}"
        )

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestPlanningExecutorScheduler:
    """Problem 2: basic tests for PlanningExecutorScheduler (DAG path)."""

    @pytest.mark.asyncio
    async def test_is_subclass_of_base_scheduler(self):
        from harness.core.scheduler import BaseScheduler, PlanningExecutorScheduler
        assert issubclass(PlanningExecutorScheduler, BaseScheduler)

    @pytest.mark.asyncio
    async def test_instantiation_and_dag_fields(self, store: EventStore):
        """Smoke test: PlanningExecutorScheduler can be created with all deps."""
        from harness.core.dag_executor import DagExecutor
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag_executor = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        scheduler = PlanningExecutorScheduler(
            store, executor, planner, dag_executor, [], {},
        )

        assert scheduler.planner is planner
        assert scheduler.dag_executor is dag_executor

    @pytest.mark.asyncio
    async def test_confirm_retries_configured_for_dag_path(self, store: EventStore):
        """Verify max_confirm_retries is accessible for DAG scheduler."""
        from harness.core.dag_executor import DagExecutor
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="Mock answer")

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag_executor = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        config = SchedulerConfig(max_confirm_retries=3)
        scheduler = PlanningExecutorScheduler(
            store, executor, planner, dag_executor, [], {}, config=config,
        )

        assert scheduler.config.max_confirm_retries == 3


class TestPlanningExecutorSchedulerExecution:
    """Execution tests for PlanningExecutorScheduler DAG self-heal loop."""

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _make_env(store: EventStore):
        """Create a minimal PlanningExecutorScheduler with mock-friendly deps."""
        from harness.core.dag_executor import DagExecutor
        from harness.core.llm_client import ChatResponse
        from harness.core.planner import Planner
        from harness.core.scheduler.plan import PlanningExecutorScheduler
        from harness.tools.registry import ToolRegistry

        class _MockLLM:
            async def chat(self, messages, **kwargs):
                return ChatResponse(content="yes")  # must contain "yes" for classifier to pass

        executor = ToolExecutor(store)
        registry = ToolRegistry()
        dag = DagExecutor(executor, store, registry)
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=store)
        sched = PlanningExecutorScheduler(store, executor, planner, dag, [], {})
        return sched, planner, dag

    @staticmethod
    def _plan(*, step_ids: list[str] | None = None):
        """Build a DagPlan with one-layer independent steps."""
        from harness.models.plan import DagPlan, DagStep

        ids = step_ids if step_ids is not None else ["s1"]
        steps = [DagStep(id=sid, tool="echo", input={"msg": sid}) for sid in ids]
        return DagPlan(intent="test", steps=steps)

    @staticmethod
    def _mock_exec_layer(*, succeed: bool = True):
        """Return an AsyncMock for execute_layer that populates results."""
        async def _execute(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
            for sid in layer:
                all_results[sid] = StepResult(
                    step_id=sid,
                    exec_state=ExecState.COMPLETED if succeed else ExecState.FAILED,
                    output={},
                )
            return succeed
        return AsyncMock(side_effect=_execute)

    # ── 1. All layers succeed ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_static_all_layers_succeed(self, store: EventStore):
        """DAG path: all layers complete successfully → RUN_COMPLETED."""
        sched, planner, dag = self._make_env(store)

        planner.plan = AsyncMock(return_value=self._plan(step_ids=["s1", "s2"]))
        planner.generate_answer = AsyncMock(return_value="Done")
        planner.last_raw_response = "mock plan response"
        dag.execute_layer = self._mock_exec_layer(succeed=True)
        dag.build_dag_status_text = Mock(return_value="")

        planner.revise = AsyncMock()  # track if ever called

        state = await sched.run("run-all-ok", "test")
        events = await store.get_events("run-all-ok")

        assert state.status == RunStatus.COMPLETED
        assert any(e.event_type == EventType.PLAN_CREATED for e in events)
        assert any(e.event_type == EventType.PLAN_COMPLETED for e in events)
        assert any(e.event_type == EventType.RUN_COMPLETED for e in events)
        # No PLAN_REVISED — all-ok revise was removed
        assert not any(e.event_type == EventType.PLAN_REVISED for e in events)
        # planner.revise should never be called
        planner.revise.assert_not_called()

    # ── 2. Layer fails → revise returns steps → self-heal continues ──

    @pytest.mark.asyncio
    async def test_layer_fails_revise_continues(self, store: EventStore):
        """Self-heal: layer fails → revise returns remaing steps → while-loop re-executes → COMPLETED."""
        sched, planner, dag = self._make_env(store)

        initial_plan = self._plan(step_ids=["s1", "s2"])
        revised_plan = self._plan(step_ids=["s2_retry"])

        planner.plan = AsyncMock(return_value=initial_plan)
        planner.generate_answer = AsyncMock(return_value="Done")
        planner.last_raw_response = "mock plan response"

        # First call fails, second call (with revised plan) succeeds
        exec_call_count = 0

        async def _exec_side(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
            nonlocal exec_call_count
            exec_call_count += 1
            if exec_call_count == 1:
                for sid in layer:
                    all_results[sid] = {"status": "failed", "output": {}}
                return False  # fail → trigger revise
            # Second call with revised plan
            for sid in layer:
                all_results[sid] = {"status": "completed", "output": {}}
            return True

        dag.execute_layer = AsyncMock(side_effect=_exec_side)
        dag.build_dag_status_text = Mock(return_value="")

        # revise returns a new plan with remaining steps
        planner.revise = AsyncMock(return_value=revised_plan)

        state = await sched.run("run-heal", "test")
        events = await store.get_events("run-heal")

        assert state.status == RunStatus.COMPLETED
        # Two PLAN_CREATED events: one for initial, one for revised plan
        created = [e for e in events if e.event_type == EventType.PLAN_CREATED]
        assert len(created) == 2
        # One PLAN_REVISED before the self-heal restart
        assert len([e for e in events if e.event_type == EventType.PLAN_REVISED]) >= 1
        # One PLAN_COMPLETED
        assert any(e.event_type == EventType.PLAN_COMPLETED for e in events)
        assert any(e.event_type == EventType.RUN_COMPLETED for e in events)
        planner.revise.assert_awaited_once()

    # ── 3. Layer fails → revise returns empty → complete ─────────────

    @pytest.mark.asyncio
    async def test_layer_fails_revise_empty_completes(self, store: EventStore):
        """Layer fails → revise says no steps left → finalize → COMPLETED."""
        sched, planner, dag = self._make_env(store)

        planner.plan = AsyncMock(return_value=self._plan(step_ids=["s1"]))
        planner.generate_answer = AsyncMock(return_value="Done")
        planner.last_raw_response = "mock"

        dag.execute_layer = self._mock_exec_layer(succeed=False)
        dag.build_dag_status_text = Mock(return_value="")

        planner.revise = AsyncMock(return_value=self._plan(step_ids=[]))  # no steps = done

        state = await sched.run("run-empty-revise", "test")
        events = await store.get_events("run-empty-revise")

        assert state.status == RunStatus.COMPLETED
        assert any(e.event_type == EventType.PLAN_CREATED for e in events)
        assert any(e.event_type == EventType.PLAN_REVISED for e in events)
        assert any(e.event_type == EventType.RUN_COMPLETED for e in events)

    # ── 4. Layer fails → revise returns None → _fail ─────────────────

    @pytest.mark.asyncio
    async def test_layer_fails_revise_none_fails(self, store: EventStore):
        """Layer fails → revise fails (None) → RUN_FAILED."""
        sched, planner, dag = self._make_env(store)

        planner.plan = AsyncMock(return_value=self._plan(step_ids=["s1"]))
        planner.last_raw_response = "mock"

        dag.execute_layer = self._mock_exec_layer(succeed=False)
        dag.build_dag_status_text = Mock(return_value="")
        planner.revise = AsyncMock(return_value=None)

        state = await sched.run("run-revise-none", "test")
        events = await store.get_events("run-revise-none")

        assert state.status == RunStatus.FAILED
        # Should write RUN_FAILED
        assert any(e.event_type == EventType.RUN_FAILED for e in events)

    # ── 5. Cancel during DAG execution ───────────────────────────────

    @pytest.mark.asyncio
    async def test_cancel_during_dag_execution(self, store: EventStore):
        """Cancel during DAG execution → RUN_FAILED."""
        from harness.models.plan import DagPlan, DagStep

        sched, planner, dag = self._make_env(store)

        # Plan with 2 dependent steps → 2 layers: [s1], [s2]
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={"msg": "a"}),
            DagStep(id="s2", tool="echo", input={"msg": "b"}, depends_on=["s1"]),
        ])
        planner.plan = AsyncMock(return_value=plan)
        planner.last_raw_response = "mock"

        # execute_layer: first layer slow, second layer fast
        # Call cancel during first layer → it's detected at second layer check
        started = asyncio.Event()

        async def _exec(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
            for sid in layer:
                all_results[sid] = {"status": "completed", "output": {}}
            if layer_idx == 0:
                started.set()
                await asyncio.sleep(0.3)  # slow enough for cancel to be called
            return True

        dag.execute_layer = AsyncMock(side_effect=_exec)

        run_id = "run-cancel-dag"
        task = asyncio.create_task(sched.run(run_id, "test"))

        await started.wait()
        await asyncio.sleep(0.05)
        await sched.cancel(run_id)

        # Wait for task to finish (cancel flag detected at layer 1 entry → _fail)
        try:
            state = await asyncio.wait_for(task, timeout=3.0)
        except (asyncio.CancelledError, Exception):
            pass

        events = await store.get_events(run_id)
        assert any(e.event_type == EventType.RUN_FAILED for e in events)

    # ── 6. Confirmation retries exceeded in DAG path ─────────────────

    @pytest.mark.asyncio
    async def test_dag_confirmation_retries_exceeded(self, store: EventStore):
        """DAG confirmation loop: max_confirm_retries exceeded → RUN_FAILED."""
        from harness.core.dag_executor import PlanSuspended

        sched, planner, dag = self._make_env(store)
        sched.config = SchedulerConfig(max_confirm_retries=2, pause_timeout_ms=999999)

        planner.plan = AsyncMock(return_value=self._plan(step_ids=["s1"]))
        planner.last_raw_response = "mock"

        # execute_layer raises PlanSuspended
        dag.execute_layer = AsyncMock(side_effect=PlanSuspended(
            confirmations=[("s1", "cid-1")],
        ))
        dag.build_dag_status_text = Mock(return_value="")

        # retry_step keeps returning confirmation_needed
        dag.retry_step = AsyncMock(return_value=StepResult(
            step_id="s1", exec_state=ExecState.PENDING, confirmation_id="cid-1",
        ))

        run_id = "run-confirm-exceed"
        task = asyncio.create_task(sched.run(run_id, "test"))
        await asyncio.sleep(0.3)

        # Resume 3 times — exceeds max_confirm_retries=2
        for _ in range(3):
            await sched.resume(run_id)
            await asyncio.sleep(0.05)

        await asyncio.sleep(0.5)

        events = await store.get_events(run_id)
        assert any(e.event_type == EventType.RUN_FAILED for e in events), (
            f"Expected RUN_FAILED, got: {[e.event_type.value for e in events]}"
        )

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # ── Bug 4: SOFT_ERROR should trigger revise ───────────────────────

    @pytest.mark.asyncio
    async def test_soft_error_triggers_revise(self, store: EventStore):
        """Bug 4: SOFT_ERROR steps must trigger a revise even when layer returns True."""
        sched, planner, dag = self._make_env(store)

        planner.plan = AsyncMock(return_value=self._plan(step_ids=["s1", "s2"]))
        planner.generate_answer = AsyncMock(return_value="Done")
        planner.last_raw_response = "mock plan response"

        async def _exec_with_soft_error(run_id, plan, plan_id, layer, layer_idx, layers, all_results):
            for sid in layer:
                all_results[sid] = StepResult(
                    step_id=sid,
                    exec_state=ExecState.SOFT_ERROR if sid == "s2" else ExecState.COMPLETED,
                    output={"success": True} if sid == "s1" else {"success": False},
                    error="soft error" if sid == "s2" else None,
                )
            return True  # layer "succeeds" — SOFT_ERROR is not a hard failure

        dag.execute_layer = AsyncMock(side_effect=_exec_with_soft_error)
        dag.build_dag_status_text = Mock(return_value="")

        planner.revise = AsyncMock(return_value=self._plan(step_ids=[]))

        state = await sched.run("run-soft-error-revise", "test")
        events = await store.get_events("run-soft-error-revise")

        assert state.status == RunStatus.COMPLETED
        planner.revise.assert_awaited_once()
        assert any(e.event_type == EventType.PLAN_REVISED for e in events), (
            "Bug 4: SOFT_ERROR should trigger a revise, writing PLAN_REVISED"
        )
        revised = [e for e in events if e.event_type == EventType.PLAN_REVISED]
        assert any("soft_error" in (e.payload.get("revision_reason", "") or "") for e in revised), (
            "Bug 4: PLAN_REVISED reason should indicate it was triggered by soft_error"
        )


class TestBoundaryCases:
    """Problem 4: boundary tests for BaseScheduler methods."""

    @pytest.mark.asyncio
    async def test_run_reentry_raises_runtime_error(self, store: EventStore):
        async def slow_fn(input):
            await asyncio.sleep(0.5)
            return {"ok": True}

        tool_def = ToolDefinition(
            name="slow", description="Slow", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel([ThinkResult(thought="Slow work", tool_name="slow", tool_input={})])
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(store, executor, kernel, [tool_def], {"slow": slow_fn})
        run_id = "run-reentry"

        task = asyncio.create_task(scheduler.run(run_id, "Reentry test"))
        await asyncio.sleep(0.05)
        try:
            with pytest.raises(RuntimeError, match="already running"):
                await scheduler.run(run_id, "Duplicate")
        finally:
            await scheduler.cancel(run_id)
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_is_paused_on_pending_confirmation(self, store: EventStore):
        dangerous_def = ToolDefinition(
            name="delete_file", description="Delete",
            idempotency_key_fields=["path"], side_effects=[SideEffect.DELETE],
            requires_confirmation=True, timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel(
            [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
        )
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [dangerous_def], {"delete_file": lambda i: {"deleted": i["path"]}},
            config=SchedulerConfig(pause_timeout_ms=999999),
        )
        run_id = "run-is-paused"
        task = asyncio.create_task(scheduler.run(run_id, "Is paused test"))
        await asyncio.sleep(0.3)

        # Confirmation wait uses _confirm_events, is_paused checks _pause_events
        # So is_paused should be False for confirmation (they're separate now)
        assert scheduler.is_paused(run_id) is False

        await scheduler.cancel(run_id)
        task.cancel()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_cancel_terminates_confirmation_loop(self, store: EventStore):
        dangerous_def = ToolDefinition(
            name="delete_file", description="Delete",
            idempotency_key_fields=["path"], side_effects=[SideEffect.DELETE],
            requires_confirmation=True, timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel(
            [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
        )
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [dangerous_def], {"delete_file": lambda i: {"deleted": i["path"]}},
            config=SchedulerConfig(pause_timeout_ms=999999),
        )
        run_id = "run-cancel-confirm"
        task = asyncio.create_task(scheduler.run(run_id, "Cancel confirm test"))
        await asyncio.sleep(0.3)

        await scheduler.cancel(run_id)
        await asyncio.sleep(0.3)

        # After cancel, the run should be done (or cancelled)
        assert task.done() or task.cancelled()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_timeout_race_resume_rejected_after_timeout(store: EventStore):
    """CT-1: After _wait_for_resume times out, resume() must not resurrect.

    Without fix: resume() writes RUN_RESUMED after RUN_FAILED → fold = RUNNING.
    With fix: state-based guard in resume() checks fold state before writing.
    """
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
    config = SchedulerConfig(max_iterations=5, pause_timeout_ms=100)

    scheduler = AgentLoopScheduler(
        store, executor, kernel, [dangerous_def], {"delete_file": delete_fn}, config=config
    )

    task = asyncio.create_task(scheduler.run("run-ct1", "CT-1 test"))
    await asyncio.sleep(0.5)

    # resume() after timeout should be rejected
    await scheduler.resume("run-ct1")

    try:
        state = await asyncio.wait_for(task, timeout=3.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        events = await store.get_events("run-ct1")
        from harness.core.fold import fold_events
        state = fold_events(events)

    assert state.status == RunStatus.FAILED, (
        f"CT-1: resume() after timeout should not resurrect. "
        f"Got {state.status}, expected FAILED"
    )
    assert "Confirmation timed out" in (state.last_error or "")

    events = await store.get_events("run-ct1")
    run_failed_seqs = [e.seq for e in events if e.event_type == EventType.RUN_FAILED]
    run_resumed_after = [
        e for e in events
        if e.event_type == EventType.RUN_RESUMED
        and (not run_failed_seqs or e.seq > max(run_failed_seqs))
    ]
    assert len(run_resumed_after) == 0, (
        f"CT-1: RUN_RESUMED event after RUN_FAILED should not exist. "
        f"Found {len(run_resumed_after)} RUN_RESUMED after FAILED"
    )


@pytest.mark.asyncio
async def test_timeout_race_concurrent_resume_and_fail(store: EventStore):
    """CT-1 concurrency: resume() and _fail() called simultaneously must not produce
    RUN_RESUMED after RUN_FAILED.

    Unlike the sequential test above, this fires both operations concurrently via
    asyncio.gather so the event loop interleaves their store I/O, exercising the
    read-check-write race window.
    """
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
    config = SchedulerConfig(max_iterations=5, pause_timeout_ms=5000)

    scheduler = AgentLoopScheduler(
        store, executor, kernel, [dangerous_def], {"delete_file": delete_fn}, config=config
    )

    run_id = "run-ct1-concurrent"
    task = asyncio.create_task(scheduler.run(run_id, "CT-1 concurrent test"))
    await asyncio.sleep(0.3)  # wait for run to enter _wait_for_resume

    # Concurrently: force-fail the run AND try to resume it.
    async def fail_then_resume():
        await scheduler._fail(run_id, "Forced fail during confirmation")
        await scheduler.resume(run_id)

    await asyncio.gather(
        fail_then_resume(),
        scheduler.resume(run_id),
    )

    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
        pass

    events = await store.get_events(run_id)
    run_failed_seqs = [e.seq for e in events if e.event_type == EventType.RUN_FAILED]
    run_resumed_after = [
        e for e in events
        if e.event_type == EventType.RUN_RESUMED
        and (not run_failed_seqs or e.seq > max(run_failed_seqs))
    ]
    assert len(run_resumed_after) == 0, (
        f"CT-1 concurrent: RUN_RESUMED after RUN_FAILED should not exist. "
        f"Found {len(run_resumed_after)} RUN_RESUMED after FAILED. "
        f"Events: {[(e.event_type.value, e.seq) for e in events]}"
    )


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


# ── User-requested pause / resume ─────────────────────────────


@pytest.mark.asyncio
async def test_user_pause_stops_loop(store: EventStore):
    """scheduler.pause() should actually halt the run loop (not just write an event)."""
    async def slow_fn(input):
        await asyncio.sleep(0.08)
        return {"ok": True}

    tool_def = ToolDefinition(
        name="counter",
        description="Count calls",
        idempotency_key_fields=None,  # Disable caching so each call executes
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )

    # Use unique inputs so each call has a different idempotency key
    kernel = MockAgentKernel(
        [
            ThinkResult(thought="Call 1", tool_name="counter", tool_input={"seq": 1}),
            ThinkResult(thought="Call 2", tool_name="counter", tool_input={"seq": 2}),
            ThinkResult(thought="Call 3", tool_name="counter", tool_input={"seq": 3}),
            ThinkResult(thought="Done", tool_name=None),
        ]
    )
    executor = ToolExecutor(store)
    scheduler = AgentLoopScheduler(store, executor, kernel, [tool_def], {"counter": slow_fn})

    task = asyncio.create_task(scheduler.run("run-pause", "Pause test"))

    # Wait for at least one tool call to complete
    await asyncio.sleep(0.15)

    await scheduler.pause("run-pause")

    # Wait for loop to detect PAUSED and also let any in-flight iteration settle
    await asyncio.sleep(0.3)

    events_paused = await store.get_events("run-pause")
    n_paused = len(events_paused)
    event_types_paused = [e.event_type for e in events_paused]

    # RunPaused should be in the stream
    assert EventType.RUN_PAUSED in event_types_paused

    await scheduler.resume("run-pause")
    await asyncio.wait_for(task, timeout=10.0)

    events_final = await store.get_events("run-pause")
    event_types_final = [e.event_type for e in events_final]

    assert EventType.RUN_PAUSED in event_types_final
    assert EventType.RUN_RESUMED in event_types_final
    assert EventType.RUN_COMPLETED in event_types_final
    assert len(events_final) > n_paused, "No events added after resume"


# ── Confirmation denied path ──────────────────────────────────


@pytest.mark.asyncio
async def test_confirmation_denied_writes_tool_failed(store: EventStore):
    """When operator denies a dangerous tool, ToolFailed should be written."""
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

    scheduler = AgentLoopScheduler(store, executor, kernel, [dangerous_def], {"delete_file": delete_fn})

    task = asyncio.create_task(scheduler.run("run-deny", "Deny test"))

    await asyncio.sleep(0.3)

    events = await store.get_events("run-deny")
    cf_payload = None
    for e in events:
        if e.event_type == EventType.CONFIRMATION_REQUESTED:
            cf_payload = e.payload
            break
    assert cf_payload is not None

    from harness import ConfirmationReceivedPayload

    await store.append_event(
        "run-deny",
        EventType.CONFIRMATION_RECEIVED,
        ConfirmationReceivedPayload(
            confirmation_id=cf_payload["confirmation_id"],
            confirmed=False,
            operator_id="op-test",
        ).model_dump(),
    )

    await scheduler.resume("run-deny")
    await asyncio.wait_for(task, timeout=5.0)

    all_events = await store.get_events("run-deny")
    event_types = [e.event_type for e in all_events]

    assert EventType.CONFIRMATION_RECEIVED in event_types
    assert EventType.CONFIRMATION_REQUESTED in event_types
    assert EventType.RUN_RESUMED in event_types
    assert EventType.TOOL_FAILED in event_types
    assert EventType.RUN_COMPLETED in event_types
    # When denied, executor writes ToolFailed directly (no ToolCalled)


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


# ── 3.x Scheduler + Monitor + ContextManager integration ──────


class TestSchedulerWithFullWiring:
    """Verify scheduler runs correctly when monitor AND context_manager are wired."""

    @pytest.mark.asyncio
    async def test_scheduler_with_monitor_and_context_manager(self, store: EventStore):
        cm = ContextManager(store, token_limit=1000, compression_threshold_ratio=0.5, checkpoint_interval=2)
        monitor = RunMonitor(store, max_tokens=100, token_warning_ratio=0.5)
        monitor.attach()

        def dummy_fn(input):
            return {"ok": True}

        tool_def = ToolDefinition(
            name="dummy", description="", idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel([
            ThinkResult(thought="call tool", tool_name="dummy", tool_input={}, token_count=5),
            ThinkResult(thought="call tool again", tool_name="dummy", tool_input={}, token_count=5),
            ThinkResult(thought="done"),
        ])
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store=store, executor=executor, kernel=kernel,
            tool_defs=[tool_def], tool_fns={"dummy": dummy_fn},
            config=SchedulerConfig(max_iterations=5),
            context_manager=cm, monitor=monitor,
        )

        state = await scheduler.run("run-wired", "test full wiring")
        assert state.status == RunStatus.COMPLETED

        events = await store.get_events("run-wired")
        assert any(e.event_type == EventType.CONTEXT_CHECKPOINTED for e in events)


# ── 3.x Inheritance & lifecycle ───────────────────────────────


class TestInheritanceFromBaseScheduler:
    """Verify AgentLoopScheduler properly inherits from BaseScheduler."""

    def test_is_subclass(self):
        from harness.core.scheduler import BaseScheduler
        assert issubclass(AgentLoopScheduler, BaseScheduler)

    def test_inherited_methods_exist(self):
        """pause/cancel/resume/is_active/is_paused should not be defined in AgentLoopScheduler."""
        assert "pause" not in AgentLoopScheduler.__dict__
        assert "cancel" not in AgentLoopScheduler.__dict__
        assert "resume" not in AgentLoopScheduler.__dict__
        assert "is_active" not in AgentLoopScheduler.__dict__
        assert "is_paused" not in AgentLoopScheduler.__dict__
        assert "run" not in AgentLoopScheduler.__dict__

    def test_own_methods_present(self):
        assert "_run_loop" in AgentLoopScheduler.__dict__
        # _run_tool_call / _find_tool_def / _wait_for_resume / _fail
        # are inherited from BaseScheduler now (refactored to eliminate duplication)
        assert hasattr(AgentLoopScheduler, "_run_tool_call")
        assert hasattr(AgentLoopScheduler, "_find_tool_def")
        assert hasattr(AgentLoopScheduler, "_wait_for_resume")

    def test_fail_is_inherited(self):
        """_fail is now unified on BaseScheduler — not overridden.

        The refactoring moved _fail from AgentLoopScheduler into BaseScheduler
        so that both AgentLoopScheduler and PlanningExecutorScheduler share
        the same implementation (which uses 'thought(s)' terminology for both).
        """
        from harness.core.scheduler import BaseScheduler, PlanningExecutorScheduler
        assert AgentLoopScheduler._fail is BaseScheduler._fail
        assert PlanningExecutorScheduler._fail is BaseScheduler._fail
        # All three share the same unified implementation

    @pytest.mark.asyncio
    async def test_is_active_inherited(self, store: EventStore):
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        s = AgentLoopScheduler(store, executor, kernel, [], {})
        assert s.is_active("nonexistent") is False


class TestSeparatePauseConfirmEvents:
    """CT-2: _handle_pause and _wait_for_resume should use separate asyncio.Events."""

    @pytest.mark.asyncio
    async def test_separate_event_dicts_exist(self, store: EventStore):
        kernel = MockAgentKernel([ThinkResult(thought="done")])
        executor = ToolExecutor(store)
        s = AgentLoopScheduler(store, executor, kernel, [], {})
        assert hasattr(s, "_confirm_events")
        assert s._confirm_events is not s._pause_events

    @pytest.mark.asyncio
    async def test_cancel_sets_both_events(self, store: EventStore):
        dangerous_def = ToolDefinition(
            name="delete_file", description="Delete",
            idempotency_key_fields=["path"], side_effects=[SideEffect.DELETE],
            requires_confirmation=True, timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel(
            [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
        )
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [dangerous_def], {"delete_file": lambda i: {"deleted": i["path"]}},
            config=SchedulerConfig(pause_timeout_ms=999999),
        )
        run_id = "run-ct2-cancel"
        task = asyncio.create_task(scheduler.run(run_id, "CT-2 cancel test"))
        await asyncio.sleep(0.3)

        await scheduler.cancel(run_id)
        assert scheduler._cancel_flags.get(run_id).is_set()
        assert scheduler._confirm_events.get(run_id).is_set()
        task.cancel()
        await asyncio.sleep(0.1)


class TestConfirmRetryLimit:
    """CT-4/5: confirmation loops should have a max retry limit."""

    def test_scheduler_config_has_max_confirm_retries(self):
        config = SchedulerConfig(max_confirm_retries=5)
        assert config.max_confirm_retries == 5

    def test_max_confirm_retries_default(self):
        config = SchedulerConfig()
        assert config.max_confirm_retries > 0

    @pytest.mark.asyncio
    async def test_confirm_retry_limit_exceeded(self, store: EventStore):
        """After max_confirm_retries, the loop should fail the run."""
        dangerous_def = ToolDefinition(
            name="delete_file", description="Delete",
            idempotency_key_fields=["path"], side_effects=[SideEffect.DELETE],
            requires_confirmation=True, timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel(
            [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
        )
        executor = ToolExecutor(store)
        config = SchedulerConfig(max_confirm_retries=2, pause_timeout_ms=999999)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [dangerous_def], {"delete_file": lambda i: {"deleted": i["path"]}},
            config=config,
        )
        run_id = "run-ct4"
        task = asyncio.create_task(scheduler.run(run_id, "CT-4 test"))
        await asyncio.sleep(0.3)

        # Simulate calling resume 3 times (exceeds limit of 2)
        for _ in range(3):
            await scheduler.resume(run_id)
            await asyncio.sleep(0.1)

        events = await store.get_events(run_id)
        # Should have RUN_FAILED due to max_confirm_retries exceeded
        assert EventType.RUN_FAILED in [e.event_type for e in events]

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestFailCancelsTask:
    """CT-6: _fail() should cancel the running asyncio.Task."""

    @pytest.mark.asyncio
    async def test_fail_cancels_running_task(self, store: EventStore):
        dangerous_def = ToolDefinition(
            name="delete_file", description="Delete",
            idempotency_key_fields=["path"], side_effects=[SideEffect.DELETE],
            requires_confirmation=True, timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel(
            [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
        )
        executor = ToolExecutor(store)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [dangerous_def], {"delete_file": lambda i: {"deleted": i["path"]}},
        )
        run_id = "run-ct6"
        task = asyncio.create_task(scheduler.run(run_id, "CT-6 test"))
        await asyncio.sleep(0.3)

        assert run_id in scheduler._running_tasks
        await scheduler._fail(run_id, "Forced fail")

        await asyncio.sleep(0.1)
        assert task.cancelled() or task.done()


class TestSeparateTimeoutConfigs:
    """CT-7/8: confirmation timeout and pause timeout should be independently configurable."""
    
    def test_confirm_timeout_separate_from_pause_timeout(self):
        config = SchedulerConfig(confirm_timeout_ms=5000, pause_timeout_ms=30000)
        assert config.confirm_timeout_ms == 5000
        assert config.pause_timeout_ms == 30000

    def test_confirm_timeout_defaults_to_pause_timeout(self):
        config = SchedulerConfig(pause_timeout_ms=30000)
        assert config.confirm_timeout_ms == 30000

    @pytest.mark.asyncio
    async def test_confirm_timeout_uses_confirm_config(self, store: EventStore):
        dangerous_def = ToolDefinition(
            name="delete_file", description="Delete",
            idempotency_key_fields=["path"], side_effects=[SideEffect.DELETE],
            requires_confirmation=True, timeout_ms=5000, retry_policy=RetryPolicy(),
        )
        kernel = MockAgentKernel(
            [ThinkResult(thought="Deleting...", tool_name="delete_file", tool_input={"path": "/tmp/x"})]
        )
        executor = ToolExecutor(store)
        # Short confirm timeout, long pause timeout
        config = SchedulerConfig(confirm_timeout_ms=100, pause_timeout_ms=999999)
        scheduler = AgentLoopScheduler(
            store, executor, kernel, [dangerous_def], {"delete_file": lambda i: {"deleted": i["path"]}},
            config=config,
        )
        run_id = "run-ct7"
        task = asyncio.create_task(scheduler.run(run_id, "CT-7 test"))
        await asyncio.sleep(1.0)  # wait longer than confirm_timeout_ms but shorter than pause_timeout_ms

        events = await store.get_events(run_id)
        # Should have RUN_FAILED due to confirmation timeout (100ms)
        assert EventType.RUN_FAILED in [e.event_type for e in events]

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
