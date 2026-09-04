"""S04 — 步骤输出引用契约（OutputRef 受信化）测试。

覆盖 D-01（受信化引用）与 C-04（per-field ref_allowed）。静态校验在
PlanGuardrail 层完成，非法引用在 Executor 之前被拒绝。
"""

from __future__ import annotations

import pytest

from harness.core.planner import PlanGuardrail, parse_output_refs
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import ToolDefinition
from harness.tools.file_op import FileOpTool
from harness.tools.registry import ToolRegistry


def _echo_def() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="e",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"echo": {"type": "string"}}},
        side_effects=[],
    )


@pytest.fixture
def registry():
    r = ToolRegistry()
    r._register(_echo_def(), lambda x: {"echo": x.get("msg", "")})
    r._register(FileOpTool().to_definition(), lambda x: {"success": True})
    return r


def _plan(*steps: DagStep) -> DagPlan:
    return DagPlan(intent="t", steps=list(steps))


# ── 通过场景 ─────────────────────────────────────────────────────


def test_valid_reference_passes(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(id="s2", tool="echo", input={"msg": "$s1.echo"}, depends_on=["s1"]),
    )
    assert parse_output_refs(plan, registry=registry) == []


def test_whole_output_reference_passes(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(id="s2", tool="echo", input={"msg": "$s1"}, depends_on=["s1"]),
    )
    assert parse_output_refs(plan, registry=registry) == []


def test_inline_reference_to_existing_step_passes(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(id="s2", tool="echo", input={"msg": "prefix $s1.echo suffix"}, depends_on=["s1"]),
    )
    assert parse_output_refs(plan, registry=registry) == []


def test_external_completed_step_reference_passes(registry):
    plan = _plan(DagStep(id="s2", tool="echo", input={"msg": "$s1.echo"}, depends_on=["s1"]))
    assert parse_output_refs(plan, registry=registry, completed_step_ids={"s1"}) == []


# ── 拒绝场景 ─────────────────────────────────────────────────────


def test_unknown_step_rejected(registry):
    plan = _plan(
        DagStep(id="s2", tool="echo", input={"msg": "$s99.result"}, depends_on=[]),
    )
    errors = parse_output_refs(plan, registry=registry)
    assert any("unknown step 's99'" in e for e in errors)


def test_inline_unknown_step_rejected(registry):
    plan = _plan(
        DagStep(id="s2", tool="echo", input={"msg": "prefix $s99.result suffix"}),
    )
    errors = parse_output_refs(plan, registry=registry)
    assert any("unknown step 's99'" in e for e in errors)


def test_missing_field_in_source_schema_rejected(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(id="s2", tool="echo", input={"msg": "$s1.result"}, depends_on=["s1"]),
    )
    errors = parse_output_refs(plan, registry=registry)
    assert any("output_schema" in e for e in errors)


def test_additional_properties_lenient(registry):
    td = ToolDefinition(
        name="flex",
        description="f",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        output_schema={"type": "object", "additionalProperties": True},
        side_effects=[],
    )
    r = ToolRegistry()
    r._register(td, lambda x: {"any": 1})
    r._register(FileOpTool().to_definition(), lambda x: {"success": True})
    plan = _plan(
        DagStep(id="s1", tool="flex", input={"msg": "hello"}),
        DagStep(id="s2", tool="flex", input={"msg": "$s1.anything"}, depends_on=["s1"]),
    )
    assert parse_output_refs(plan, registry=r) == []


# ── C-04: ref_allowed per field ──────────────────────────────────


def test_file_op_content_reference_rejected(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(
            id="s2",
            tool="file_op",
            input={"operation": "write", "path": "x.txt", "content": "$s1.echo"},
            depends_on=["s1"],
        ),
    )
    errors = parse_output_refs(plan, registry=registry)
    assert any("'content' does not allow" in e for e in errors)


def test_file_op_path_reference_rejected(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(
            id="s2",
            tool="file_op",
            input={"operation": "read", "path": "$s1.echo"},
            depends_on=["s1"],
        ),
    )
    errors = parse_output_refs(plan, registry=registry)
    assert any("'path' does not allow" in e for e in errors)


def test_http_request_url_reference_rejected():
    from harness.tools.http_request import HTTP_REQUEST_DEF

    r = ToolRegistry()
    r._register(_echo_def(), lambda x: {"echo": "x"})
    r._register(HTTP_REQUEST_DEF, lambda x: {"status_code": 200})
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(id="s2", tool="http_request", input={"url": "$s1.echo", "method": "GET"}, depends_on=["s1"]),
    )
    errors = parse_output_refs(plan, registry=r)
    assert any("'url' does not allow" in e for e in errors)


def test_nested_ref_inherits_top_field_allowance():
    from harness.tools.http_request import HTTP_REQUEST_DEF

    r = ToolRegistry()
    r._register(_echo_def(), lambda x: {"echo": "x"})
    r._register(HTTP_REQUEST_DEF, lambda x: {"status_code": 200})
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(
            id="s2",
            tool="http_request",
            input={"url": "http://a", "method": "POST", "body": {"uuid": "$s1.echo"}},
            depends_on=["s1"],
        ),
    )
    # body is ref_allowed=True → nested uuid reference inherits allowance.
    assert parse_output_refs(plan, registry=r) == []


# ── PlanGuardrail 集成 ───────────────────────────────────────────


def test_guardrail_rejects_unvalidated_reference(registry):
    plan = _plan(
        DagStep(id="s2", tool="echo", input={"msg": "$s99.result"}),
    )
    errors = PlanGuardrail(registry).validate(plan)
    assert any("unknown step 's99'" in e for e in errors)


def test_guardrail_passes_valid_reference(registry):
    plan = _plan(
        DagStep(id="s1", tool="echo", input={"msg": "hello"}),
        DagStep(id="s2", tool="echo", input={"msg": "$s1.echo"}, depends_on=["s1"]),
    )
    assert PlanGuardrail(registry).validate(plan) == []


def test_money_literal_not_treated_as_step_ref(registry):
    plan = _plan(DagStep(id="s1", tool="echo", input={"msg": "Price is $100 and $2.50"}))
    assert parse_output_refs(plan, registry=registry) == []
