"""Guardrail framework — pre-execution safety checks for tool calls (V0.4).

Includes:
- SchemaGuardrail (built-in, always first)
- ScopeGuardrail (7.1): path/domain whitelist enforcement
- RateLimitGuardrail (7.2): per-tool/per-run rate limiting
- DestructiveOpGuardrail (7.3): auto-trigger confirmation on dangerous ops
- DependencyGuardrail (7.4): Event Store prerequisite event check
"""

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

import jsonschema

from harness.core.logger import guard_logger
from harness.models.tools import DependencyConstraint, ToolDefinition

_log_guard = guard_logger("executor.guardrails")


@dataclass
class GuardrailResult:
    passed: bool
    guardrail_id: str
    reason: str
    triggers_confirmation: bool = False  # V0.4: DestructiveOpGuardrail signals this


# ── Built-in: SchemaGuardrail ─────────────────────────────────────


class SchemaGuardrail:
    GUARDRAIL_ID = "schema"

    @staticmethod
    def check(tool_def: ToolDefinition, input: dict) -> GuardrailResult:
        schema = tool_def.input_schema or {}
        try:
            jsonschema.validate(instance=input, schema=schema)
            return GuardrailResult(passed=True, guardrail_id=SchemaGuardrail.GUARDRAIL_ID, reason="")
        except jsonschema.ValidationError as exc:
            return GuardrailResult(
                passed=False,
                guardrail_id=SchemaGuardrail.GUARDRAIL_ID,
                reason=exc.message,
            )


# ── 7.1 ScopeGuardrail ────────────────────────────────────────────


class ScopeGuardrail:
    """Check operation targets against allowed scopes.

    Config (in Guardrail.config):
        allowed_directories: list[str]  — path prefixes for file operations
        allowed_domains: list[str]      — domain whitelist for HTTP/browser
        allowed_commands: list[str]     — command whitelist (future, run_code)
    """

    GUARDRAIL_ID = "scope"

    def __init__(self, **kwargs):
        pass

    @staticmethod
    def check(tool_def: ToolDefinition, input: dict, config: dict[str, Any]) -> GuardrailResult:
        allowed_dirs = config.get("allowed_directories", [])
        allowed_domains = config.get("allowed_domains", [])
        allowed_commands = config.get("allowed_commands", [])

        if tool_def.name == "file_op":
            path = (input.get("path") or "").replace("\\", "/")
            dirs = list(allowed_dirs) if allowed_dirs else []
            source = "allowed directories"
            if not dirs:
                # 无显式配置时，回退到 file_op 的沙箱根目录。
                # 后期可扩展为服务器安全目录白名单、用户 home 目录等来源。
                from harness.tools.file_op import _SANDBOX_BASE
                sb = _SANDBOX_BASE
                if sb is not None:
                    dirs = [str(sb.resolve()).replace("\\", "/")]
                    source = "sandbox root"
            if dirs:
                resolved = path
                if not os.path.isabs(path):
                    resolved = os.path.join(dirs[0], path).replace("\\", "/")
                    resolved = os.path.abspath(resolved).replace("\\", "/")
                if not any(resolved.startswith(d) for d in dirs):
                    return GuardrailResult(
                        passed=False,
                        guardrail_id=ScopeGuardrail.GUARDRAIL_ID,
                        reason=f"Path '{path}' is outside {source}: {dirs}",
                    )

        if tool_def.name in ("http_request", "browser"):
            url = input.get("url") or input.get("arguments", {}).get("url", "")
            if allowed_domains:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                domain = parsed.hostname or ""
                if not any(domain == d or domain.endswith("." + d) for d in allowed_domains):
                    return GuardrailResult(
                        passed=False,
                        guardrail_id=ScopeGuardrail.GUARDRAIL_ID,
                        reason=f"Domain '{domain}' is not in allowed list: {allowed_domains}",
                    )

        if tool_def.name == "run_code":
            command = input.get("command", "")
            if allowed_commands and not any(cmd in command for cmd in allowed_commands):
                return GuardrailResult(
                    passed=False,
                    guardrail_id=ScopeGuardrail.GUARDRAIL_ID,
                    reason=f"Command not in allowed list: {allowed_commands}",
                )

        return GuardrailResult(passed=True, guardrail_id=ScopeGuardrail.GUARDRAIL_ID, reason="")


# ── 7.2 RateLimitGuardrail ────────────────────────────────────────


