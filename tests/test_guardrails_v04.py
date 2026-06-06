"""Unit and integration tests for V0.4 Guardrails — Scope, RateLimit, DestructiveOp, Dependency."""

import pytest

from harness import (
    ConfirmationReceivedPayload,
    EventType,
    ExecutionStatus,
    Guardrail,
    GuardrailRunner,
    SideEffect,
    ToolDefinition,
    ToolExecutor,
)
from harness.models.tools import DependencyConstraint
from harness.tools.guardrails import (
    DependencyGuardrail,
    DestructiveOpGuardrail,
    GuardrailResult,
    RateLimitGuardrail,
    ScopeGuardrail,
)


# ── Helpers ────────────────────────────────────────────────────────


def _make_tool(name="test", input_schema=None, guardrails=None, idempotency_key_fields=None, side_effects=None, **kw):
    return ToolDefinition(
        name=name,
        description=name,
        input_schema=input_schema or {},
        idempotency_key_fields=idempotency_key_fields or ["x"],
        side_effects=side_effects or [],
        guardrails=guardrails,
        **kw,
    )


# ── 7.1 ScopeGuardrail ────────────────────────────────────────────


class TestScopeGuardrail:
    def test_blocks_outside_directory(self):
        td = _make_tool(name="file_op")
        config = {"allowed_directories": ["/home/user/sandbox"]}
        result = ScopeGuardrail.check(td, {"operation": "read", "path": "/etc/passwd"}, config)
        assert not result.passed
        assert result.reason.startswith("Path")

    def test_allows_inside_directory(self):
        td = _make_tool(name="file_op")
        config = {"allowed_directories": ["/home/user/sandbox"]}
        result = ScopeGuardrail.check(td, {"operation": "read", "path": "/home/user/sandbox/file.txt"}, config)
        assert result.passed

    def test_blocks_outside_domain(self):
        td = _make_tool(name="http_request")
        config = {"allowed_domains": ["example.com"]}
        result = ScopeGuardrail.check(td, {"url": "https://malicious.com/data"}, config)
        assert not result.passed
        assert "not in allowed list" in result.reason

    def test_allows_inside_domain(self):
        td = _make_tool(name="http_request")
        config = {"allowed_domains": ["example.com"]}
        result = ScopeGuardrail.check(td, {"url": "https://sub.example.com/page"}, config)
        assert result.passed

    def test_empty_config_allows_all(self):
        td = _make_tool(name="file_op")
        result = ScopeGuardrail.check(td, {"operation": "delete", "path": "/any/path"}, {})
        assert result.passed

    def test_allows_browser_to_allowed_domain(self):
        td = _make_tool(name="browser")
        config = {"allowed_domains": ["trusted.org"]}
        result = ScopeGuardrail.check(td, {"action": "navigate", "url": "https://trusted.org/page"}, config)
        assert result.passed

    def test_unknown_tool_passes(self):
        td = _make_tool(name="unknown_tool")
        config = {"allowed_directories": ["/x"]}
        result = ScopeGuardrail.check(td, {"x": 1}, config)
        assert result.passed


# ── 7.2 RateLimitGuardrail ────────────────────────────────────────


class TestRateLimitGuardrail:
    def setup_method(self):
        RateLimitGuardrail.reset()

    def test_first_call_passes(self):
        td = _make_tool(name="http_request")
        config = {"max_calls": 3, "window_seconds": 60, "scope": "tool"}
        result = RateLimitGuardrail.check(td, {}, config)
        assert result.passed

    def test_exceed_limit_blocks(self):
        td = _make_tool(name="http_request")
        config = {"max_calls": 2, "window_seconds": 60, "scope": "tool"}
        assert RateLimitGuardrail.check(td, {}, config).passed
        assert RateLimitGuardrail.check(td, {}, config).passed
        result = RateLimitGuardrail.check(td, {}, config)
        assert not result.passed
        assert "Rate limit exceeded" in result.reason

    def test_different_tools_have_independent_limits(self):
        td_a = _make_tool(name="tool_a")
        td_b = _make_tool(name="tool_b")
        config = {"max_calls": 1, "window_seconds": 60, "scope": "tool"}
        assert RateLimitGuardrail.check(td_a, {}, config).passed
        assert RateLimitGuardrail.check(td_a, {}, config).passed is False
        result = RateLimitGuardrail.check(td_b, {}, config)
        assert result.passed  # different tool, different counter

    def test_limit_one(self):
        td = _make_tool(name="one_shot")
        config = {"max_calls": 1, "window_seconds": 60, "scope": "tool"}
        assert RateLimitGuardrail.check(td, {}, config).passed
        assert RateLimitGuardrail.check(td, {}, config).passed is False

    def test_reset_clears_history(self):
        td = _make_tool(name="test_reset")
        config = {"max_calls": 1, "window_seconds": 60, "scope": "tool"}
        assert RateLimitGuardrail.check(td, {}, config).passed
        assert RateLimitGuardrail.check(td, {}, config).passed is False
        RateLimitGuardrail.reset()
        assert RateLimitGuardrail.check(td, {}, config).passed


