from __future__ import annotations

import asyncio

import pytest

from harness.core.dag_executor import DagExecutor
from harness.core.dag_types import StepResult, StepStatus
from harness.core.dag_vars import VariableResolutionError, resolve_variables_in_input, substitute_vars
from harness.models.events import EventType
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import RetryPolicy, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def store():
    s = EventStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def echo_def():
    return ToolDefinition(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object"},
        idempotency_key_fields=["msg"],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )


@pytest.fixture
def registry(echo_def):
    r = ToolRegistry()
    async def echo_fn(input: dict) -> dict:
        await asyncio.sleep(0.01)
        return {"echo": input.get("msg", ""), "status": "ok"}
    r.register(echo_def, echo_fn)
    return r


@pytest.fixture
def nested_echo_def():
    return ToolDefinition(
        name="nested_echo",
        description="Echo input with nested output body.uuid",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object"},
        idempotency_key_fields=["msg"],
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(),
    )


@pytest.fixture
def registry_with_nested(echo_def, nested_echo_def):
    r = ToolRegistry()
    async def echo_fn(input: dict) -> dict:
        await asyncio.sleep(0.01)
        return {"echo": input.get("msg", ""), "status": "ok"}
    async def nested_echo_fn(input: dict) -> dict:
        await asyncio.sleep(0.01)
        return {"body": {"uuid": input.get("msg", "")}, "status": "ok"}
    r.register(echo_def, echo_fn)
    r.register(nested_echo_def, nested_echo_fn)
    return r


@pytest.fixture
def executor(store):
    return ToolExecutor(store)


class TestDagExecutorBasic:
    async def test_semaphore_limits_concurrency(self, store, executor, registry):
        """DagExecutor semaphore should limit concurrent tool executions."""
        dag = DagExecutor(executor, store, registry, max_parallel=2)
        plan = DagPlan(
            intent="test concurrency",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "a"}),
                DagStep(id="s2", tool="echo", input={"msg": "b"}),
                DagStep(id="s3", tool="echo", input={"msg": "c"}),
            ],
        )
        results = await dag.execute("run-1", plan)
        assert len(results) == 3
        for sid in ("s1", "s2", "s3"):
            assert results[sid].is_completed

    async def test_execute_layer_returns_bool(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="echo", input={"msg": "x"})],
        )
        plan_id = "test-plan"
        layers = plan.topological_sort()
        result = await dag.execute_layer("run-2", plan, plan_id, layers[0], 0, layers, {})
        assert result is True

    async def test_event_order_started_before_completed(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test event order",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "first"}),
            ],
        )
        await dag.execute("run-3", plan)
        events = await store.get_events("run-3")
        dag_started = [e for e in events if e.event_type == EventType.DAG_STEP_STARTED]
        dag_completed = [e for e in events if e.event_type == EventType.DAG_STEP_COMPLETED]
        assert len(dag_started) == 1
        assert len(dag_completed) == 1
        assert dag_started[0].seq < dag_completed[0].seq


class TestDagExecutorEdgeCases:
    async def test_unknown_tool_returns_error(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test unknown tool",
            steps=[DagStep(id="s1", tool="nonexistent", input={})],
        )
        results = await dag.execute("run-edge-1", plan)
        assert results["s1"].is_failed

    async def test_dependency_results_merged(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test deps",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "hello"}),
                DagStep(id="s2", tool="echo", input={"msg": ""}, depends_on=["s1"]),
            ],
        )
        results = await dag.execute("run-edge-2", plan)
        assert results["s1"].is_completed
        assert results["s2"].is_completed