class RateLimitGuardrail:
    """Rate-limit tool calls within a time window.

    Config:
        max_calls: int         — max calls allowed (default 10)
        window_seconds: int    — sliding window in seconds (default 60)
        scope: str             — "tool" (per tool name) or "run" (per run_id+tool)

    Note: _call_history is a class-level dict. Call reset() between test cases
    to prevent cross-test pollution.
    """

    GUARDRAIL_ID = "rate_limit"

    def __init__(self, **kwargs):
        pass

    _call_history: dict[str, list[float]] = {}

    @staticmethod
    def _make_key(scope: str, tool_name: str, config: dict) -> str:
        if scope == "run":
            run_id = config.get("run_id", "")
            return f"run:{run_id}:{tool_name}"
        return f"tool:{tool_name}"

    @classmethod
    def check(cls, tool_def: ToolDefinition, input: dict, config: dict[str, Any]) -> GuardrailResult:
        max_calls = config.get("max_calls", 10)
        window = config.get("window_seconds", 60)
        scope = config.get("scope", "tool")
        now = time.time()

        key = cls._make_key(scope, tool_def.name, config)
        history = cls._call_history.setdefault(key, [])
        history[:] = [t for t in history if now - t < window]
        if not history:
            del cls._call_history[key]
            history = cls._call_history.setdefault(key, [])

        if len(history) >= max_calls:
            return GuardrailResult(
                passed=False,
                guardrail_id=RateLimitGuardrail.GUARDRAIL_ID,
                reason=f"Rate limit exceeded: {max_calls} calls per {window}s (scope={scope})",
            )

        history.append(now)
        return GuardrailResult(passed=True, guardrail_id=RateLimitGuardrail.GUARDRAIL_ID, reason="")

    @classmethod
    def reset(cls) -> None:
        cls._call_history.clear()


# ── 7.3 DestructiveOpGuardrail ────────────────────────────────────


class DestructiveOpGuardrail:
    """Detect destructive operations and trigger confirmation flow.

    Config:
        destructive_operations: list[str]  — input operations to treat as destructive (default ["delete"])
    """

    GUARDRAIL_ID = "destructive_op"

    def __init__(self, **kwargs):
        pass

    @staticmethod
    def check(tool_def: ToolDefinition, input: dict, config: dict[str, Any]) -> GuardrailResult:
        destructive_ops = config.get("destructive_operations", ["delete"])

        if tool_def.name == "file_op" and input.get("operation") in destructive_ops:
            return GuardrailResult(
                passed=True,
                guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID,
                reason=f"Destructive operation '{input['operation']}' requires confirmation",
                triggers_confirmation=True,
            )

        if tool_def.name == "run_code":
            return GuardrailResult(
                passed=True,
                guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID,
                reason="Code execution requires confirmation",
                triggers_confirmation=True,
            )

        if tool_def.name == "file_op" and input.get("operation") == "write":
            if "write" not in destructive_ops:
                return GuardrailResult(passed=True, guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID, reason="")
            return GuardrailResult(
                passed=True,
                guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID,
                reason="Write operation requires confirmation",
                triggers_confirmation=True,
            )

        return GuardrailResult(passed=True, guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID, reason="")


# ── 7.4 DependencyGuardrail ───────────────────────────────────────


class DependencyGuardrail:
    """Check that prerequisite events exist in the Event Store.

    Two constraint sources (checked in this order):
      1. tool_def.depends_on  — declarative list of DependencyConstraint (recommended)
      2. config["required_events"] — legacy list of event type strings

    If tool_def.depends_on is non-empty, config["required_events"] is ignored.
    """

    GUARDRAIL_ID = "dependency"

    def __init__(self, store=None):
        self._store = store

    async def check(
        self, tool_def: ToolDefinition, input: dict, config: dict[str, Any], *, run_id: str | None = None
    ) -> GuardrailResult:
        if self._store is None:
            return GuardrailResult(
                passed=True,
                guardrail_id=DependencyGuardrail.GUARDRAIL_ID,
                reason="EventStore not available — skipping dependency check",
            )

        constraints = self._resolve_constraints(tool_def, config)
        if not constraints:
            return GuardrailResult(passed=True, guardrail_id=DependencyGuardrail.GUARDRAIL_ID, reason="")

        rid = run_id or config.get("run_id", "")
        if not rid:
            return GuardrailResult(passed=True, guardrail_id=DependencyGuardrail.GUARDRAIL_ID, reason="")

        events = await self._store.get_events(rid)

        for constraint in constraints:
            matched = any(
                e.event_type.value == constraint.event_type
                and self._matches_payload(e.payload, constraint.payload_filter)
                for e in events
            )
            if not matched:
                reason = constraint.message or f"Prerequisite event '{constraint.event_type}' not found"
                return GuardrailResult(
                    passed=False,
                    guardrail_id=DependencyGuardrail.GUARDRAIL_ID,
                    reason=reason,
                )

        return GuardrailResult(passed=True, guardrail_id=DependencyGuardrail.GUARDRAIL_ID, reason="")

    @staticmethod
    def _matches_payload(payload: dict[str, Any], filter_: dict[str, Any]) -> bool:
        """Check that all filter key-value pairs are present (and match) in payload."""
        return all(payload.get(k) == v for k, v in filter_.items())

    @staticmethod
    def _resolve_constraints(tool_def: ToolDefinition, config: dict[str, Any]) -> list[DependencyConstraint]:
        """Return constraints from tool_def.depends_on (preferred) or legacy config."""
        if tool_def.depends_on:
            return tool_def.depends_on
        required = config.get("required_events", [])
        return [DependencyConstraint(event_type=ev) for ev in required]


