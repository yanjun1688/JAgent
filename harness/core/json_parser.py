"""Shared utilities for parsing JSON from LLM text responses.

LLMs commonly wrap JSON in markdown code fences, prefix with explanatory
text, or suffix with trailing commentary.  This module provides a single,
robust extraction function used by all evaluator checks and any other
component that needs to parse structured output from LLMs.
"""

from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction from an LLM text response.

    Attempts the following strategies in order:
      1. Direct ``json.loads`` on the whole trimmed text.
      2. Strip leading/trailing `` ``` `` fences then ``json.loads``.
      3. Find the first ``{`` and last ``}`` (balanced) and parse that
         substring — this handles JSON embedded in natural-language
         preamble and postamble, which is the most common LLM output
         pattern.
      4. Find `` ```json `` / ````` `` fence blocks and extract content.

    Returns the parsed dict, or *None* if no valid JSON could be found.
    """
    text = text.strip()
    if not text:
        return None

    # Strategy 1 — plain JSON
    parsed = _try_loads(text)
    if parsed is not None:
        return parsed

    # Strategy 2 — strip ``` fences from the whole text
    stripped = _strip_fences(text)
    if stripped is not text:
        parsed = _try_loads(stripped)
        if parsed is not None:
            return parsed

    # Strategy 3 — extract the outermost JSON object (handles text
    # like "Here is the result: {...} Hope this helps.")
    parsed = _extract_balanced_json(text)
    if parsed is not None:
        return parsed

    # Strategy 4 — find fenced code blocks within the text
    parsed = _extract_fenced_json(text)
    if parsed is not None:
        return parsed

    return None


def _try_loads(s: str) -> dict | None:
    try:
        result = json.loads(s)
        if isinstance(result, dict):
            return result
        return None
    except (json.JSONDecodeError, ValueError):
        return None


def _strip_fences(text: str) -> str:
    lines = text.split("\n")
    if not lines:
        return text
    if lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _extract_balanced_json(text: str) -> dict | None:
    """Find the first ``{`` and the matching closing ``}`` using
    brace-depth counting, then attempt to parse the substring.

    This is the key improvement over the previous implementation:
    the old code passed ``rest[:end+1]`` to ``json.loads`` after
    stripping the opening ``{``, which produced invalid JSON when
    the marker was ``{`` (it was missing the leading brace).
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == -1:
        return None

    return _try_loads(text[start : end + 1])


def _extract_fenced_json(text: str) -> dict | None:
    """Look for `` ```json `` blocks within the text."""
    pattern = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
    for match in pattern.finditer(text):
        content = match.group(1).strip()
        parsed = _try_loads(content)
        if parsed is not None:
            return parsed
    return None