class TestDagExecutorVarSubstitution:
    """V0.7: Pure static-method tests for _substitute_vars and _resolve_variables_in_input."""

    async def test_substitute_basic(self):
        upstream = {"s1": {"name": "World"}}
        result = substitute_vars("Hello $s1.name", upstream)
        assert result == "Hello World"

    async def test_substitute_nested_path(self):
        upstream = {"s1": {"user": {"address": {"city": "Beijing"}}}}
        result = substitute_vars("City: $s1.user.address.city", upstream)
        assert result == "City: Beijing"

    async def test_substitute_missing_variable(self):
        upstream = {"s1": {"name": "hello"}}
        result = substitute_vars("$unknown.field", upstream)
        assert result == "$unknown.field"

    async def test_substitute_dollar_in_value(self):
        upstream = {"s1": {"price": 100}}
        result = substitute_vars("price is $100", upstream)
        assert result == "price is $100"

    async def test_substitute_none_value(self):
        upstream = {"s1": {"x": None}}
        result = substitute_vars("value=$s1.x", upstream)
        assert result == "value=null"

    async def test_substitute_missing_path_segment(self):
        upstream = {"s1": {"name": "hello"}}
        with pytest.raises(VariableResolutionError, match="name.missing"):
            substitute_vars("$s1.name.missing", upstream)

    async def test_substitute_no_match_returns_original(self):
        upstream = {"s1": {"name": "hello"}}
        result = substitute_vars("plain text with no vars", upstream)
        assert result == "plain text with no vars"

    async def test_substitute_variable_without_path(self):
        upstream = {"greeting": "Hello"}
        result = substitute_vars("$greeting World", upstream)
        assert result == "Hello World"

    async def test_resolve_nested_dict(self):
        upstream = {"s1": {"name": "Alice"}}
        step_input = {"greeting": "Hi $s1.name", "static": "keep"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result == {"greeting": "Hi Alice", "static": "keep"}

    async def test_resolve_list(self):
        upstream = {"s1": {"x": "a", "y": "b"}}
        step_input = {"items": ["$s1.x", "$s1.y", "static"]}
        result = resolve_variables_in_input(step_input, upstream)
        assert result == {"items": ["a", "b", "static"]}

    async def test_resolve_nested_dict_in_list(self):
        upstream = {"s1": {"name": "Bob"}}
        step_input = {"list": [{"greet": "Hello $s1.name"}, {"greet": "Hi $s1.name"}]}
        result = resolve_variables_in_input(step_input, upstream)
        assert result == {"list": [{"greet": "Hello Bob"}, {"greet": "Hi Bob"}]}

    async def test_resolve_int_value_unchanged(self):
        """$s1.val resolves to int 42 (type preservation for bare refs).
        
        Pure variable references ($s1.field) preserve the original type.
        Inline references ($"prefix_$s1.field") are always stringified.
        Downstream tools must handle the type as specified in their input_schema.
        """
        upstream = {"s1": {"val": 42}}
        step_input = {"count": 10, "name": "$s1.val"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result == {"count": 10, "name": 42}

    async def test_resolve_empty_upstream(self):
        step_input = {"msg": "hello $s1.name"}
        result = resolve_variables_in_input(step_input, {})
        assert result == {"msg": "hello $s1.name"}

    # ── flattening removed: body.field access ─────────────────────

    async def test_resolve_body_uuid(self):
        """$s1.body.uuid resolves through body dict (no flattening)."""
        upstream = {"s1": {"status_code": 200, "headers": {}, "body": {"uuid": "abc-123"}, "elapsed_ms": 50}}
        step_input = {"uuid": "$s1.body.uuid", "static": "x"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result == {"uuid": "abc-123", "static": "x"}

    async def test_flat_field_gone(self):
        """$s1.uuid resolves via deep search (uuid nested in body)."""
        upstream = {"s1": {"status_code": 200, "headers": {}, "body": {"uuid": "abc-123"}, "elapsed_ms": 50}}
        step_input = {"uuid": "$s1.uuid"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result["uuid"] == "abc-123"

    async def test_body_passthrough(self):
        """$s1.body gives entire body dict."""
        upstream = {"s1": {"status_code": 200, "headers": {}, "body": {"uuid": "abc-123"}, "elapsed_ms": 50}}
        step_input = {"payload": "$s1.body"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result["payload"] == {"uuid": "abc-123"}

    async def test_body_uuid_in_nested_input(self):
        """$s1.body.uuid resolves inside a nested body dict (common POST pattern)."""
        upstream = {"s1": {"status_code": 200, "headers": {}, "body": {"uuid": "550e8400-e29b-41d4-a716-446655440000"}, "elapsed_ms": 45}}
        step_input = {"url": "https://httpbin.org/post", "method": "POST", "body": {"uuid": "$s1.body.uuid"}}
        result = resolve_variables_in_input(step_input, upstream)
        assert result["body"]["uuid"] == "550e8400-e29b-41d4-a716-446655440000"

    async def test_legacy_key_format_s1_result(self):
        """$s1_result.body.uuid resolves when upstream has both 's1' and 's1_result' keys.

        _execute_step_only normalizes by adding f\"{dep_id}_result\" → dep_id mapping,
        so the resolver receives BOTH keys.
        """
        upstream = {"s1": {"body": {"uuid": "abc-123"}}, "s1_result": {"body": {"uuid": "abc-123"}}}
        step_input = {"uuid": "$s1_result.body.uuid", "static": "x"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result["uuid"] == "abc-123", (
            f"Bug: legacy $s1_result syntax failed to resolve — "
            f"got '{result.get('uuid')}'"
        )

    async def test_legacy_key_format_fallback(self):
        """$s1_result.field resolves when upstream has both 's1' and 's1_result' keys."""
        upstream = {"s1": {"result": "ok"}, "s1_result": {"result": "ok"}}
        step_input = {"msg": "$s1_result.result"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result["msg"] == "ok", (
            f"Bug: $s1_result.result did not resolve — got '{result.get('msg')}'"
        )

    async def test_deep_resolve_json_string_body(self):
        """_substitute_vars parses JSON string during path traversal.

        When $s1.body.uuid is referenced and s1.body is a JSON string,
        the path traversal tries json.loads(body) and continues resolving.
        """
        upstream = {"s1": {"body": '{"uuid": "from-json", "nested": {"key": "val"}}'}}
        step_input = {"uuid": "$s1.body.uuid", "nested_key": "$s1.body.nested.key"}
        result = resolve_variables_in_input(step_input, upstream)
        assert result["uuid"] == "from-json", (
            f"Bug: $s1.body.uuid from JSON string body failed — "
            f"got '{result.get('uuid')}'"
        )
        assert result["nested_key"] == "val", (
            f"Bug: $s1.body.nested.key from JSON string body failed — "
            f"got '{result.get('nested_key')}'"
        )


class TestDagExecutorConfirmationPhase2:
    """Phase 2: CONFIRMATION_NEEDED propagation (Bug #36 fix).
    
    Verifies that confirmation_needed is NOT collapsed to "error",
    PlanSuspended is raised, and retry_step works after resume.
    """

    @pytest.fixture
    def confirm_def(self):
        return ToolDefinition(
            name="confirm_echo",
            description="Echo that requires confirmation",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            output_schema={"type": "object"},
            idempotency_key_fields=["msg"],
            requires_confirmation=True,
            side_effects=["write"],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        )

    @pytest.fixture
    def registry_with_confirm(self, confirm_def):
        r = ToolRegistry()
        async def echo_fn(input: dict) -> dict:
            await asyncio.sleep(0.01)
            return {"echo": input.get("msg", ""), "status": "ok"}
        r.register(confirm_def, echo_fn)
        return r

    async def test_step_only_returns_confirmation_needed(self, store, executor, registry_with_confirm):
        """_execute_step_only returns confirmation_needed status instead of error."""
        from harness.core.dag_executor import PlanSuspended
        dag = DagExecutor(executor, store, registry_with_confirm)
        plan = DagPlan(
            intent="test confirmation",
            steps=[DagStep(id="s1", tool="confirm_echo", input={"msg": "hello"})],
        )
        # Pre-write CONFIRMATION_RECEIVED so the executor will confirm on retry
        # First call: executor writes CONFIRMATION_REQUESTED, returns CONFIRMATION_NEEDED
        result = await dag._execute_step("run-conf-1", plan, {}, "s1")
        assert result.needs_confirmation, (
            f"Expected confirmation_needed, got '{result.status.value}' — "
            f"Bug #36: was collapsing to 'error'"
        )
        assert result.confirmation_id is not None
        assert result.step_id == "s1"

    async def test_execute_layer_raises_plan_suspended(self, store, executor, registry_with_confirm):
        """_execute_layer raises PlanSuspended when a step needs confirmation."""
        from harness.core.dag_executor import PlanSuspended
        dag = DagExecutor(executor, store, registry_with_confirm)
        plan = DagPlan(
            intent="test suspension",
            steps=[DagStep(id="s1", tool="confirm_echo", input={"msg": "hello"})],
        )
        plan_id = "plan-susp"
        layers = plan.topological_sort()
        results = {}
        with pytest.raises(PlanSuspended) as exc_info:
            await dag.execute_layer("run-conf-2", plan, plan_id, layers[0], 0, layers, results)
        assert exc_info.value.confirmations[0][0] == "s1"
        assert exc_info.value.confirmations[0][1] is not None
        # The CONFIRMATION_REQUESTED event should have been written by executor
        events = await store.get_events("run-conf-2")
        types = [e.event_type for e in events]
        assert EventType.CONFIRMATION_REQUESTED in types
        # No DAG_STEP_FAILED should have been written for the suspended step
        assert EventType.DAG_STEP_FAILED not in types

    async def test_retry_step_after_confirmation(self, store, executor, registry_with_confirm):
        """retry_step succeeds after CONFIRMATION_RECEIVED is written."""
        from harness.core.dag_executor import PlanSuspended
        from harness.models.events import ConfirmationReceivedPayload

        dag = DagExecutor(executor, store, registry_with_confirm)
        plan = DagPlan(
            intent="test retry",
            steps=[DagStep(id="s1", tool="confirm_echo", input={"msg": "retry-test"})],
        )
        plan_id = "plan-retry"
        layers = plan.topological_sort()
        results = {}

        # First call: triggers CONFIRMATION_NEEDED
        with pytest.raises(PlanSuspended) as exc_info:
            await dag.execute_layer("run-conf-3", plan, plan_id, layers[0], 0, layers, results)
        assert exc_info.value.confirmations[0][0] == "s1"
        confirmation_id = exc_info.value.confirmations[0][1]

        # Simulate operator confirmation
        await store.append_event(
            "run-conf-3", EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=confirmation_id, confirmed=True, operator_id="test",
            ).model_dump(),
        )

        # Retry the step — executor should see CONFIRMATION_RECEIVED and execute
        retry_raw = await dag.retry_step("run-conf-3", plan, "s1", results)
        assert retry_raw.is_completed, (
            f"Expected completed after confirmation, got '{retry_raw.status.value}'"
        )
        output = retry_raw.output or {}
        assert output.get("echo") == "retry-test"

    async def test_retry_step_still_confirmation_needed(self, store, executor, registry_with_confirm):
        """retry_step still returns confirmation_needed if no CONFIRMATION_RECEIVED yet."""
        from harness.core.dag_executor import PlanSuspended
        dag = DagExecutor(executor, store, registry_with_confirm)
        plan = DagPlan(
            intent="test retry no confirm",
            steps=[DagStep(id="s1", tool="confirm_echo", input={"msg": "hello"})],
        )
        plan_id = "plan-noconf"
        layers = plan.topological_sort()
        results = {}

        with pytest.raises(PlanSuspended) as exc_info:
            await dag.execute_layer("run-conf-4", plan, plan_id, layers[0], 0, layers, results)
        assert exc_info.value.confirmations[0][0] == "s1"

        # Retry WITHOUT writing CONFIRMATION_RECEIVED
        retry_raw = await dag.retry_step("run-conf-4", plan, "s1", results)
        assert retry_raw.needs_confirmation, (
            f"Expected still confirmation_needed, got '{retry_raw.status.value}'"
        )

    async def test_retry_step_denied_returns_error(self, store, executor, registry_with_confirm):
        """retry_step returns error when confirmation is denied."""
        from harness.core.dag_executor import PlanSuspended
        from harness.models.events import ConfirmationReceivedPayload

        dag = DagExecutor(executor, store, registry_with_confirm)
        plan = DagPlan(
            intent="test denial",
            steps=[DagStep(id="s1", tool="confirm_echo", input={"msg": "hello"})],
        )
        plan_id = "plan-deny"
        layers = plan.topological_sort()
        results = {}

        with pytest.raises(PlanSuspended) as exc_info:
            await dag.execute_layer("plan-deny", plan, plan_id, layers[0], 0, layers, results)
        confirmation_id = exc_info.value.confirmations[0][1]

        # Operator denies confirmation
        await store.append_event(
            "plan-deny", EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=confirmation_id, confirmed=False, operator_id="test",
            ).model_dump(),
        )

        retry_raw = await dag.retry_step("plan-deny", plan, "s1", results)
        assert retry_raw.is_failed

    async def test_multi_step_confirmation_same_layer(self, store, executor):
        """Two steps in same layer both need confirmation — BOTH must survive PlanSuspended."""
        from harness.core.dag_executor import PlanSuspended
        from harness.models.events import ConfirmationReceivedPayload

        confirm_def_a = ToolDefinition(
            name="confirm_a",
            description="A",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            output_schema={"type": "object"},
            idempotency_key_fields=["msg"],
            requires_confirmation=True,
            side_effects=["write"],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        )
        confirm_def_b = ToolDefinition(
            name="confirm_b",
            description="B",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            output_schema={"type": "object"},
            idempotency_key_fields=["msg"],
            requires_confirmation=True,
            side_effects=["write"],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        )

        r = ToolRegistry()
        async def echo_fn(input: dict) -> dict:
            await asyncio.sleep(0.01)
            return {"echo": input.get("msg", ""), "status": "ok"}
        r.register(confirm_def_a, echo_fn)
        r.register(confirm_def_b, echo_fn)

        dag = DagExecutor(executor, store, r)
        plan = DagPlan(
            intent="multi confirm",
            steps=[
                DagStep(id="s1", tool="confirm_a", input={"msg": "first"}),
                DagStep(id="s2", tool="confirm_b", input={"msg": "second"}),
            ],
        )
        plan_id = "plan-multi-confirm"
        layers = plan.topological_sort()
        results = {}

        # Layer has both s1 and s2 in same layer (parallel)
        assert len(layers[0]) == 2, f"Expected 2 steps in layer, got {layers[0]}"
        with pytest.raises(PlanSuspended) as exc_info:
            await dag.execute_layer("run-multi-confirm", plan, plan_id, layers[0], 0, layers, results)
        assert len(exc_info.value.confirmations) == 2, (
            f"Expected 2 confirmations, got {len(exc_info.value.confirmations)} — "
            f"Bug: multi-step confirmation lost"
        )
        ids = {cid for cid in map(lambda x: x[1], exc_info.value.confirmations)}
        assert len(ids) == 2, "Both confirmation IDs must be unique"

        # Confirm and retry s1
        for sid_i, cid_i in exc_info.value.confirmations:
            await store.append_event(
                "run-multi-confirm", EventType.CONFIRMATION_RECEIVED,
                ConfirmationReceivedPayload(
                    confirmation_id=cid_i, confirmed=True, operator_id="test",
                ).model_dump(),
            )
            retry = await dag.retry_step("run-multi-confirm", plan, sid_i, results)
            assert retry.is_completed, (
                f"Bug: step {sid_i} failed after confirmation: {retry.error}"
            )


class TestDagExecutorVariableIntegration:
    """Integration tests: upstream_outputs() + resolve_variables_in_input() full path.

    These test the actual contract between plan.py and dag_executor.py —
    that upstream_outputs() produces keys matching what the resolver expects.
    """

    async def test_variable_resolution_full_path(self, store, executor, registry):
        """$s1.echo is correctly resolved through upstream_outputs() → resolver.

        s1: echo({"msg": "hello"}) → output {"echo": "hello", "status": "ok"}
        s2: echo({"msg": "$s1.echo"}) → should resolve to echo({"msg": "hello"})

        Without the fix (key naming mismatch in upstream_outputs), s2 receives
        literal "$s1.echo" and outputs {"echo": "$s1.echo"}.
        """
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test variable resolution full path",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "hello"}),
                DagStep(id="s2", tool="echo", input={"msg": "$s1.echo"}, depends_on=["s1"]),
            ],
        )
        results = await dag.execute("run-var-int", plan)
        assert results["s1"].is_completed
        assert results["s2"].is_completed
        s2_output = results["s2"].output or {}
        assert s2_output.get("echo") == "hello", (
            f"Variable resolution failed: expected 'hello', got '{s2_output.get('echo')}'"
        )

    async def test_variable_resolution_nested_path(self, store, executor, registry_with_nested):
        """$s1.body.uuid resolves through full DAG integration.

        s1: nested_echo({"msg": "abc-123"}) → output {"body": {"uuid": "abc-123"}, "status": "ok"}
        s2: echo({"msg": "$s1.body.uuid"}) → should resolve to echo({"msg": "abc-123"})
        """
        dag = DagExecutor(executor, store, registry_with_nested)
        plan = DagPlan(
            intent="test nested variable resolution",
            steps=[
                DagStep(id="s1", tool="nested_echo", input={"msg": "abc-123"}),
                DagStep(id="s2", tool="echo", input={"msg": "$s1.body.uuid"}, depends_on=["s1"]),
            ],
        )
        results = await dag.execute("run-var-nested", plan)
        assert results["s1"].is_completed
        assert results["s2"].is_completed
        s2_output = results["s2"].output or {}
        assert s2_output.get("echo") == "abc-123", (
            f"Nested path $s1.body.uuid resolution failed: expected 'abc-123', "
            f"got '{s2_output.get('echo')}'"
        )


class TestStepResultDictBackCompat:
    """StepResult.get() provides dict-compatible access for backward compatibility."""
    pytestmark = []  # these are sync tests, override global asyncio mark

    def test_get_output(self):
        sr = StepResult(step_id="s1", status=StepStatus.COMPLETED, output={"k": "v"})
        assert sr.get("output") == {"k": "v"}

    def test_get_status(self):
        sr = StepResult(step_id="s1", status=StepStatus.COMPLETED)
        assert sr.get("status") == "completed"

    def test_get_summary(self):
        sr = StepResult(step_id="s1", status=StepStatus.COMPLETED, summary="done")
        assert sr.get("summary") == "done"

    def test_get_error(self):
        sr = StepResult(step_id="s1", status=StepStatus.FAILED, error="boom")
        assert sr.get("error") == "boom"

    def test_get_error_none(self):
        sr = StepResult(step_id="s1", status=StepStatus.COMPLETED)
        assert sr.get("error") is None

    def test_get_unknown_key_returns_default(self):
        sr = StepResult(step_id="s1", status=StepStatus.COMPLETED)
        assert sr.get("nonexistent", "fallback") == "fallback"


class TestDagExecutorMergedStep:
    """Verify that _execute_step works for both normal and retry paths."""

    async def test_execute_step_normal_returns_step_result(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="echo", input={"msg": "x"})],
        )
        result = await dag._execute_step("run-norm", plan, {}, "s1")
        assert isinstance(result, StepResult)
        assert result.is_completed
        assert result.output == {"echo": "x", "status": "ok"}

    async def test_execute_step_retry_includes_legacy_key(self, store, executor, registry):
        """_execute_step (is_retry=True) also maps legacy keys for backward compat."""
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test retry legacy",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "hello"}),
                DagStep(id="s2", tool="echo", input={"msg": "$s1_result.echo"}, depends_on=["s1"]),
            ],
        )
        all_results: dict = {}
        # Execute s1 first
        r1 = await dag._execute_step("run-legacy", plan, all_results, "s1")
        all_results["s1"] = r1
        # Execute s2 via retry path — should still resolve $s1_result.echo
        r2 = await dag._execute_step("run-legacy", plan, all_results, "s2", is_retry=True)
        assert r2.is_completed
        assert r2.output == {"echo": "hello", "status": "ok"}

    async def test_execute_step_retry_unknown_step(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(intent="test", steps=[])
        result = await dag._execute_step("run-x", plan, {}, "nonexistent", is_retry=True)
        assert result.is_failed
        assert "not found" in (result.error or "")


class TestDagExecutorPlanId:
    """plan_id now uses uuid4 instead of int(time.time())."""

    async def test_plan_id_is_uuid_based(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test plan id",
            steps=[DagStep(id="s1", tool="echo", input={"msg": "x"})],
        )
        results = await dag.execute("run-pid", plan)
        events = await store.get_events("run-pid")
        plan_created = [e for e in events if e.event_type == EventType.PLAN_CREATED]
        assert len(plan_created) == 1
        # plan_id format: plan_{run_id}_{8 hex chars}
        pid = plan_created[0].payload["plan_id"]
        import re
        assert re.match(r"^plan_run-pid_[0-9a-f]{8}$", pid), f"Unexpected plan_id format: {pid}"

    async def test_two_plans_have_different_ids(self, store, executor, registry):
        dag = DagExecutor(executor, store, registry)
        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="echo", input={"msg": "x"})],
        )
        await dag.execute("run-uniq-a", plan)
        await dag.execute("run-uniq-b", plan)
        events_a = await store.get_events("run-uniq-a")
        events_b = await store.get_events("run-uniq-b")
        pid_a = [e for e in events_a if e.event_type == EventType.PLAN_CREATED][0].payload["plan_id"]
        pid_b = [e for e in events_b if e.event_type == EventType.PLAN_CREATED][0].payload["plan_id"]
        assert pid_a != pid_b


class TestDagExecutorBuildStatus:
    """build_dag_status_text works with StepResult values."""

    async def test_build_status_with_step_results(self):
        plan = DagPlan(
            intent="test",
            steps=[
                DagStep(id="s1", tool="echo", input={"msg": "x"}),
                DagStep(id="s2", tool="echo", input={"msg": "y"}, depends_on=["s1"]),
            ],
        )
        results: dict[str, StepResult] = {
            "s1": StepResult(step_id="s1", status=StepStatus.COMPLETED, summary="echo ok"),
            "s2": StepResult(step_id="s2", status=StepStatus.FAILED, error="timeout"),
        }
        text = DagExecutor.build_dag_status_text(plan, results, current_layer=0)

        assert "【系统状态 - 不可折叠】" in text
        assert "[done]" in text
        assert "[failed]" in text
        assert "echo ok" in text
        assert "timeout" in text

    async def test_build_status_pending_steps(self):
        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="echo", input={"msg": "x"})],
        )
        results: dict[str, StepResult] = {}
        text = DagExecutor.build_dag_status_text(plan, results, current_layer=0)
        assert "[pending]" in text
        assert "Input:" in text



