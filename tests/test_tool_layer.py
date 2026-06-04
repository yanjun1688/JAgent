"""Unit and integration tests for Tool Layer (L2)."""

import asyncio

import pytest

from harness import (
    ConfirmationReceivedPayload,
    EventType,
    ExecutionStatus,
    Guardrail,
    GuardrailRunner,
    IdempotencyKeyGenerator,
    RetryPolicy,
    RetryRunner,
    Sandbox,
    SchemaGuardrail,
    SideEffect,
    ToolDefinition,
    ToolExecutor,
)

# ── Shared fixtures ──────────────────────────────────────────────


@pytest.fixture
def http_tool_def():
    return ToolDefinition(
        name="http_request",
        description="Make an HTTP request",
        idempotency_key_fields=["url", "method"],
        side_effects=[SideEffect.EXTERNAL],
        timeout_ms=5000,
        retry_policy=RetryPolicy(max_retries=2, retryable_errors=["timeout", "ConnectionError"]),
    )


@pytest.fixture
def read_only_tool_def():
    return ToolDefinition(
        name="read_file",
        description="Read a file",
        idempotency_key_fields=["path"],
        side_effects=[],
    )


@pytest.fixture
def dangerous_tool_def():
    return ToolDefinition(
        name="delete_file",
        description="Delete a file",
        idempotency_key_fields=["path"],
        side_effects=[SideEffect.DELETE],
        requires_confirmation=True,
    )


# ── 2.2 IdempotencyKeyGenerator ─────────────────────────────────


class TestIdempotencyKeyGenerator:
    def test_same_input_same_key(self, http_tool_def):
        k1 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        k2 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        assert k1 == k2

    def test_different_input_different_key(self, http_tool_def):
        k1 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        k2 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://b.com", "method": "POST"})
        assert k1 != k2

    def test_extra_fields_do_not_affect_key(self, http_tool_def):
        k1 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        k2 = IdempotencyKeyGenerator.compute(
            http_tool_def, {"url": "https://a.com", "method": "GET", "headers": {"X": "1"}}
        )
        assert k1 == k2

    def test_key_field_order_does_not_matter(self, http_tool_def):
        k1 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        k2 = IdempotencyKeyGenerator.compute(http_tool_def, {"method": "GET", "url": "https://a.com"})
        assert k1 == k2

    def test_empty_key_fields_produces_consistent_key(self, read_only_tool_def):
        read_only_tool_def.idempotency_key_fields = []
        k1 = IdempotencyKeyGenerator.compute(read_only_tool_def, {"path": "/a"})
        k2 = IdempotencyKeyGenerator.compute(read_only_tool_def, {"path": "/b"})
        assert k1 == k2

    def test_partial_key_fields_in_input(self, http_tool_def):
        k = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com"})
        assert isinstance(k, str)
        assert len(k) == 64  # SHA256 hex

    def test_key_is_stable_across_runs(self, http_tool_def):
        k1 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        k2 = IdempotencyKeyGenerator.compute(http_tool_def, {"url": "https://a.com", "method": "GET"})
        assert k1 == k2
        assert len(k1) == 64


# ── 2.3 SchemaGuardrail ─────────────────────────────────────────


class TestSchemaGuardrail:
    def test_empty_schema_passes_anything(self):
        td = ToolDefinition(name="test", description="t", idempotency_key_fields=["x"], side_effects=[])
        result = SchemaGuardrail.check(td, {"anything": 1})
        assert result.passed

    def test_required_field_missing(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
        )
        result = SchemaGuardrail.check(td, {})
        assert not result.passed
        assert "'x' is a required property" in result.reason

    def test_type_mismatch(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
            idempotency_key_fields=["x"],
            side_effects=[],
        )
        result = SchemaGuardrail.check(td, {"x": "not-a-number"})
        assert not result.passed

    def test_valid_input_passes(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
        )
        result = SchemaGuardrail.check(td, {"x": 42})
        assert result.passed


