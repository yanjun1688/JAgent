"""Planner package (V0.7, L4) — DAG plan generation/revision via LLM.

Public API (import paths unchanged from the former ``planner.py`` module):

- :class:`Planner` — non-trusted LLM orchestration (plan/revise/repair/answer)
- :class:`PlanGuardrail` — trusted plan validation before execution
- :func:`parse_output_refs` — trusted static ``$step`` reference validation
- :func:`validate_revision_invariants` / :func:`revision_invariant_feedback`
  — trusted revision invariant enforcement (S08 / ADR-009)
- :func:`parse_plan_response` — LLM response parsing (used directly by tests)
"""

from harness.core.planner.agent import Planner
from harness.core.planner.guardrail import PlanGuardrail
from harness.core.planner.output_refs import parse_output_refs
from harness.core.planner.parsing import parse_local_repair_proposal, parse_plan_response
from harness.core.planner.revision_invariants import (
    revision_invariant_feedback,
    validate_revision_invariants,
)

__all__ = [
    "Planner",
    "PlanGuardrail",
    "parse_output_refs",
    "parse_plan_response",
    "parse_local_repair_proposal",
    "validate_revision_invariants",
    "revision_invariant_feedback",
]
