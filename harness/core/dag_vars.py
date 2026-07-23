"""Variable resolution utilities for DagPlan step input.

Extracted from DagExecutor static methods to keep the executor focused
on orchestration. All functions are pure (side-effect-free except logging).

V2.2: Unified path resolution.  Both pure references ("$s6.result") and
inline substitutions ("prefix $s6.result suffix") now route through a single
_resolve_ref() core.  When a variable exists in upstream but the requested
field path cannot be traversed, VariableResolutionError is raised instead of
silently returning None or the raw placeholder string.
"""

from __future__ import annotations

import json
import re
from typing import Any

from harness.core.logger import agent_logger

_log = agent_logger("dag_vars")

_OUTPUT_SUMMARY_MAX_CHARS = 200

# ── Sentinels ────────────────────────────────────────────────────────────

class VariableResolutionError(ValueError):
    """Raised when a $var.field reference cannot be resolved because the
    variable exists in upstream but the field path is missing.
    """

    def __init__(self, reference: str, var_name: str, path: str, available_keys: list[str] | None = None):
        self.reference = reference
        self.var_name = var_name
        self.path = path
        self.available_keys = available_keys or []
        msg = f"Cannot resolve '{reference}' — step '{var_name}' output has no field '{path}'"
        if self.available_keys:
            msg += f". Available fields: {sorted(self.available_keys)}"
        super().__init__(msg)


class _RefNotFound:
    """Sentinel: variable name is not a key in upstream at all."""
    pass

_REF_NOT_FOUND = _RefNotFound()

# ── Core resolution ─────────────────────────────────────────────────────

def _resolve_ref(var_name: str, path: str | None, upstream: dict[str, Any]) -> Any:
    """Resolve a single ``$var_name`` or ``$var_name.path`` reference.

    Returns the resolved value (may be ``None`` for an explicit null field).
    Raises ``VariableResolutionError`` if *var_name* is in upstream but the
    path cannot be traversed (field does not exist).
    Returns ``_REF_NOT_FOUND`` if *var_name* is not in upstream at all, so
    callers can keep the raw placeholder text.
    """
    if var_name not in upstream:
        _log.warning("[var] '%s' not found in upstream outputs", var_name)
        return _REF_NOT_FOUND

    value = upstream[var_name]
    if value is None or path is None:
        return value

    # ── Path traversal ──────────────────────────────────────────────
    parts = path.split(".")
    source_for_deep = upstream[var_name]
    field_missing = False

    for part in parts:
        if isinstance(value, dict):
            if part in value:
                value = value[part]
            else:
                value = None
                field_missing = True
                break
        elif isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                field_missing = True
                break
            if isinstance(parsed, dict):
                if part in parsed:
                    value = parsed[part]
                else:
                    value = None
                    field_missing = True
                    break
            else:
                field_missing = True
                break
        else:
            field_missing = True
            break

    # ── Fuzzy fallback ──────────────────────────────────────────────
    if field_missing and isinstance(source_for_deep, dict):
        found = deep_resolve(source_for_deep, parts)
        if found is not None:
            _log.info("[var] $%s.%s resolved via deep search", var_name, path)
            return found

    # ── Hard error for explicit field path on existing var ───────────
    if field_missing:
        raise VariableResolutionError(
            reference=f"${var_name}.{path}",
            var_name=var_name,
            path=path,
            available_keys=_collect_keys(source_for_deep),
        )

    return value

# ── Public API ───────────────────────────────────────────────────────────

def resolve_variables_in_input(step_input: dict, upstream: dict[str, Any]) -> dict:
    """Resolve $var and $var.path references in a step input dict.

    Handles:
      - Pure references like "$var_name" or "$var_name.a.b" (entire value is a ref)
      - Inline references like "prefix_${var}" via :func:`substitute_vars`
      - Nested dict and list recursion

    Raises:
        VariableResolutionError: a pure reference names an existing variable
            but the field path does not exist.
    """
    resolved = {}
    for key, value in step_input.items():
        if isinstance(value, str):
            pure = re.match(r'^\$(\w+)(?:\.([\w.]+))?$', value)
            if pure:
                var_name = pure.group(1)
                path = pure.group(2)
                rv = _resolve_ref(var_name, path, upstream)
                if rv is not _REF_NOT_FOUND:
                    resolved[key] = rv
                    continue
                _log.warning("[var] '%s' not found in upstream outputs, keeping raw placeholder", value)
                resolved[key] = value
            resolved[key] = substitute_vars(value, upstream)
        elif isinstance(value, dict):
            resolved[key] = resolve_variables_in_input(value, upstream)
        elif isinstance(value, list):
            resolved_list = []
            for item in value:
                if isinstance(item, str):
                    resolved_list.append(substitute_vars(item, upstream))
                elif isinstance(item, dict):
                    resolved_list.append(resolve_variables_in_input(item, upstream))
                else:
                    resolved_list.append(item)
            resolved[key] = resolved_list
        else:
            resolved[key] = value
    return resolved


def substitute_vars(text: str, upstream: dict[str, Any]) -> str:
    """Inline substitute $var and $var.path references within a string.

    When *var_name* does not appear in upstream the raw placeholder is kept
    (this handles literals like ``$100`` that the regex captures but are not
    actual variable names).  When *var_name* **does** exist but the field path
    cannot be traversed ``VariableResolutionError`` is raised.

    Raises:
        VariableResolutionError: a referenced variable exists in upstream
            but the field path is missing.
    """

    def _replacer(m: re.Match) -> str:
        var_name = m.group(1)
        path = m.group(2)
        resolved = _resolve_ref(var_name, path, upstream)
        if resolved is _REF_NOT_FOUND:
            return m.group(0)
        return "null" if resolved is None else str(resolved)

    return re.sub(r'\$(\w+)(?:\.([\w.]+))?', _replacer, text)


def deep_resolve(output: dict, parts: list[str], _depth: int = 0) -> Any:
    """Recursive fuzzy search through nested dict values for a path match.

    Depth-limited to 5 to prevent runaway recursion on deeply nested outputs.
    Also tries parsing JSON string values to find nested dicts.
    """
    if _depth > 5 or not output:
        return None
    for v in output.values():
        if isinstance(v, dict):
            current = v
            for part in parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    break
            else:
                return current
            result = deep_resolve(v, parts, _depth + 1)
            if result is not None:
                return result
        elif isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    current = parsed
                    for part in parts:
                        if isinstance(current, dict) and part in current:
                            current = current[part]
                        else:
                            break
                    else:
                        return current
                    result = deep_resolve(parsed, parts, _depth + 1)
                    if result is not None:
                        return result
            except (json.JSONDecodeError, TypeError):
                pass
    return None


def _collect_keys(obj: Any) -> list[str]:
    """Collect available top-level + one-level-nested keys for error messages."""
    if not isinstance(obj, dict):
        return []
    keys: list[str] = []
    for k, v in obj.items():
        keys.append(k)
        if isinstance(v, dict):
            for nk in v:
                dotted = f"{k}.{nk}"
                if dotted not in keys:
                    keys.append(dotted)
    return keys


def truncate_output(output: Any, max_chars: int = _OUTPUT_SUMMARY_MAX_CHARS) -> str:
    """Truncate tool output to a summary string of at most max_chars."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output[:max_chars]
    text = json.dumps(output, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text