# ── 2.3 GuardrailRunner ─────────────────────────────────────────


class FakeGuardrail:
    @staticmethod
    def check(tool_def, input, config):
        from harness.tools.guardrails import GuardrailResult

        if config.get("fail", False):
            return GuardrailResult(passed=False, guardrail_id="fake", reason=config.get("reason", "fail"))
        return GuardrailResult(passed=True, guardrail_id="fake", reason="")


class TestGuardrailRunner:
    @pytest.mark.asyncio
    async def test_schema_runs_first(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
        )
        runner = GuardrailRunner({"fake": FakeGuardrail})
        results = await runner.run(td, {})
        assert len(results) == 1
        assert not results[0].passed
        assert results[0].guardrail_id == "schema"

    @pytest.mark.asyncio
    async def test_custom_guardrail_passes(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
            guardrails=[Guardrail(guardrail_type="fake", config={"fail": False})],
        )
        runner = GuardrailRunner({"fake": FakeGuardrail})
        results = await runner.run(td, {"x": "hello"})
        assert len(results) == 2
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_custom_guardrail_blocks(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
            guardrails=[Guardrail(guardrail_type="fake", config={"fail": True, "reason": "blocked"})],
        )
        runner = GuardrailRunner({"fake": FakeGuardrail})
        results = await runner.run(td, {"x": "hello"})
        assert len(results) == 2
        assert results[0].passed  # schema ok
        assert not results[1].passed  # custom blocked

    @pytest.mark.asyncio
    async def test_unknown_guardrail_type_blocks(self):
        td = ToolDefinition(
            name="test",
            description="t",
            idempotency_key_fields=["x"],
            side_effects=[],
            guardrails=[Guardrail(guardrail_type="nonexistent")],
        )
        runner = GuardrailRunner()
        results = await runner.run(td, {"x": "hello"})
        assert len(results) == 2
        assert results[0].passed
        assert not results[1].passed
        assert "Unknown guardrail type" in results[1].reason

    @pytest.mark.asyncio
    async def test_short_circuit_on_schema_failure(self):
        td = ToolDefinition(
            name="test",
            description="t",
            input_schema={"type": "object", "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
            guardrails=[Guardrail(guardrail_type="fake", config={"fail": False})],
        )
        runner = GuardrailRunner({"fake": FakeGuardrail})
        results = await runner.run(td, {})
        assert len(results) == 1
        assert results[0].guardrail_id == "schema"


# ── 2.5 Sandbox ──────────────────────────────────────────────────


class TestSandbox:
    @pytest.mark.asyncio
    async def test_basic_execution(self):
        result = await Sandbox.run(["python", "-c", "print('hello')"])
        assert result.exit_code == 0
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_stderr_capture(self):
        import sys

        result = await Sandbox.run([sys.executable, "-c", "import sys; print('err', file=sys.stderr)"])
        assert "err" in result.stderr

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self):
        import sys

        result = await Sandbox.run([sys.executable, "-c", "exit(1)"])
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self):
        import sys

        with pytest.raises(asyncio.TimeoutError):
            await Sandbox.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout_ms=200)


# ── 2.6 RetryRunner ──────────────────────────────────────────────