# ── 7.3 DestructiveOpGuardrail ────────────────────────────────────


class TestDestructiveOpGuardrail:
    def test_file_op_delete_triggers_confirmation(self):
        td = _make_tool(name="file_op")
        result = DestructiveOpGuardrail.check(td, {"operation": "delete", "path": "/important.txt"}, {})
        assert result.passed
        assert result.triggers_confirmation

    def test_file_op_read_does_not_trigger(self):
        td = _make_tool(name="file_op")
        result = DestructiveOpGuardrail.check(td, {"operation": "read", "path": "/file.txt"}, {})
        assert result.passed
        assert not result.triggers_confirmation

    def test_run_code_triggers_confirmation(self):
        td = _make_tool(name="run_code")
        result = DestructiveOpGuardrail.check(td, {"command": "rm -rf /"}, {})
        assert result.passed
        assert result.triggers_confirmation

    def test_http_request_does_not_trigger(self):
        td = _make_tool(name="http_request")
        result = DestructiveOpGuardrail.check(td, {"url": "https://example.com"}, {})
        assert result.passed
        assert not result.triggers_confirmation

    def test_custom_destructive_operations(self):
        td = _make_tool(name="file_op")
        config = {"destructive_operations": ["write", "delete"]}
        result = DestructiveOpGuardrail.check(td, {"operation": "write", "path": "/out.txt"}, config)
        assert result.passed
        assert result.triggers_confirmation

    def test_file_op_list_no_trigger(self):
        td = _make_tool(name="file_op")
        result = DestructiveOpGuardrail.check(td, {"operation": "list", "path": "/dir"}, {})
        assert result.passed
        assert not result.triggers_confirmation


# ── 7.4 DependencyGuardrail ────────────────────────────────────────


class TestDependencyGuardrail:
    @pytest.mark.asyncio
    async def test_missing_required_events_blocks(self, store):
        await store.append_event("run-1", EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}})
        g = DependencyGuardrail(store=store)
        td = _make_tool(name="some_tool")
        config = {"required_events": ["RunStarted", "AgentThought"]}
        result = await g.check(td, {}, config, run_id="run-1")
        assert not result.passed
        assert "AgentThought" in result.reason

    @pytest.mark.asyncio
    async def test_all_required_events_exist_passes(self, store):
        await store.append_event("run-1", EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}})
        await store.append_event("run-1", EventType.AGENT_THOUGHT, {"thought": "ok", "token_count": 0})
        g = DependencyGuardrail(store=store)
        td = _make_tool(name="some_tool")
        config = {"required_events": ["RunStarted", "AgentThought"]}
        result = await g.check(td, {}, config, run_id="run-1")
        assert result.passed

    @pytest.mark.asyncio
    async def test_no_store_available_skips_check(self):
        g = DependencyGuardrail(store=None)
        td = _make_tool(name="some_tool")
        result = await g.check(td, {}, {"required_events": ["RunStarted"]}, run_id="run-1")
        assert result.passed

    @pytest.mark.asyncio
    async def test_no_required_events_always_passes(self, store):
        g = DependencyGuardrail(store=store)
        td = _make_tool(name="some_tool")
        result = await g.check(td, {}, {}, run_id="run-1")
        assert result.passed

    @pytest.mark.asyncio
    async def test_no_run_id_skips(self, store):
        g = DependencyGuardrail(store=store)
        td = _make_tool(name="some_tool")
        result = await g.check(td, {}, {"required_events": ["RunStarted"]})
        assert result.passed

    # ── depends_on path (V2.1+) ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_depends_on_missing_event_blocks(self, store):
        await store.append_event("run-1", EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}})
        g = DependencyGuardrail(store=store)
        td = _make_tool(
            name="plan_tool",
            depends_on=[DependencyConstraint(event_type="OrchestrationStarted")],
        )
        result = await g.check(td, {}, {}, run_id="run-1")
        assert not result.passed
        assert "OrchestrationStarted" in result.reason

    @pytest.mark.asyncio
    async def test_depends_on_all_exist_passes(self, store):
        await store.append_event("run-1", EventType.RUN_STARTED, {"intent": "test", "context_snapshot": {}})
        g = DependencyGuardrail(store=store)
        td = _make_tool(
            name="plan_tool",
            depends_on=[DependencyConstraint(event_type="RunStarted")],
        )
        result = await g.check(td, {}, {}, run_id="run-1")
        assert result.passed

    @pytest.mark.asyncio
    async def test_depends_on_payload_filter(self, store):
        await store.append_event("run-1", EventType.STEP_COMPLETED, {"plan_id": "p-1", "step_index": 0, "tool_call_id": "tc-1", "output": {}})
        g = DependencyGuardrail(store=store)
        td = _make_tool(
            name="step_tool",
            depends_on=[
                DependencyConstraint(
                    event_type="StepCompleted",
                    payload_filter={"step_index": 0},
                    message="Step 0 must be completed first",
                ),
            ],
        )
        result = await g.check(td, {}, {}, run_id="run-1")
        assert result.passed

    @pytest.mark.asyncio
    async def test_depends_on_payload_filter_mismatch_blocks(self, store):
        await store.append_event("run-1", EventType.STEP_COMPLETED, {"plan_id": "p-1", "step_index": 0, "tool_call_id": "tc-1", "output": {}})
        g = DependencyGuardrail(store=store)
        td = _make_tool(
            name="step_tool",
            depends_on=[
                DependencyConstraint(
                    event_type="StepCompleted",
                    payload_filter={"step_index": 1},
                    message="Step 1 must be completed first",
                ),
            ],
        )
        result = await g.check(td, {}, {}, run_id="run-1")
        assert not result.passed
        assert "Step 1" in result.reason

    @pytest.mark.asyncio
    async def test_depends_on_takes_precedence_over_required_events(self, store):
        g = DependencyGuardrail(store=store)
        td = _make_tool(
            name="prefer_depends_on",
            depends_on=[DependencyConstraint(event_type="RunStarted")],
        )
        # Even though required_events says StepCompleted, depends_on takes precedence
        result = await g.check(td, {}, {"required_events": ["StepCompleted"]}, run_id="run-1")
        assert not result.passed
        assert "RunStarted" in result.reason
        # Verify required_events was NOT consulted (StepCompleted should not be in reason)
        assert "StepCompleted" not in result.reason


