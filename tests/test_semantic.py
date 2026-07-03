"""Tests for semantic evaluation — SuccessIndicator, SemanticEvaluator, and executor integration.

Covers the P4 (429 semantic detection) fix: tool outputs that are
structurally complete but semantically failed are now recorded as
TOOL_COMPLETED with result_type=SOFT_ERROR instead of TOOL_FAILED.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from harness.models.events import Event, EventType, ToolCompletedPayload, ToolResultType
from harness.models.tools import SideEffect, SuccessIndicator, ToolDefinition
from harness.tools.semantic import SemanticEvaluator
from harness.tools.executor import ExecutionStatus
from harness.storage.event_store import EventStore
from harness.core.fold import fold_events, ToolResultStatus
from harness.core.dag_types import StepResult, StepStatus


# ── SuccessIndicator model tests ──────────────────────────────────


class TestSuccessIndicatorModel:
    def test_eq_boolean(self):
        ind = SuccessIndicator(field="success", op="eq", value=True)
        assert ind.field == "success"
        assert ind.op == "eq"
        assert ind.value is True

    def test_lt_numeric(self):
        ind = SuccessIndicator(field="status_code", op="lt", value=400)
        assert ind.field == "status_code"
        assert ind.op == "lt"
        assert ind.value == 400

    def test_in_list(self):
        ind = SuccessIndicator(field="code", op="in", value=[200, 201, 204])
        assert ind.value == [200, 201, 204]

    def test_field_is_required(self):
        with pytest.raises(Exception):
            SuccessIndicator(op="eq", value=True)

    def test_op_is_required(self):
        with pytest.raises(Exception):
            SuccessIndicator(field="x", value=True)

    def test_value_can_be_none(self):
        ind = SuccessIndicator(field="x", op="eq", value=None)
        assert ind.value is None


# ── ToolCompletedPayload backward compatibility ───────────────────


class TestToolCompletedPayloadBackCompat:
    def test_defaults_when_fields_missing(self):
        tp = ToolCompletedPayload(
            tool_call_id="a",
            tool_name="b",
            output={"x": 1},
            duration_ms=100,
        )
        assert tp.result_type == ToolResultType.SUCCESS
        assert tp.error is None

    def test_round_trip_through_dict(self):
        tp = ToolCompletedPayload(
            tool_call_id="a",
            tool_name="b",
            output={"x": 1},
            duration_ms=100,
        )
        d = tp.model_dump()
        reloaded = ToolCompletedPayload(**d)
        assert reloaded.result_type == ToolResultType.SUCCESS
        assert reloaded.error is None

    def test_round_trip_with_soft_error(self):
        tp = ToolCompletedPayload(
            tool_call_id="a",
            tool_name="b",
            output={"status_code": 429, "body": ""},
            duration_ms=200,
            result_type=ToolResultType.SOFT_ERROR,
            error="status_code=429 (op=lt, value=400)",
        )
        d = tp.model_dump()
        reloaded = ToolCompletedPayload(**d)
        assert reloaded.result_type == ToolResultType.SOFT_ERROR
        assert reloaded.error == "status_code=429 (op=lt, value=400)"

    def test_deserialize_old_event_without_result_type(self):
        old_payload = {
            "tool_call_id": "a",
            "tool_name": "b",
            "output": {"x": 1},
            "duration_ms": 100,
        }
        tp = ToolCompletedPayload(**old_payload)
        assert tp.result_type == ToolResultType.SUCCESS
        assert tp.error is None


# ── SemanticEvaluator pure-function tests ─────────────────────────


class TestSemanticEvaluator:
    def _def(self, indicator: SuccessIndicator | None = None) -> ToolDefinition:
        return ToolDefinition(
            name="test",
            description="",
            side_effects=[],
            success_indicator=indicator,
        )

    # ── No indicator / edge cases ─────────────────────────────────

    def test_no_indicator_returns_success(self):
        td = self._def(indicator=None)
        result, error = SemanticEvaluator.evaluate({"status_code": 429}, td)
        assert result == ToolResultType.SUCCESS
        assert error is None

    def test_non_dict_output_returns_success(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        result, error = SemanticEvaluator.evaluate("plain string", td)
        assert result == ToolResultType.SUCCESS
        assert error is None

    def test_field_missing_in_output_returns_success(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        result, error = SemanticEvaluator.evaluate({"other": 1}, td)
        assert result == ToolResultType.SUCCESS
        assert error is None

    def test_field_is_none_returns_success(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        result, error = SemanticEvaluator.evaluate({"success": None}, td)
        assert result == ToolResultType.SUCCESS
        assert error is None

    # ── eq ────────────────────────────────────────────────────────

    def test_eq_success(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        result, _ = SemanticEvaluator.evaluate({"success": True}, td)
        assert result == ToolResultType.SUCCESS

    def test_eq_failure(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        result, error = SemanticEvaluator.evaluate({"success": False, "error": "bad"}, td)
        assert result == ToolResultType.SOFT_ERROR
        assert error == "bad"

    def test_eq_failure_no_error_key(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        result, error = SemanticEvaluator.evaluate({"success": False}, td)
        assert result == ToolResultType.SOFT_ERROR
        assert "success=False" in error

    # ── ne ────────────────────────────────────────────────────────

    def test_ne_success(self):
        td = self._def(SuccessIndicator(field="status", op="ne", value="error"))
        result, _ = SemanticEvaluator.evaluate({"status": "ok"}, td)
        assert result == ToolResultType.SUCCESS

    def test_ne_failure(self):
        td = self._def(SuccessIndicator(field="status", op="ne", value="error"))
        result, _ = SemanticEvaluator.evaluate({"status": "error"}, td)
        assert result == ToolResultType.SOFT_ERROR

    # ── lt (HTTP status_code < 400) ───────────────────────────────

    def test_lt_success_200(self):
        td = self._def(SuccessIndicator(field="status_code", op="lt", value=400))
        result, _ = SemanticEvaluator.evaluate({"status_code": 200}, td)
        assert result == ToolResultType.SUCCESS

    def test_lt_success_399(self):
        td = self._def(SuccessIndicator(field="status_code", op="lt", value=400))
        result, _ = SemanticEvaluator.evaluate({"status_code": 399}, td)
        assert result == ToolResultType.SUCCESS

    def test_lt_failure_400(self):
        td = self._def(SuccessIndicator(field="status_code", op="lt", value=400))
        result, _ = SemanticEvaluator.evaluate({"status_code": 400}, td)
        assert result == ToolResultType.SOFT_ERROR

    def test_lt_failure_429(self):
        td = self._def(SuccessIndicator(field="status_code", op="lt", value=400))
        result, _ = SemanticEvaluator.evaluate({"status_code": 429}, td)
        assert result == ToolResultType.SOFT_ERROR

    def test_lt_failure_500(self):
        td = self._def(SuccessIndicator(field="status_code", op="lt", value=400))
        result, _ = SemanticEvaluator.evaluate({"status_code": 500}, td)
        assert result == ToolResultType.SOFT_ERROR

    # ── lte ───────────────────────────────────────────────────────

    def test_lte_success(self):
        td = self._def(SuccessIndicator(field="count", op="lte", value=10))
        result, _ = SemanticEvaluator.evaluate({"count": 10}, td)
        assert result == ToolResultType.SUCCESS

    def test_lte_failure(self):
        td = self._def(SuccessIndicator(field="count", op="lte", value=10))
        result, _ = SemanticEvaluator.evaluate({"count": 11}, td)
        assert result == ToolResultType.SOFT_ERROR

    # ── gt ────────────────────────────────────────────────────────

    def test_gt_success(self):
        td = self._def(SuccessIndicator(field="score", op="gt", value=0))
        result, _ = SemanticEvaluator.evaluate({"score": 5}, td)
        assert result == ToolResultType.SUCCESS

    def test_gt_failure(self):
        td = self._def(SuccessIndicator(field="score", op="gt", value=0))
        result, _ = SemanticEvaluator.evaluate({"score": 0}, td)
        assert result == ToolResultType.SOFT_ERROR

    # ── gte ───────────────────────────────────────────────────────

    def test_gte_success(self):
        td = self._def(SuccessIndicator(field="level", op="gte", value=1))
        result, _ = SemanticEvaluator.evaluate({"level": 1}, td)
        assert result == ToolResultType.SUCCESS

    def test_gte_failure(self):
        td = self._def(SuccessIndicator(field="level", op="gte", value=1))
        result, _ = SemanticEvaluator.evaluate({"level": 0}, td)
        assert result == ToolResultType.SOFT_ERROR

    # ── in ────────────────────────────────────────────────────────

    def test_in_success(self):
        td = self._def(SuccessIndicator(field="code", op="in", value=[200, 201, 204]))
        result, _ = SemanticEvaluator.evaluate({"code": 201}, td)
        assert result == ToolResultType.SUCCESS

    def test_in_failure(self):
        td = self._def(SuccessIndicator(field="code", op="in", value=[200, 201, 204]))
        result, _ = SemanticEvaluator.evaluate({"code": 400}, td)
        assert result == ToolResultType.SOFT_ERROR

    def test_in_empty_list(self):
        td = self._def(SuccessIndicator(field="code", op="in", value=[]))
        result, _ = SemanticEvaluator.evaluate({"code": 200}, td)
        assert result == ToolResultType.SOFT_ERROR

    # ── error extraction priority ─────────────────────────────────

    def test_error_from_error_field(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        _, error = SemanticEvaluator.evaluate(
            {"success": False, "error": "explicit error", "message": "msg"}, td
        )
        assert error == "explicit error"

    def test_error_from_message_field(self):
        td = self._def(SuccessIndicator(field="success", op="eq", value=True))
        _, error = SemanticEvaluator.evaluate(
            {"success": False, "message": "fallback message"}, td
        )
        assert error == "fallback message"


# ── ToolExecutor integration tests ────────────────────────────────


class TestExecutorSemanticEvaluation:
    """End-to-end: executor invokes a tool, SemanticEvaluator detects SOFT_ERROR."""

    @pytest.fixture
    def store(self):
        return EventStore(db_path=":memory:")

    @pytest.fixture
    async def init_store(self, store):
        await store.initialize()
        return store

    @pytest.mark.asyncio
    async def test_http_429_detected_as_soft_error(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.http_request import HTTP_REQUEST_DEF

        executor = ToolExecutor(store=store)

        async def fake_http(input: dict[str, Any]) -> dict[str, Any]:
            return {
                "status_code": 429,
                "headers": {"Retry-After": "60"},
                "body": '{"error": "rate limit exceeded"}',
                "elapsed_ms": 120,
            }

        run_id = "run-429-test"
        result = await executor.execute(
            run_id=run_id,
            tool_name="http_request",
            input={"url": "https://api.example.com/data"},
            tool_def=HTTP_REQUEST_DEF,
            tool_fn=fake_http,
        )

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is True
        assert result.output["status_code"] == 429

        events = await store.get_events(run_id)
        completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
        assert len(completed) == 1
        tp = ToolCompletedPayload.model_validate(completed[0].payload)
        assert tp.result_type == ToolResultType.SOFT_ERROR
        assert tp.error is not None

    @pytest.mark.asyncio
    async def test_http_200_is_success(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.http_request import HTTP_REQUEST_DEF

        executor = ToolExecutor(store=store)

        async def fake_http(input: dict[str, Any]) -> dict[str, Any]:
            return {
                "status_code": 200,
                "headers": {},
                "body": '{"data": "ok"}',
                "elapsed_ms": 50,
            }

        run_id = "run-200-test"
        result = await executor.execute(
            run_id=run_id,
            tool_name="http_request",
            input={"url": "https://api.example.com/data"},
            tool_def=HTTP_REQUEST_DEF,
            tool_fn=fake_http,
        )

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is False

        events = await store.get_events(run_id)
        completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
        tp = ToolCompletedPayload.model_validate(completed[0].payload)
        assert tp.result_type == ToolResultType.SUCCESS
        assert tp.error is None

    @pytest.mark.asyncio
    async def test_file_op_success_false_is_soft_error(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.file_op import FILE_OP_DEF

        executor = ToolExecutor(store=store)

        async def fake_file_op(input: dict[str, Any]) -> dict[str, Any]:
            return {"success": False, "error": "File not found: /tmp/nope.txt"}

        run_id = "run-fop-fail"
        result = await executor.execute(
            run_id=run_id,
            tool_name="file_op",
            input={"operation": "read", "path": "/tmp/nope.txt"},
            tool_def=FILE_OP_DEF,
            tool_fn=fake_file_op,
        )

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is True
        assert "File not found" in result.error

        events = await store.get_events(run_id)
        completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
        assert len(completed) == 1
        tp = ToolCompletedPayload.model_validate(completed[0].payload)
        assert tp.result_type == ToolResultType.SOFT_ERROR

    @pytest.mark.asyncio
    async def test_file_op_success_true_is_success(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.file_op import FILE_OP_DEF

        executor = ToolExecutor(store=store)

        async def fake_file_op(input: dict[str, Any]) -> dict[str, Any]:
            return {"success": True, "content": "hello", "size": 5}

        run_id = "run-fop-ok"
        result = await executor.execute(
            run_id=run_id,
            tool_name="file_op",
            input={"operation": "read", "path": "/tmp/f.txt"},
            tool_def=FILE_OP_DEF,
            tool_fn=fake_file_op,
        )

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is False

    @pytest.mark.asyncio
    async def test_idempotency_cache_propagates_semantic_error(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.http_request import HTTP_REQUEST_DEF

        executor = ToolExecutor(store=store)
        call_count = 0

        async def fake_http(input: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {
                "status_code": 429,
                "headers": {},
                "body": "",
                "elapsed_ms": 10,
            }

        run_id = "run-idem-se"
        input_data = {"url": "https://api.example.com/data"}

        result1 = await executor.execute(
            run_id=run_id, tool_name="http_request", input=input_data,
            tool_def=HTTP_REQUEST_DEF, tool_fn=fake_http,
        )
        assert result1.status == ExecutionStatus.COMPLETED
        assert result1.has_semantic_error is True
        assert call_count == 1

        result2 = await executor.execute(
            run_id=run_id, tool_name="http_request", input=input_data,
            tool_def=HTTP_REQUEST_DEF, tool_fn=fake_http,
        )
        assert result2.status == ExecutionStatus.IDEMPOTENCY_HIT
        assert result2.has_semantic_error is True, "idempotency hit must propagate semantic error"
        assert result2.error is not None
        assert call_count == 1, "second call should hit cache, not invoke tool_fn"

    @pytest.mark.asyncio
    async def test_tool_without_indicator_still_works(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor

        executor = ToolExecutor(store=store)
        td = ToolDefinition(
            name="no_indicator",
            description="A tool with no success_indicator",
            side_effects=[],
            timeout_ms=5000,
        )

        async def fn(input: dict[str, Any]) -> dict[str, Any]:
            return {"status": "bad", "error": "something"}

        run_id = "run-no-ind"
        result = await executor.execute(
            run_id=run_id,
            tool_name="no_indicator",
            input={},
            tool_def=td,
            tool_fn=fn,
        )

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is False

        events = await store.get_events(run_id)
        completed = [e for e in events if e.event_type == EventType.TOOL_COMPLETED]
        tp = ToolCompletedPayload.model_validate(completed[0].payload)
        assert tp.result_type == ToolResultType.SUCCESS


# ── StepResult property tests ─────────────────────────────────────


class TestStepResultProperties:
    def test_is_done_covers_completed(self):
        sr = StepResult(step_id="s1", status=StepStatus.COMPLETED)
        assert sr.is_completed is True
        assert sr.is_done is True
        assert sr.is_failed is False
        assert sr.has_soft_error is False

    def test_is_done_covers_soft_error(self):
        sr = StepResult(step_id="s1", status=StepStatus.SOFT_ERROR, error="e")
        assert sr.is_completed is False
        assert sr.is_done is True
        assert sr.is_failed is False
        assert sr.has_soft_error is True

    def test_soft_error_not_failed(self):
        sr = StepResult(step_id="s1", status=StepStatus.SOFT_ERROR)
        assert sr.is_failed is False
        assert sr.needs_confirmation is False


# ── Fold integration tests ────────────────────────────────────────


class TestFoldSoftError:
    def test_soft_error_tool_completed_folds_to_soft_error_status(self):
        events = [
            Event(
                run_id="r", seq=1, event_type=EventType.RUN_STARTED,
                payload={"intent": "test"}, created_at=1.0,
            ),
            Event(
                run_id="r", seq=2, event_type=EventType.TOOL_CALLED,
                payload={"tool_call_id": "a", "tool_name": "t", "input": {}},
                created_at=2.0,
            ),
            Event(
                run_id="r", seq=3, event_type=EventType.TOOL_COMPLETED,
                payload={
                    "tool_call_id": "a",
                    "tool_name": "t",
                    "output": {"status_code": 429},
                    "duration_ms": 100,
                    "result_type": "soft_error",
                    "error": "status_code=429 (op=lt, value=400)",
                },
                created_at=3.0,
            ),
        ]
        state = fold_events(events)
        assert len(state.tool_results) == 1
        tr = state.tool_results[0]
        assert tr.status == ToolResultStatus.SOFT_ERROR
        assert tr.error == "status_code=429 (op=lt, value=400)"
        assert tr.output == {"status_code": 429}

    def test_success_tool_completed_folds_to_completed_status(self):
        events = [
            Event(
                run_id="r", seq=1, event_type=EventType.RUN_STARTED,
                payload={"intent": "test"}, created_at=1.0,
            ),
            Event(
                run_id="r", seq=2, event_type=EventType.TOOL_CALLED,
                payload={"tool_call_id": "a", "tool_name": "t", "input": {}},
                created_at=2.0,
            ),
            Event(
                run_id="r", seq=3, event_type=EventType.TOOL_COMPLETED,
                payload={
                    "tool_call_id": "a",
                    "tool_name": "t",
                    "output": {"status_code": 200},
                    "duration_ms": 50,
                    "result_type": "success",
                },
                created_at=3.0,
            ),
        ]
        state = fold_events(events)
        tr = state.tool_results[0]
        assert tr.status == ToolResultStatus.COMPLETED
        assert tr.error is None

    def test_old_event_without_result_type_folds_to_completed(self):
        events = [
            Event(
                run_id="r", seq=1, event_type=EventType.RUN_STARTED,
                payload={"intent": "test"}, created_at=1.0,
            ),
            Event(
                run_id="r", seq=2, event_type=EventType.TOOL_CALLED,
                payload={"tool_call_id": "a", "tool_name": "t", "input": {}},
                created_at=2.0,
            ),
            Event(
                run_id="r", seq=3, event_type=EventType.TOOL_COMPLETED,
                payload={
                    "tool_call_id": "a",
                    "tool_name": "t",
                    "output": {"x": 1},
                    "duration_ms": 50,
                },
                created_at=3.0,
            ),
        ]
        state = fold_events(events)
        tr = state.tool_results[0]
        assert tr.status == ToolResultStatus.COMPLETED
        assert tr.error is None


# ── DagExecutor chain integration tests ───────────────────────────


class TestDagExecutorSoftError:
    @pytest.fixture
    def store(self):
        return EventStore(db_path=":memory:")

    @pytest.fixture
    async def init_store(self, store):
        await store.initialize()
        return store

    @pytest.mark.asyncio
    async def test_execute_step_returns_soft_error_when_has_semantic_error(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.http_request import HTTP_REQUEST_DEF
        from harness.core.dag_executor import DagExecutor
        from harness.tools.registry import ToolRegistry
        from harness.models.plan import DagPlan, DagStep

        executor = ToolExecutor(store=store)
        registry = ToolRegistry()
        registry.register(HTTP_REQUEST_DEF, lambda i: {"status_code": 429, "headers": {}, "body": "", "elapsed_ms": 10})

        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="http_request", input={"url": "http://x"})],
        )
        dag = DagExecutor(executor=executor, store=store, registry=registry, max_parallel=1)

        all_results = await dag.execute(run_id="run-dag-se", plan=plan)
        assert len(all_results) == 1
        sr = all_results["s1"]
        assert sr.status == StepStatus.SOFT_ERROR
        assert sr.has_soft_error is True
        assert sr.is_done is True

    @pytest.mark.asyncio
    async def test_execute_step_returns_completed_when_no_semantic_error(self, init_store):
        store = init_store
        from harness.tools.executor import ToolExecutor
        from harness.tools.http_request import HTTP_REQUEST_DEF
        from harness.core.dag_executor import DagExecutor
        from harness.tools.registry import ToolRegistry
        from harness.models.plan import DagPlan, DagStep

        executor = ToolExecutor(store=store)
        registry = ToolRegistry()
        registry.register(HTTP_REQUEST_DEF, lambda i: {"status_code": 200, "headers": {}, "body": '{"ok":1}', "elapsed_ms": 10})

        plan = DagPlan(
            intent="test",
            steps=[DagStep(id="s1", tool="http_request", input={"url": "http://x"})],
        )
        dag = DagExecutor(executor=executor, store=store, registry=registry, max_parallel=1)

        all_results = await dag.execute(run_id="run-dag-ok", plan=plan)
        sr = all_results["s1"]
        assert sr.status == StepStatus.COMPLETED
        assert sr.has_soft_error is False
        assert sr.is_done is True


# ── RunMonitor SOFT_ERROR integration tests ───────────────────────


class TestRunMonitorSoftError:
    @pytest.fixture
    def store(self):
        return EventStore(db_path=":memory:")

    @pytest.fixture
    async def init_store(self, store):
        await store.initialize()
        return store

    @pytest.mark.asyncio
    async def test_soft_error_tool_completed_does_not_reset_consecutive_failures(self, init_store):
        store = init_store
        from harness.monitoring.run_monitor import RunMonitor

        monitor = RunMonitor(store=store)
        monitor.attach()

        await store.append_event(
            "r", EventType.TOOL_CALLED,
            {"tool_call_id": "tc1", "tool_name": "http_request", "input": {"url": "http://x"}},
        )
        await store.append_event(
            "r", EventType.TOOL_COMPLETED,
            {
                "tool_call_id": "tc1",
                "tool_name": "http_request",
                "output": {"status_code": 429},
                "duration_ms": 100,
                "result_type": "soft_error",
                "error": "rate limit",
            },
        )

        assert monitor._consecutive_failures.get("r", 0) > 0

    @pytest.mark.asyncio
    async def test_success_tool_completed_resets_consecutive_failures(self, init_store):
        store = init_store
        from harness.monitoring.run_monitor import RunMonitor

        monitor = RunMonitor(store=store)
        monitor.attach()

        await store.append_event(
            "r", EventType.TOOL_CALLED,
            {"tool_call_id": "tc1", "tool_name": "http_request", "input": {"url": "http://x"}},
        )
        await store.append_event(
            "r", EventType.TOOL_COMPLETED,
            {
                "tool_call_id": "tc1",
                "tool_name": "http_request",
                "output": {"status_code": 429},
                "duration_ms": 100,
                "result_type": "soft_error",
                "error": "rate limit",
            },
        )

        await store.append_event(
            "r", EventType.TOOL_CALLED,
            {"tool_call_id": "tc2", "tool_name": "http_request", "input": {"url": "http://y"}},
        )
        await store.append_event(
            "r", EventType.TOOL_COMPLETED,
            {
                "tool_call_id": "tc2",
                "tool_name": "http_request",
                "output": {"status_code": 200},
                "duration_ms": 50,
                "result_type": "success",
            },
        )

        assert monitor._consecutive_failures.get("r", -1) == 0