class TestRetryRunner:
    def test_should_retry_within_limit(self):
        policy = RetryPolicy(max_retries=3, retryable_errors=["timeout"])
        assert RetryRunner.should_retry(1, "timeout occurred", policy) is True
        assert RetryRunner.should_retry(2, "timeout occurred", policy) is True
        assert RetryRunner.should_retry(3, "timeout occurred", policy) is True

    def test_should_retry_exceeds_limit(self):
        policy = RetryPolicy(max_retries=3, retryable_errors=["timeout"])
        assert RetryRunner.should_retry(3, "timeout occurred", policy) is True
        assert RetryRunner.should_retry(4, "timeout occurred", policy) is False

    def test_non_retryable_error_returns_false(self):
        policy = RetryPolicy(max_retries=3, retryable_errors=["timeout"])
        assert RetryRunner.should_retry(1, "permission denied", policy) is False

    def test_empty_retryable_errors_allows_all(self):
        policy = RetryPolicy(max_retries=3)
        assert RetryRunner.should_retry(1, "any error", policy) is True

    def test_backoff_increases_exponentially(self):
        policy = RetryPolicy(backoff_base_ms=1000)
        b1 = RetryRunner._backoff_ms(1, policy)
        b3 = RetryRunner._backoff_ms(3, policy)
        assert b3 > b1 * 1.2

    @pytest.mark.asyncio
    async def test_execute_with_retry_eventually_succeeds(self):
        call_count = [0]

        async def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("timeout error")
            return "ok"

        policy = RetryPolicy(max_retries=4, retryable_errors=["timeout"], backoff_base_ms=10)
        result, retry_count = await RetryRunner.execute_with_retry(flaky, policy=policy)
        assert result == "ok"
        assert retry_count == 2
        assert call_count[0] == 3

    @pytest.mark.asyncio
    async def test_execute_with_retry_non_retryable_raises(self):
        async def bad():
            raise RuntimeError("fatal error")

        policy = RetryPolicy(max_retries=3, retryable_errors=["timeout"], backoff_base_ms=10)
        with pytest.raises(RuntimeError):
            await RetryRunner.execute_with_retry(bad, policy=policy)