# ── GuardrailRunner async + mixed guardrails ──────────────────────


class FakeSyncGuardrail:
    @staticmethod
    def check(tool_def, input, config):
        fail = config.get("fail", False)
        return GuardrailResult(passed=not fail, guardrail_id="sync", reason="sync fail" if fail else "", triggers_confirmation=config.get("trigger_confirm", False))


class TestGuardrailRunnerV04:
    @pytest.mark.asyncio
    async def test_async_guardrail_runner_works(self):
        td = _make_tool(
            guardrails=[Guardrail(guardrail_type="sync", config={"fail": False})],
        )
        runner = GuardrailRunner({"sync": FakeSyncGuardrail})
        results = await runner.run(td, {"x": 1})
        assert len(results) == 2
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_async_guardrail_with_run_id(self, store):
        await store.append_event("run-1", EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}})
        td = _make_tool(
            name="dep_test",
            guardrails=[Guardrail(guardrail_type="dependency", config={"required_events": ["RunStarted"]})],
        )
        runner = GuardrailRunner({"dependency": DependencyGuardrail}, store=store)
        results = await runner.run(td, {"x": 1}, run_id="run-1")
        assert len(results) == 2
        assert all(r.passed for r in results)


# ── 7.3 Executor integration: DestructiveOpGuardrail triggers confirmation ──


