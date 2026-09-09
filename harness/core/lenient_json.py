"""Lenient JSON parsing for non-trusted LLM text outputs (R8-a).

LLMs frequently wrap a JSON object in markdown code fences or surround it with
prose. Every call site previously hand-copied the same "strip fence → try whole
text → slice outermost ``{...}`` mechanics (five divergent copies). This module
is the single implementation; call sites keep their own failure vocabulary
(Planner feeds a detailed reason back to the model, the judge / local-repair
collapse to ``None``) by mapping :class:`LenientJsonError` themselves.

All outputs parsed here are non-trusted LLM text and remain subject to the
trusted PlanGuardrail / Scheduler / contract validation before any effect.
"""

from __future__ import annotations

import json


class LenientJsonError(ValueError):
    """The text could not be recovered into parseable JSON.

    ``kind`` is ``"no_object"`` when no ``{...}`` span exists at all, or
    ``"invalid"`` when a span existed but failed to parse; ``cause`` carries the
    final :class:`json.JSONDecodeError` for callers that want position detail.
    """

    def __init__(self, kind: str, *, cause: json.JSONDecodeError | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.cause = cause


def strip_code_fence(text: str | None) -> str:
    """Remove a leading markdown `````lang`` fence and a trailing ```````."""
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
        stripped = stripped.strip()
    return stripped


def parse_lenient_json(text: str | None) -> object:
    """Best-effort parse of LLM text into a JSON value.

    Tries the (fence-stripped) whole text first; on failure, extracts the
    outermost ``{...}`` span and retries. Raises :class:`LenientJsonError`
    (``no_object`` / ``invalid``) rather than returning ``None`` so that callers
    distinguish "no JSON present" from "JSON present but malformed". A
    successful parse may still be a non-object (e.g. a list); callers validate
    the type themselves.
    """
    stripped = strip_code_fence(text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as whole_error:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            raise LenientJsonError("no_object") from whole_error
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as slice_error:
            raise LenientJsonError("invalid", cause=slice_error) from slice_error