# ── 2.4 ToolExecutor ─────────────────────────────────────────────


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_basic_execution_writes_events(self, store, http_tool_def):
        executor = ToolExecutor(store)
        calls = []

        def my_tool(input):
            calls.append(input)
            return {"status": 200}

        result = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, my_tool
        )
        assert result.status == ExecutionStatus.COMPLETED
        assert result.output == {"status": 200}
        assert len(calls) == 1

        events = await store.get_events("run-1")
        event_types = [e.event_type for e in events]
        assert EventType.TOOL_CALLED in event_types
        assert EventType.TOOL_COMPLETED in event_types
        assert EventType.GUARDRAIL_TRIGGERED not in event_types

    @pytest.mark.asyncio
    async def test_idempotency_cache_hit(self, store, http_tool_def):
        executor = ToolExecutor(store)
        calls = []

        def my_tool(input):
            calls.append(input)
            return {"status": 200}

        r1 = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, my_tool
        )
        assert r1.status == ExecutionStatus.COMPLETED
        assert len(calls) == 1

        r2 = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, my_tool
        )
        assert r2.status == ExecutionStatus.IDEMPOTENCY_HIT
        assert r2.cached is True
        assert len(calls) == 1  # Not called again
        assert r2.output == {"status": 200}

    @pytest.mark.asyncio
    async def test_idempotency_only_hits_tool_completed(self, store, http_tool_def):
        """ToolFailed + same key should still allow retry, not return cached."""
        executor = ToolExecutor(store)

        call_count = [0]

        def failing_tool(input):
            call_count[0] += 1
            raise RuntimeError("fatal error (not retryable)")

        r1 = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, failing_tool
        )
        assert r1.status == ExecutionStatus.FAILED
        assert call_count[0] == 1

        def ok_tool(input):
            call_count[0] += 1
            return {"ok": True}

        r2 = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, ok_tool
        )
        assert r2.status == ExecutionStatus.COMPLETED
        assert call_count[0] == 2  # Was retried

    @pytest.mark.asyncio
    async def test_schema_validation_blocks(self, store, http_tool_def):
        http_tool_def.input_schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        executor = ToolExecutor(store)

        result = await executor.execute("run-1", "http_request", {"method": "GET"}, http_tool_def, lambda x: x)
        assert result.status == ExecutionStatus.GUARDRAIL_BLOCKED
        assert result.guardrail_id == "schema"

        events = await store.get_events("run-1")
        event_types = [e.event_type for e in events]
        assert EventType.GUARDRAIL_TRIGGERED in event_types
        assert EventType.TOOL_CALLED not in event_types

    @pytest.mark.asyncio
    async def test_custom_guardrail_blocks(self, store):
        td = ToolDefinition(
            name="risky",
            description="risky",
            idempotency_key_fields=["x"],
            side_effects=[SideEffect.DELETE],
            guardrails=[Guardrail(guardrail_type="fake", config={"fail": True, "reason": "scope violation"})],
        )
        runner = GuardrailRunner({"fake": FakeGuardrail})
        executor = ToolExecutor(store, guardrail_runner=runner)

        result = await executor.execute("run-1", "risky", {"x": 1}, td, lambda x: x)
        assert result.status == ExecutionStatus.GUARDRAIL_BLOCKED
        assert result.guardrail_id == "fake"
        assert result.guardrail_reason == "scope violation"

        events = await store.get_events("run-1")
        assert any(e.event_type == EventType.GUARDRAIL_TRIGGERED for e in events)
        assert not any(e.event_type == EventType.TOOL_CALLED for e in events)

    @pytest.mark.asyncio
    async def test_requires_confirmation_returns_confirmation_needed(self, store, dangerous_tool_def):
        executor = ToolExecutor(store)

        result = await executor.execute(
            "run-1", "delete_file", {"path": "/important.txt"}, dangerous_tool_def, lambda x: None
        )
        assert result.status == ExecutionStatus.CONFIRMATION_NEEDED
        assert result.confirmation_id is not None

        events = await store.get_events("run-1")
        assert any(e.event_type == EventType.CONFIRMATION_REQUESTED for e in events)
        assert not any(e.event_type == EventType.TOOL_CALLED for e in events)

    @pytest.mark.asyncio
    async def test_confirmation_reentry_still_needed_when_pending(self, store, dangerous_tool_def):
        executor = ToolExecutor(store)

        r1 = await executor.execute(
            "run-1", "delete_file", {"path": "/important.txt"}, dangerous_tool_def, lambda x: None
        )
        assert r1.status == ExecutionStatus.CONFIRMATION_NEEDED

        r2 = await executor.execute(
            "run-1", "delete_file", {"path": "/important.txt"}, dangerous_tool_def, lambda x: None
        )
        assert r2.status == ExecutionStatus.CONFIRMATION_NEEDED

    @pytest.mark.asyncio
    async def test_confirmation_reentry_skips_when_confirmed(self, store, dangerous_tool_def):
        executor = ToolExecutor(store)

        r1 = await executor.execute(
            "run-1", "delete_file", {"path": "/important.txt"}, dangerous_tool_def, lambda x: None
        )
        assert r1.status == ExecutionStatus.CONFIRMATION_NEEDED
        assert r1.confirmation_id is not None

        await store.append_event(
            "run-1",
            EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=r1.confirmation_id, confirmed=True, operator_id="op-1"
            ).model_dump(),
        )

        call_count = []

        r2 = await executor.execute(
            "run-1",
            "delete_file",
            {"path": "/important.txt"},
            dangerous_tool_def,
            lambda x: (call_count.append(1), "done")[1],
        )
        assert r2.status == ExecutionStatus.COMPLETED
        assert len(call_count) == 1

    @pytest.mark.asyncio
    async def test_confirmation_denied_returns_failure(self, store, dangerous_tool_def):
        executor = ToolExecutor(store)

        r1 = await executor.execute(
            "run-1", "delete_file", {"path": "/important.txt"}, dangerous_tool_def, lambda x: None
        )
        await store.append_event(
            "run-1",
            EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=r1.confirmation_id, confirmed=False, operator_id="op-1"
            ).model_dump(),
        )

        r2 = await executor.execute(
            "run-1", "delete_file", {"path": "/important.txt"}, dangerous_tool_def, lambda x: None
        )
        assert r2.status == ExecutionStatus.FAILED
        assert "denied" in (r2.error or "").lower()

    @pytest.mark.asyncio
    async def test_tool_exception_writes_failed_event(self, store, http_tool_def):
        executor = ToolExecutor(store)

        def bad_tool(input):
            raise ValueError("something went wrong")

        result = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, bad_tool
        )
        assert result.status == ExecutionStatus.FAILED
        assert result.error == "something went wrong"

        events = await store.get_events("run-1")
        assert any(e.event_type == EventType.TOOL_CALLED for e in events)
        assert any(e.event_type == EventType.TOOL_FAILED for e in events)

    @pytest.mark.asyncio
    async def test_async_tool_fn_supported(self, store, http_tool_def):
        executor = ToolExecutor(store)

        async def async_tool(input):
            await asyncio.sleep(0)
            return {"status": 200}

        result = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, async_tool
        )
        assert result.status == ExecutionStatus.COMPLETED
        assert result.output == {"status": 200}

    @pytest.mark.asyncio
    async def test_cross_run_idempotency_independent(self, store, http_tool_def):
        executor = ToolExecutor(store)

        def my_tool(input):
            return {"run": input.get("run", "?")}

        r1 = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET", "run": "A"}, http_tool_def, my_tool
        )
        assert r1.status == ExecutionStatus.COMPLETED

        r2 = await executor.execute(
            "run-2", "http_request", {"url": "https://a.com", "method": "GET", "run": "B"}, http_tool_def, my_tool
        )
        assert r2.status == ExecutionStatus.COMPLETED
        assert r2.output == {"run": "B"}  # Different run, not cached


