"""S04 (D-01 / C-04): static validation of ``$step`` / ``$step.field`` references.

Trusted, pure functions — no I/O, no LLM. Invalid references are rejected by
PlanGuardrail before the Executor, so a ``$`` string can never reach a tool as
a raw literal path.
"""

from __future__ import annotations

import re
from typing import Any

from harness.models.plan import DagPlan, DagStep, OutputRef
from harness.tools.registry import ToolRegistry

_REF_PURE_PATTERN = re.compile(r"^\$([A-Za-z_][\w-]*)(?:\.([\w.]+))?$")
_REF_INLINE_PATTERN = re.compile(r"\$([A-Za-z_][\w-]*)(?:\.([\w.]+))?")


def _is_plausible_step_id(sid: str) -> bool:
    """All-numeric names (``$100``) are money/literal text, never step ids."""
    return bool(sid) and not sid.isdigit()


def _schema_has_field(output_schema: dict | None, field_path: str) -> bool:
    """Shallow top-level property check (S04 §6): lenient by design.

    Allows unknown fields when the schema declares ``additionalProperties``,
    a ``*`` wildcard property, OR no explicit ``properties`` at all (an
    unconstrained object).  Only schemas that explicitly enumerate
    ``properties`` reject a missing field.
    """
    schema = output_schema or {}
    if schema.get("additionalProperties"):
        return True
    props = schema.get("properties")
    if not props:
        return True
    if "*" in props:
        return True
    if not field_path:
        return True
    return field_path.split(".", 1)[0] in props


def _collect_refs(value: Any, top_field: str | None = None) -> list[tuple[str, OutputRef, bool]]:
    """Recursively collect ``$step`` / ``$step.field`` references from a step input.

    Returns ``(top_field, ref, is_pure)`` triples. ``top_field`` is the
    top-level input field name used for the ``ref_allowed`` (C-04) decision;
    nested references inside a field inherit that field's allowance.
    """
    refs: list[tuple[str, OutputRef, bool]] = []
    if isinstance(value, dict):
        for key, val in value.items():
            refs.extend(_collect_refs(val, key if top_field is None else top_field))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs(item, top_field))
    elif isinstance(value, str):
        pure = _REF_PURE_PATTERN.match(value)
        if pure and _is_plausible_step_id(pure.group(1)):
            refs.append(
                (
                    top_field or "",
                    OutputRef(step_id=pure.group(1), field_path=pure.group(2) or ""),
                    True,
                )
            )
        else:
            for m in _REF_INLINE_PATTERN.finditer(value):
                sid = m.group(1)
                if not _is_plausible_step_id(sid):
                    continue
                refs.append(
                    (top_field or "", OutputRef(step_id=sid, field_path=m.group(2) or ""), False)
                )
    return refs


def parse_output_refs(
    plan: DagPlan,
    *,
    registry: ToolRegistry,
    completed_step_ids: set[str] | None = None,
    available_step_ids: set[str] | None = None,
) -> list[str]:
    """S04: statically validate every ``$step`` reference in a plan.

    Returns a list of errors; empty list = valid.  Rules (D-01 / C-04):
      - referenced step exists in current plan ∪ completed ∪ available;
      - pure references: field exists in the source step's operation
        ``output_schema`` (shallow, lenient) AND the target input field is
        ``ref_allowed=True`` (unlisted fields default to False → reject);
      - inline references: step existence only (field validation lenient);
      - ``file_op.path`` / ``file_op.content`` never allow references.

    Invalid plans are rejected by the trusted PlanGuardrail before the Executor,
    so a ``$`` string can never reach a tool as a raw literal path.
    """
    errors: list[str] = []
    completed = set(completed_step_ids or ())
    available = set(available_step_ids or ())
    step_map = {s.id: s for s in plan.steps}
    valid_steps = set(step_map.keys()) | completed | available

    for step in plan.steps:
        if not isinstance(step.input, dict):
            continue
        target_op = None
        target_def = registry.get_tool_def(step.tool)
        if target_def is not None:
            target_op = target_def.resolve_operation(step.input)

        for top_field, ref, is_pure in _collect_refs(step.input):
            if ref.step_id not in valid_steps:
                errors.append(
                    f"Step '{step.id}': reference '${ref.step_id}' targets unknown step "
                    f"'{ref.step_id}' (not in plan, completed, or available)"
                )
                continue

            # C-04: target field must allow references (unlisted → False).
            if target_op is not None and not target_op.ref_allowed(top_field):
                errors.append(
                    f"Step '{step.id}': input field '{top_field}' does not allow "
                    f"$step.output references (ref_allowed=False)"
                )
                continue

            # Pure reference: source field must exist in the source step's
            # operation output_schema (only for in-plan source steps we know).
            if is_pure and ref.field_path and ref.step_id in step_map:
                src_step: DagStep = step_map[ref.step_id]
                src_def = registry.get_tool_def(src_step.tool)
                if src_def is not None:
                    src_op = src_def.resolve_operation(src_step.input)
                    src_schema = src_op.output_schema if src_op is not None else src_def.output_schema
                    if not _schema_has_field(src_schema, ref.field_path):
                        op_label = f"'{src_op.operation}'" if src_op is not None else f"tool '{src_def.name}'"
                        errors.append(
                            f"Step '{step.id}': reference '${ref.step_id}.{ref.field_path}' — "
                            f"field '{ref.field_path}' not in operation {op_label} output_schema"
                        )

    return errors
