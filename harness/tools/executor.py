"""Tool Executor — 8-step execution flow with idempotency, guardrails, confirmation, and retry."""

import asyncio
import contextvars
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import jsonschema

from harness.core.logger import agent_logger, guard_logger
from harness.models.events import (
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
    EventType,
    GuardrailTriggeredPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolTimeoutPayload,
)
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.guardrails import GuardrailRunner
from harness.tools.idempotency import IdempotencyKeyGenerator
from harness.tools.retry import RetryRunner
from harness.tools.sandbox import Sandbox

_guard_log = guard_logger("executor")
_agent_log = agent_logger("executor")
_log_guardrails = guard_logger("executor.guardrails")
_log_idem = guard_logger("executor.idempotency")
_log_confirm = guard_logger("executor.confirm")
_log_sandbox = guard_logger("executor.sandbox")

# Context variable that tool functions can read to discover the current run_id.
# Set by ToolExecutor.execute() before each invocation.
current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_run_id", default="")


class ConfirmationNeededError(Exception):
    """Raised by tool wrappers when an inner tool requires human confirmation.
    
    Caught by ToolExecutor.execute() and converted to CONFIRMATION_NEEDED status
    so the Scheduler can pause and wait for operator confirmation.
    """
    def __init__(self, tool_name: str, confirmation_id: str):
        self.tool_name = tool_name
        self.confirmation_id = confirmation_id
        super().__init__(f"Tool '{tool_name}' requires confirmation (id={confirmation_id})")