class TestToolCompletedDuration:
    @pytest.mark.asyncio
    async def test_duration_ms_positive_on_completion(self, store, http_tool_def):
        executor = ToolExecutor(store)

        async def slow_tool(input):
            await asyncio.sleep(0.05)
            return {"ok": True}

        result = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, slow_tool
        )
        assert result.status == ExecutionStatus.COMPLETED
        assert result.duration_ms >= 40  # 50ms sleep, allow some variance

    @pytest.mark.asyncio
    async def test_duration_ms_positive_on_timeout(self, store, http_tool_def):
        http_tool_def.timeout_ms = 500
        executor = ToolExecutor(store)

        async def slow_tool(input):
            await asyncio.sleep(10)
            return {"ok": True}

        result = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, slow_tool
        )
        assert result.status == ExecutionStatus.TIMEOUT
        assert result.duration_ms >= 400  # timeout = 500ms, duration close to it

    @pytest.mark.asyncio
    async def test_duration_ms_positive_on_failure(self, store, http_tool_def):
        executor = ToolExecutor(store)

        def bad_tool(input):
            raise ValueError("fail")

        result = await executor.execute(
            "run-1", "http_request", {"url": "https://a.com", "method": "GET"}, http_tool_def, bad_tool
        )
        assert result.status == ExecutionStatus.FAILED
        assert result.duration_ms >= 0  # failure can be fast


class TestConfirmationRequestedPayload:
    def test_payload_roundtrip(self):
        from harness import ConfirmationRequestedPayload

        p = ConfirmationRequestedPayload(
            confirmation_id="cf-1",
            tool_call_id="tc-1",
            tool_name="file_op",
            input={"path": "/etc"},
            idempotency_key="ik-1",
            risk_level="high",
        )
        assert p.confirmation_id == "cf-1"
        assert p.tool_call_id == "tc-1"
        assert p.idempotency_key == "ik-1"

    def test_payload_serialization_roundtrip(self):
        from harness import ConfirmationRequestedPayload

        p = ConfirmationRequestedPayload(
            confirmation_id="cf-2",
            tool_call_id="tc-2",
            tool_name="delete",
            input={"path": "/x"},
            idempotency_key="ik-2",
        )
        d = p.model_dump()
        p2 = ConfirmationRequestedPayload(**d)
        assert p2.confirmation_id == p.confirmation_id
        assert p2.tool_call_id == p.tool_call_id
        assert p2.idempotency_key == p.idempotency_key
        assert p2.risk_level == "medium"