class TestExecutorDestructiveOpTrigger:
    @pytest.mark.asyncio
    async def test_destructive_op_guardrail_triggers_confirmation_flow(self, store):
        td = _make_tool(
            name="file_op",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["operation", "path"],
            },
            idempotency_key_fields=["operation", "path"],
            side_effects=[SideEffect.DELETE],
            requires_confirmation=False,
            guardrails=[Guardrail(guardrail_type="destructive", config={})],
        )
        runner = GuardrailRunner({"destructive": DestructiveOpGuardrail})
        executor = ToolExecutor(store, guardrail_runner=runner)

        def fake_tool(input):
            return {"success": True}

        result = await executor.execute(
            "run-1", "file_op", {"operation": "delete", "path": "/important.txt"}, td, fake_tool,
        )
        assert result.status == ExecutionStatus.CONFIRMATION_NEEDED
        assert result.confirmation_id is not None

        events = await store.get_events("run-1")
        assert any(e.event_type == EventType.CONFIRMATION_REQUESTED for e in events)
        assert not any(e.event_type == EventType.TOOL_CALLED for e in events)

    @pytest.mark.asyncio
    async def test_destructive_op_guardrail_confirmed_then_executes(self, store):
        td = _make_tool(
            name="file_op",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["operation", "path"],
            },
            idempotency_key_fields=["operation", "path"],
            side_effects=[SideEffect.DELETE],
            requires_confirmation=False,
            guardrails=[Guardrail(guardrail_type="destructive", config={})],
        )
        runner = GuardrailRunner({"destructive": DestructiveOpGuardrail})
        executor = ToolExecutor(store, guardrail_runner=runner)

        r1 = await executor.execute(
            "run-1", "file_op", {"operation": "delete", "path": "/important.txt"}, td, lambda x: {"ok": True},
        )
        assert r1.status == ExecutionStatus.CONFIRMATION_NEEDED

        await store.append_event(
            "run-1",
            EventType.CONFIRMATION_RECEIVED,
            ConfirmationReceivedPayload(
                confirmation_id=r1.confirmation_id, confirmed=True, operator_id="op-1"
            ).model_dump(),
        )

        call_count = []
        r2 = await executor.execute(
            "run-1", "file_op", {"operation": "delete", "path": "/important.txt"}, td,
            lambda x: (call_count.append(1), {"ok": True})[1],
        )
        assert r2.status == ExecutionStatus.COMPLETED
        assert len(call_count) == 1

    @pytest.mark.asyncio
    async def test_nondestructive_op_passes_through(self, store):
        td = _make_tool(
            name="file_op",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["operation", "path"],
            },
            idempotency_key_fields=["operation", "path"],
            side_effects=[],
            requires_confirmation=False,
            guardrails=[Guardrail(guardrail_type="destructive", config={})],
        )
        runner = GuardrailRunner({"destructive": DestructiveOpGuardrail})
        executor = ToolExecutor(store, guardrail_runner=runner)

        result = await executor.execute(
            "run-1", "file_op", {"operation": "read", "path": "/safe.txt"}, td, lambda x: {"content": "data"},
        )
        assert result.status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_scope_guardrail_blocks_outside_dir(self, store):
        td = _make_tool(
            name="file_op",
            input_schema={
                "type": "object",
                "properties": {
                    "operation": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["operation", "path"],
            },
            idempotency_key_fields=["operation", "path"],
            side_effects=[],
            guardrails=[Guardrail(guardrail_type="scope", config={"allowed_directories": ["/allowed"]})],
        )
        runner = GuardrailRunner({"scope": ScopeGuardrail})
        executor = ToolExecutor(store, guardrail_runner=runner)

        result = await executor.execute(
            "run-1", "file_op", {"operation": "read", "path": "/etc/passwd"}, td, lambda x: {"ok": True},
        )
        assert result.status == ExecutionStatus.GUARDRAIL_BLOCKED
        assert result.guardrail_id == "scope"

    @pytest.mark.asyncio
    async def test_runtime_registration_of_guardrail(self):
        runner = GuardrailRunner()
        runner.register("scope", ScopeGuardrail)
        td = _make_tool(
            name="file_op",
            guardrails=[Guardrail(guardrail_type="scope", config={"allowed_directories": ["/ok"]})],
        )
        results = await runner.run(td, {"operation": "read", "path": "/bad/file"}, run_id="r1")
        assert results[1].passed is False

    @pytest.mark.asyncio
    async def test_multiple_guardrails_run_in_order(self, store):
        RateLimitGuardrail.reset()
        td = _make_tool(
            name="multi_check",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[SideEffect.WRITE],
            guardrails=[
                Guardrail(guardrail_type="scope", config={"allowed_directories": ["/ok"]}),
                Guardrail(guardrail_type="rate_limit", config={"max_calls": 10, "window_seconds": 60, "scope": "tool"}),
            ],
        )
        runner = GuardrailRunner({"scope": ScopeGuardrail, "rate_limit": RateLimitGuardrail})
        executor = ToolExecutor(store, guardrail_runner=runner)

        result = await executor.execute(
            "run-1", "multi_check", {"x": 1}, td, lambda x: {"ok": True},
        )
        assert result.status == ExecutionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_dependency_guardrail_in_executor_rejects(self, store):
        td = _make_tool(
            name="dep_tool",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            idempotency_key_fields=["x"],
            side_effects=[],
            guardrails=[Guardrail(guardrail_type="dep", config={"required_events": ["AgentThought"]})],
        )
        runner = GuardrailRunner({"dep": DependencyGuardrail}, store=store)
        executor = ToolExecutor(store, guardrail_runner=runner)

        result = await executor.execute(
            "run-1", "dep_tool", {"x": 1}, td, lambda x: {"ok": True},
        )
        assert result.status == ExecutionStatus.GUARDRAIL_BLOCKED
        assert "AgentThought" in (result.guardrail_reason or "")