class ExecutionStatus(Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    GUARDRAIL_BLOCKED = "guardrail_blocked"
    CONFIRMATION_NEEDED = "confirmation_needed"
    IDEMPOTENCY_HIT = "idempotency_hit"


@dataclass
class ToolExecutionResult:
    status: ExecutionStatus
    tool_call_id: str
    tool_name: str
    idempotency_key: str | None = None
    output: Any = None
    duration_ms: int = 0
    error: str | None = None
    retryable: bool = False
    timeout_ms: int = 0
    guardrail_id: str | None = None
    guardrail_reason: str | None = None
    confirmation_id: str | None = None
    cached: bool = False
    retry_attempts: int = 0


class ToolExecutor:
    def __init__(self, store: EventStore, guardrail_runner: GuardrailRunner | None = None):
        self.store = store
        self.guardrails = guardrail_runner or GuardrailRunner(store=store)

    async def execute(
        self,
        run_id: str,
        tool_name: str,
        input: dict[str, Any],
        tool_def: ToolDefinition,
        tool_fn: Callable[[dict[str, Any]], Any],
        *,
        override_tool_call_id: str | None = None,
    ) -> ToolExecutionResult:
        _t0 = time.monotonic()

        # ── Step 0: Generate/Reuse tool_call_id ──────────────────
        tool_call_id = override_tool_call_id or str(uuid.uuid4())
        _guard_log.debug("[setup] tool_call_id=%s", tool_call_id)

        # ── Step 2: Compute idempotency key ──────────────────────
        ik_key = IdempotencyKeyGenerator.compute(tool_def, input)
        _log_idem.debug("[idem] ik=%s", ik_key or "null")

        # ── Step 1+4: Schema + Guardrails pre-checks ─────────────
        _t_gr = time.monotonic()
        gr_results = await self.guardrails.run(tool_def, input, run_id=run_id)
        _ms_gr = (time.monotonic() - _t_gr) * 1000
        guardrail_triggers_confirmation = any(
            getattr(gr, "triggers_confirmation", False) for gr in gr_results
        )

        for gr in gr_results:
            if not gr.passed:
                _log_guardrails.warning("[guardrails] Blocked by '%s': %s (%dms)",
                                        gr.guardrail_id, gr.reason, _ms_gr)
                await self.store.append_event(
                    run_id,
                    EventType.GUARDRAIL_TRIGGERED,
                    GuardrailTriggeredPayload(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        guardrail_id=gr.guardrail_id,
                        reason=gr.reason,
                    ).model_dump(),
                )
                return ToolExecutionResult(
                    status=ExecutionStatus.GUARDRAIL_BLOCKED,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    guardrail_id=gr.guardrail_id,
                    guardrail_reason=gr.reason,
                )
        _log_guardrails.info("[guardrails] All %d guardrails passed (%dms)", len(gr_results), _ms_gr)

        # ── Step 3: Idempotency cache lookup ─────────────────────
        if ik_key is not None:
            existing_tc = await self.store.find_by_idempotency_key(run_id, EventType.TOOL_COMPLETED, ik_key)
            if existing_tc is not None:
                payload = ToolCompletedPayload.model_validate(existing_tc.payload)
                _log_idem.info("[idem] Cache HIT (previous result @ seq=%d)", existing_tc.seq)
                return ToolExecutionResult(
                    status=ExecutionStatus.IDEMPOTENCY_HIT,
                    tool_call_id=payload.tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    output=payload.output,
                    duration_ms=payload.duration_ms,
                    cached=True,
                )
            _log_idem.debug("[idem] Cache miss")

        # ── Step 5: Confirmation check ───────────────────────────
        needs_confirmation = tool_def.requires_confirmation or guardrail_triggers_confirmation
        if needs_confirmation:
            _log_confirm.info("[confirm] Tool requires human confirmation")
            confirm_key = ik_key or f"_noik_{tool_name}"
            existing_req = await self.store.find_by_idempotency_key(
                run_id, EventType.CONFIRMATION_REQUESTED, confirm_key
            )
            if existing_req is not None:
                req_payload = ConfirmationRequestedPayload.model_validate(existing_req.payload)
                confirmed_event = await self._find_confirmation_received(run_id, req_payload.confirmation_id)
                if confirmed_event is not None:
                    cr_payload = ConfirmationReceivedPayload.model_validate(confirmed_event.payload)
                    if cr_payload.confirmed:
                        _log_confirm.info("[confirm] Operator confirmed, proceeding")
                        tool_call_id = req_payload.tool_call_id
                    else:
                        _log_confirm.info("[confirm] Operator declined")
                        await self.store.append_event(
                            run_id,
                            EventType.TOOL_FAILED,
                            ToolFailedPayload(
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                error="Confirmation denied by operator",
                                retryable=False,
                            ).model_dump(),
                        )
                        return ToolExecutionResult(
                            status=ExecutionStatus.FAILED,
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            idempotency_key=ik_key,
                            error="Confirmation denied by operator",
                        )
                else:
                    _log_confirm.info("[confirm] Waiting for operator (confirmation_id=%s)",
                                      req_payload.confirmation_id)
                    return ToolExecutionResult(
                        status=ExecutionStatus.CONFIRMATION_NEEDED,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        idempotency_key=ik_key,
                        confirmation_id=req_payload.confirmation_id,
                    )
            else:
                confirmation_id = str(uuid.uuid4())
                confirmation_payload = ConfirmationRequestedPayload(
                    confirmation_id=confirmation_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    input=input,
                    idempotency_key=confirm_key,
                )
                await self.store.append_event(
                    run_id,
                    EventType.CONFIRMATION_REQUESTED,
                    confirmation_payload.model_dump(),
                    idempotency_key=confirm_key,
                )
                _log_confirm.info("[confirm] Confirmation requested (id=%s)", confirmation_id)
                return ToolExecutionResult(
                    status=ExecutionStatus.CONFIRMATION_NEEDED,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    confirmation_id=confirmation_id,
                )

        # ── Step 6: Write ToolCalled ─────────────────────────────
        _agent_log.info("[exec] Writing ToolCalled event")
        await self.store.append_event(
            run_id,
            EventType.TOOL_CALLED,
            ToolCalledPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                input=input,
                idempotency_key=ik_key,
            ).model_dump(),
        )

        # ── Step 7: Execute via Sandbox with RetryRunner ─────────
        _log_sandbox.info("[sandbox] Executing (timeout=%dms)...", tool_def.timeout_ms)
        step7_start = time.monotonic()
        token = current_run_id.set(run_id)
        try:
            async def _run() -> Any:
                return await Sandbox.invoke(tool_fn, input, timeout_ms=tool_def.timeout_ms)

            output, retry_count = await RetryRunner.execute_with_retry(
                _run,
                policy=tool_def.retry_policy,
            )
            duration_ms = int((time.monotonic() - step7_start) * 1000)
            if isinstance(output, dict) and output.get("success") is False:
                _log_sandbox.warning("[sandbox] Soft failure: %s (%dms)",
                                     output.get("error", "success=False"), duration_ms)
                error_msg = output.get("error", "Tool returned success=False")
                tp = ToolFailedPayload(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    error=error_msg,
                    retryable=False,
                )
                await self.store.append_event(run_id, EventType.TOOL_FAILED, tp.model_dump())
                return ToolExecutionResult(
                    status=ExecutionStatus.FAILED,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    error=error_msg,
                    duration_ms=duration_ms,
                )
            _log_sandbox.info("[sandbox] Completed in %dms (retries=%d)", duration_ms, retry_count)

            if tool_def.output_schema:
                try:
                    jsonschema.validate(instance=output, schema=tool_def.output_schema)
                except jsonschema.ValidationError as exc:
                    _log_sandbox.warning("[sandbox] Output schema validation failed: %s (%dms)",
                                         exc.message, duration_ms)
                    tp = ToolFailedPayload(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        error=f"Output schema validation failed: {exc.message}",
                        retryable=False,
                    )
                    await self.store.append_event(run_id, EventType.TOOL_FAILED, tp.model_dump())
                    return ToolExecutionResult(
                        status=ExecutionStatus.FAILED,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        idempotency_key=ik_key,
                        error=f"Output schema validation failed: {exc.message}",
                        duration_ms=duration_ms,
                    )

            if tool_def.side_effects:
                _log_sandbox.info("[sidefx] tool=%s side_effects=%s",
                                  tool_name, [s.value for s in tool_def.side_effects])

            tp = ToolCompletedPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                output=output,
                duration_ms=duration_ms,
            )
            await self.store.append_event(run_id, EventType.TOOL_COMPLETED, tp.model_dump(), idempotency_key=ik_key)
            return ToolExecutionResult(
                status=ExecutionStatus.COMPLETED,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                idempotency_key=ik_key,
                output=output,
                duration_ms=duration_ms,
                retry_attempts=retry_count,
            )
        except asyncio.TimeoutError:
            duration_ms = int((time.monotonic() - step7_start) * 1000)
            _log_sandbox.warning("[sandbox] Timed out after %dms (limit=%dms)",
                                 duration_ms, tool_def.timeout_ms)
            tp = ToolTimeoutPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                timeout_ms=tool_def.timeout_ms,
            )
            await self.store.append_event(run_id, EventType.TOOL_TIMEOUT, tp.model_dump())
            return ToolExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                idempotency_key=ik_key,
                timeout_ms=tool_def.timeout_ms,
                duration_ms=duration_ms,
                error=f"Tool timed out after {duration_ms}ms",
            )
        except ConfirmationNeededError as exc:
            duration_ms = int((time.monotonic() - step7_start) * 1000)
            _log_confirm.info("[confirm] Tool '%s' needs confirmation (id=%s) — propagating from inner call",
                              exc.tool_name, exc.confirmation_id)
            return ToolExecutionResult(
                status=ExecutionStatus.CONFIRMATION_NEEDED,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                idempotency_key=ik_key,
                confirmation_id=exc.confirmation_id,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - step7_start) * 1000)
            retryable = bool(
                tool_def.retry_policy
                and any(
                    candidate in str(exc) or candidate in type(exc).__name__
                    for candidate in tool_def.retry_policy.retryable_errors
                )
            )
            _log_sandbox.error("[sandbox] Failed: %s (retryable=%s, %dms)",
                               exc, retryable, duration_ms)
            tp = ToolFailedPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                error=str(exc),
                retryable=retryable,
            )
            await self.store.append_event(run_id, EventType.TOOL_FAILED, tp.model_dump())
            return ToolExecutionResult(
                status=ExecutionStatus.FAILED,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                idempotency_key=ik_key,
                error=str(exc),
                retryable=retryable,
                duration_ms=duration_ms,
                retry_attempts=0,
            )
        finally:
            current_run_id.reset(token)

    # ── Helpers ──────────────────────────────────────────────────

    async def _find_confirmation_received(self, run_id: str, confirmation_id: str):
        return await self.store.find_confirmation_by_id(run_id, confirmation_id)
