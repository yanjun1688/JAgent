"""Phase-0 guardrails (R10): event-type contract completeness assertions.

These tests make the previously discipline-only invariants fail loudly:

1. Every ``EventType`` member must have exactly one payload model in
   ``PAYLOAD_MODEL_MAP`` (and vice versa). A new event type without a payload
   model previously failed only at write time ("Unknown event type"); a payload
   model without an enum member was silently dead.
2. ``fold_events`` must handle every ``EventType`` explicitly — no silent
   default-drop. Adding an event type without a fold branch fails the test,
   forcing the author to decide whether the event mutates run state or is an
   explicit no-op (audit / out-of-run-scope).

Both are structural (enum/AST) checks; they do not construct payloads.
"""

from __future__ import annotations

import ast
from pathlib import Path

from harness.core import fold as fold_module
from harness.models.events import PAYLOAD_MODEL_MAP, EventType

FOLD_PATH = Path(fold_module.__file__)


def _event_type_members() -> set[str]:
    return {member.name for member in EventType}


def test_payload_model_map_covers_every_event_type_exactly_once():
    map_keys = {key.name for key in PAYLOAD_MODEL_MAP}
    enum_members = _event_type_members()

    missing = enum_members - map_keys
    assert not missing, f"EventType members missing from PAYLOAD_MODEL_MAP: {sorted(missing)}"

    extra = map_keys - enum_members
    assert not extra, f"PAYLOAD_MODEL_MAP has keys that are not EventType members: {sorted(extra)}"

    assert len(PAYLOAD_MODEL_MAP) == len(EventType) == 41


def test_fold_handles_every_event_type_explicitly():
    tree = ast.parse(FOLD_PATH.read_text(encoding="utf-8"))

    matched: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Match):
            continue
        for clause in node.cases:
            for name in _names_in_pattern(clause.pattern):
                if name in _event_type_members():
                    matched.add(name)

    unhandled = _event_type_members() - matched
    assert not unhandled, (
        "fold_events has no explicit match branch for (new events must either "
        f"mutate state or be an explicit no-op): {sorted(unhandled)}"
    )


def test_fold_has_no_wildcard_default_case():
    """A bare ``case _:`` default would silently swallow unlisted event types."""
    tree = ast.parse(FOLD_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Match):
            continue
        for clause in node.cases:
            if isinstance(clause.pattern, ast.MatchAs) and clause.pattern.pattern is None:
                raise AssertionError("fold_events must not use a bare `case _:` default")


def _names_in_pattern(pattern: ast.pattern) -> set[str]:
    """Extract EventType member names referenced anywhere in a match pattern."""
    names: set[str] = set()
    if isinstance(pattern, ast.MatchValue):
        value = pattern.value
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            names.add(value.attr)
    elif isinstance(pattern, ast.MatchOr):
        for sub in pattern.patterns:
            names.update(_names_in_pattern(sub))
    elif isinstance(pattern, ast.MatchAs):
        if pattern.pattern is not None:
            names.update(_names_in_pattern(pattern.pattern))
    return names