# ── GuardrailRunner (V0.4: async, store-aware) ────────────────────


class GuardrailRunner:
    def __init__(self, custom_guardrails: dict[str, type] | None = None, store=None):
        self._registry: dict[str, type] = {
            "destructive": DestructiveOpGuardrail,
            "scope": ScopeGuardrail,
            "rate_limit": RateLimitGuardrail,
            "dependency": DependencyGuardrail,
        }
        if custom_guardrails:
            self._registry.update(custom_guardrails)
        self._store = store

    def register(self, guardrail_type: str, guardrail_cls: type):
        self._registry[guardrail_type] = guardrail_cls

    async def run(self, tool_def: ToolDefinition, input: dict, *, run_id: str | None = None) -> list[GuardrailResult]:
        results: list[GuardrailResult] = []
        _t0 = time.monotonic()

        _t1 = time.monotonic()
        schema_result = SchemaGuardrail.check(tool_def, input)
        _ms1 = (time.monotonic() - _t1) * 1000
        _log_guard.debug("  schema → %s (%dms)", "pass" if schema_result.passed else "FAIL", _ms1)
        results.append(schema_result)
        if not schema_result.passed:
            _log_guard.warning("Blocked by schema guardrail: %s (%.1fms)", schema_result.reason,
                               (time.monotonic() - _t0) * 1000)
            return results

        # Auto-check depends_on — always runs if declared, regardless of guardrails list
        dep_result = await self._auto_check_depends_on(tool_def, input, run_id=run_id)
        if dep_result is not None:
            _ms_dep = (time.monotonic() - _t0) * 1000
            _log_guard.info("  depends_on → %s%s (%dms)",
                            "pass" if dep_result.passed else "FAIL",
                            f" — {dep_result.reason}" if not dep_result.passed else "",
                            _ms_dep)
            results.append(dep_result)
            if not dep_result.passed:
                return results

        if tool_def.guardrails:
            for gr in tool_def.guardrails:
                guardrail_cls = self._registry.get(gr.guardrail_type)
                if guardrail_cls is None:
                    _log_guard.warning("Unknown guardrail type '%s', blocking execution", gr.guardrail_type)
                    results.append(
                        GuardrailResult(
                            passed=False,
                            guardrail_id=gr.guardrail_type,
                            reason=f"Unknown guardrail type: {gr.guardrail_type}",
                        )
                    )
                    return results

                instance = guardrail_cls(store=self._store) if self._store else guardrail_cls()

                _t_gr = time.monotonic()
                if asyncio.iscoroutinefunction(instance.check):
                    result = await instance.check(tool_def, input, gr.config, run_id=run_id)
                else:
                    result = instance.check(tool_def, input, gr.config)
                _ms_gr = (time.monotonic() - _t_gr) * 1000

                detail = ""
                if not result.passed:
                    detail = f" — {result.reason}"
                elif result.triggers_confirmation:
                    detail = " (triggers confirmation)"
                _log_guard.info("  %s → %s%s (%dms)",
                                gr.guardrail_type, "pass" if result.passed else "FAIL",
                                detail, _ms_gr)
                results.append(result)
                if not result.passed:
                    return results

        _log_guard.info("All %d guardrails passed (%.1fms)", len(results), (time.monotonic() - _t0) * 1000)
        return results

    async def _auto_check_depends_on(
        self, tool_def: ToolDefinition, input: dict, *, run_id: str | None = None
    ) -> GuardrailResult | None:
        """Run DependencyGuardrail if tool_def.depends_on is declared, without needing guardrails list entry."""
        if not tool_def.depends_on:
            return None
        dep = DependencyGuardrail(store=self._store)
        return await dep.check(tool_def, input, {}, run_id=run_id)
