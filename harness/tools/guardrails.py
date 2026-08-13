"""Guardrail framework — pre-execution safety checks for tool calls (V0.4).

Includes:
- SchemaGuardrail (built-in, always first)
- ScopeGuardrail (7.1): path/domain whitelist enforcement
- RateLimitGuardrail (7.2): per-tool/per-run rate limiting
- DestructiveOpGuardrail (7.3): auto-trigger confirmation on dangerous ops
- DependencyGuardrail (7.4): Event Store prerequisite event check
"""

import asyncio
import inspect
import shlex
import time
from dataclasses import dataclass
from typing import Any

import jsonschema

from harness.core.logger import guard_logger
from harness.execution.base import ExecutionBackend
from harness.models.tools import DependencyConstraint, SideEffect, ToolDefinition
from harness.models.workspace import WorkspaceScope

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

    ADR-010 D-04: contract-driven — the tool declares its scope targets via
    ``ToolDefinition.scope_targets`` (kind: path/domain/command + input_field).
    Whitelist config keys default per kind:
        path → allowed_directories, domain → allowed_domains, command → allowed_commands.
    Empty whitelist means "no restriction" (legacy behaviour preserved).
    """

    GUARDRAIL_ID = "scope"

    _DEFAULT_CONFIG_KEYS = {
        "path": "allowed_directories",
        "domain": "allowed_domains",
        "command": "allowed_commands",
    }

    def __init__(self, **kwargs):
        pass

    @staticmethod
    def _url_domain(url: str) -> str:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""

    @staticmethod
    def _path_within(path: str, directories: list[str]) -> bool:
        """Return True when ``path`` falls inside at least one allowed directory."""
        from os.path import commonpath
        from posixpath import commonpath as posix_commonpath

        normalized = path.replace("\\", "/")
        for directory in directories:
            directory = directory.replace("\\", "/")
            try:
                if normalized.startswith("/") or directory.startswith("/"):
                    if posix_commonpath([normalized, directory]) == directory:
                        return True
                elif commonpath([normalized, directory]) == directory:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def check(tool_def: ToolDefinition, input: dict, config: dict[str, Any]) -> GuardrailResult:
        """Sync check — path targets use local path rules (no backend resolve)."""
        for target in tool_def.scope_targets:
            key = target.config_key or ScopeGuardrail._DEFAULT_CONFIG_KEYS[target.kind]
            allowed = list(config.get(key, []))
            raw = input.get(target.input_field)
            if target.kind == "path":
                if raw is None or not allowed:
                    continue
                if not ScopeGuardrail._path_within(str(raw), allowed):
                    return GuardrailResult(
                        False,
                        ScopeGuardrail.GUARDRAIL_ID,
                        f"Path '{raw}' is outside allowed directories: {allowed}",
                    )
            elif target.kind == "domain":
                if not allowed:
                    continue
                domain = ScopeGuardrail._url_domain(str(raw or ""))
                if not any(domain == d or domain.endswith("." + d) for d in allowed):
                    return GuardrailResult(
                        False,
                        ScopeGuardrail.GUARDRAIL_ID,
                        f"Domain '{domain}' is not in allowed list: {allowed}",
                    )
            elif target.kind == "command":
                if not allowed:
                    continue
                command = str(raw or "")
                try:
                    argv = shlex.split(command, posix=False)
                except ValueError:
                    argv = []
                shell_tokens = (";", "&&", "||", "|", ">", "<", "`", "$", "(", ")")
                has_shell_operator = any(token in command for token in shell_tokens)
                executable = argv[0].strip("\"'") if argv else ""
                if not executable or has_shell_operator or executable not in set(allowed):
                    return GuardrailResult(
                        False,
                        ScopeGuardrail.GUARDRAIL_ID,
                        f"Command not in allowed list: {allowed}",
                    )
        return GuardrailResult(passed=True, guardrail_id=ScopeGuardrail.GUARDRAIL_ID, reason="")

    @classmethod
    async def check_async(
        cls,
        tool_def: ToolDefinition,
        input: dict,
        config: dict[str, Any],
        *,
        backend: ExecutionBackend | None = None,
    ) -> GuardrailResult:
        """Async check — path targets with a backend are resolved by the trusted backend."""
        if backend is not None:
            for target in tool_def.scope_targets:
                if target.kind != "path":
                    continue
                raw = input.get(target.input_field)
                if raw is None:
                    continue
                try:
                    await backend.resolve(str(raw))
                except PermissionError as exc:
                    return GuardrailResult(False, ScopeGuardrail.GUARDRAIL_ID, str(exc))
        return cls.check(tool_def, input, config)


class ToolWhitelistGuardrail:
    GUARDRAIL_ID = "tool_whitelist"

    @staticmethod
    def check(
        tool_def: ToolDefinition,
        input: dict,
        config: dict[str, Any],
        *,
        workspace_scope: WorkspaceScope | None = None,
        **_kwargs: Any,
    ) -> GuardrailResult:
        allowed = workspace_scope.allowed_tools if workspace_scope else None
        if allowed is not None and tool_def.name not in allowed:
            return GuardrailResult(
                False, ToolWhitelistGuardrail.GUARDRAIL_ID, f"Tool '{tool_def.name}' not allowed in this workspace"
            )
        return GuardrailResult(True, ToolWhitelistGuardrail.GUARDRAIL_ID, "")


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
    """Detect destructive operations and trigger the confirmation flow.

    ADR-010 D-04: contract-driven — a matching ``OperationContract`` whose
    ``side_effects`` contains DELETE, or that declares ``requires_confirmation``,
    triggers confirmation.  ``config.destructive_operations`` remains an override
    list of operation names (legacy file_op write behaviour preserved).
    """

    GUARDRAIL_ID = "destructive_op"

    def __init__(self, **kwargs):
        pass

    @staticmethod
    def check(tool_def: ToolDefinition, input: dict, config: dict[str, Any]) -> GuardrailResult:
        destructive_ops = list(config.get("destructive_operations", ["delete"]))

        op = tool_def.resolve_operation(input)
        if op is not None and (SideEffect.DELETE in op.side_effects or op.requires_confirmation):
            return GuardrailResult(
                passed=True,
                guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID,
                reason="Destructive operation requires confirmation",
                triggers_confirmation=True,
            )

        value = input.get(tool_def.operation_key)
        if value in destructive_ops:
            return GuardrailResult(
                passed=True,
                guardrail_id=DestructiveOpGuardrail.GUARDRAIL_ID,
                reason=f"Destructive operation '{value}' requires confirmation",
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

    async def run(
        self,
        tool_def: ToolDefinition,
        input: dict,
        *,
        run_id: str | None = None,
        workspace_scope: WorkspaceScope | None = None,
        backend: ExecutionBackend | None = None,
    ) -> list[GuardrailResult]:
        results: list[GuardrailResult] = []
        _t0 = time.monotonic()

        _t1 = time.monotonic()
        schema_result = SchemaGuardrail.check(tool_def, input)
        _ms1 = (time.monotonic() - _t1) * 1000
        _log_guard.debug("  schema → %s (%dms)", "pass" if schema_result.passed else "FAIL", _ms1)
        results.append(schema_result)
        if not schema_result.passed:
            _log_guard.warning(
                "Blocked by schema guardrail: %s (%.1fms)", schema_result.reason, (time.monotonic() - _t0) * 1000
            )
            return results

        whitelist = ToolWhitelistGuardrail.check(tool_def, input, {}, workspace_scope=workspace_scope)
        if not whitelist.passed:
            results.append(whitelist)
            return results

        # Auto-check depends_on — always runs if declared, regardless of guardrails list
        dep_result = await self._auto_check_depends_on(tool_def, input, run_id=run_id)
        if dep_result is not None:
            _ms_dep = (time.monotonic() - _t0) * 1000
            _log_guard.info(
                "  depends_on → %s%s (%dms)",
                "pass" if dep_result.passed else "FAIL",
                f" — {dep_result.reason}" if not dep_result.passed else "",
                _ms_dep,
            )
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
                if gr.guardrail_type == "scope" and backend is not None:
                    result = await ScopeGuardrail.check_async(tool_def, input, gr.config, backend=backend)
                elif asyncio.iscoroutinefunction(instance.check):
                    # Pass run_id only to guardrails that declare it (currently
                    # DependencyGuardrail). Signature introspection replaces the
                    # previous brittle except-TypeError string-matching fallback.
                    sig = inspect.signature(instance.check)
                    kwargs: dict[str, Any] = {}
                    if "run_id" in sig.parameters:
                        kwargs["run_id"] = run_id
                    result = await instance.check(tool_def, input, gr.config, **kwargs)
                else:
                    result = instance.check(tool_def, input, gr.config)
                _ms_gr = (time.monotonic() - _t_gr) * 1000

                detail = ""
                if not result.passed:
                    detail = f" — {result.reason}"
                elif result.triggers_confirmation:
                    detail = " (triggers confirmation)"
                _log_guard.info(
                    "  %s → %s%s (%dms)", gr.guardrail_type, "pass" if result.passed else "FAIL", detail, _ms_gr
                )
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
