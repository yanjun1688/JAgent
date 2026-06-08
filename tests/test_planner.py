from __future__ import annotations

import pytest

from harness.core.planner import Planner, PlanGuardrail
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import RetryPolicy, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.registry import ToolRegistry


@pytest.fixture
def store():
    return EventStore(":memory:")


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(
        ToolDefinition(
            name="echo", description="Echo",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            output_schema={"type": "object"},
            idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(),
        ),
        lambda x: {"ok": True},
    )
    return r


class TestPlannerParsePlan:
    def test_parse_plan_parameters_fallback(self):
        """_parse_plan should accept 'parameters' as alias for 'input'."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "parameters": {"msg": "hello"}, "depends_on": []}]}'
        plan = Planner._parse_plan(response)
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].input == {"msg": "hello"}

    def test_parse_plan_input_takes_priority(self):
        """When both 'input' and 'parameters' exist, 'input' wins."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "input": {"msg": "from_input"}, "parameters": {"msg": "from_params"}}]}'
        plan = Planner._parse_plan(response)
        assert plan is not None
        assert plan.steps[0].input == {"msg": "from_input"}

    def test_parse_plan_missing_input_returns_none(self):
        """When neither 'input' nor 'parameters' exists, return None."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "depends_on": []}]}'
        plan = Planner._parse_plan(response)
        assert plan is None

    def test_parse_plan_non_dict_input_returns_none(self):
        """When input is not a dict (e.g. string), return None."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "input": "not_a_dict"}]}'
        plan = Planner._parse_plan(response)
        assert plan is None

    def test_parse_plan_parameters_non_dict_returns_none(self):
        response = '{"steps": [{"id": "s1", "tool": "echo", "parameters": "bad"}]}'
        plan = Planner._parse_plan(response)
        assert plan is None

    def test_parse_plan_empty_steps(self):
        response = '{"steps": []}'
        plan = Planner._parse_plan(response)
        assert plan is not None
        assert len(plan.steps) == 0

    def test_parse_plan_malformed_json(self):
        plan = Planner._parse_plan("not json at all")
        assert plan is None

    def test_parse_plan_code_fences(self):
        response = '```\n{"steps": [{"id": "s1", "tool": "echo", "input": {"msg": "hi"}}]}\n```'
        plan = Planner._parse_plan(response)
        assert plan is not None
        assert len(plan.steps) == 1


class TestPlanGuardrail:
    def test_validate_valid_plan(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[DagStep(id="s1", tool="echo", input={"msg": "hi"})])
        errors = guardrail.validate(plan)
        assert errors == []

    def test_validate_unknown_tool(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[DagStep(id="s1", tool="ghost_tool", input={})])
        errors = guardrail.validate(plan)
        assert any("unknown tool" in e for e in errors)

    def test_validate_duplicate_step_id(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}),
            DagStep(id="s1", tool="echo", input={}),
        ])
        errors = guardrail.validate(plan)
        assert any("Duplicate" in e for e in errors)

    def test_validate_cyclic_dependency(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}, depends_on=["s2"]),
            DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
        ])
        errors = guardrail.validate(plan)
        assert any("Cycle" in e for e in errors)

    def test_validate_depends_on_unknown(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}, depends_on=["ghost"]),
        ])
        errors = guardrail.validate(plan)
        assert any("depends on unknown" in e for e in errors)

    def test_validate_empty_plan_passes(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[])
        errors = guardrail.validate(plan)
        assert errors == []

    def test_validate_max_parallel_is_warning_now(self, registry):
        """_check_max_parallel should no longer produce hard errors."""
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}),
            DagStep(id="s2", tool="echo", input={}),
            DagStep(id="s3", tool="echo", input={}),
            DagStep(id="s4", tool="echo", input={}),  # 4 echoes in same layer, max_parallel=3
        ])
        errors = guardrail.validate(plan)
        # Should pass without errors (semaphore handles enforcement)
        assert errors == []
