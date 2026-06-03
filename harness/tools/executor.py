"""Tool Executor — 8-step execution flow with idempotency, guardrails, confirmation, and retry."""

import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

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
        self.guardrails = guardrail_runner or GuardrailRunner()

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
        # ── Step 0: Generate/Reuse tool_call_id ──────────────────
        tool_call_id = override_tool_call_id or str(uuid.uuid4())

        # ── Step 2: Compute idempotency key ──────────────────────
        # Returns None when tool_def.idempotency_key_fields is None
        # (opt-out of idempotency caching per architecture §5.2).
        ik_key = IdempotencyKeyGenerator.compute(tool_def, input)

        # ── Step 1+4: Schema + Guardrails pre-checks ─────────────
        gr_results = self.guardrails.run(tool_def, input)
        for gr in gr_results:
            if not gr.passed:
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

        # ── Step 3: Idempotency cache lookup ─────────────────────
        # Skip lookup when ik_key is None (tool opted out of idempotency).
        if ik_key is not None:
            existing_tc = await self.store.find_by_idempotency_key(run_id, EventType.TOOL_COMPLETED, ik_key)
            if existing_tc is not None:
                payload = ToolCompletedPayload.model_validate(existing_tc.payload)
                return ToolExecutionResult(
                    status=ExecutionStatus.IDEMPOTENCY_HIT,
                    tool_call_id=payload.tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    output=payload.output,
                    duration_ms=payload.duration_ms,
                    cached=True,
                )

        # ── Step 5: Confirmation check ───────────────────────────
        if tool_def.requires_confirmation:
            # Use a dummy key for confirmation lookup when ik_key is None
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
                        # Reuse original tool_call_id to maintain trace chain (Issue #6 fix)
                        tool_call_id = req_payload.tool_call_id
                    else:
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
                return ToolExecutionResult(
                    status=ExecutionStatus.CONFIRMATION_NEEDED,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    confirmation_id=confirmation_id,
                )

        # ── Step 6: Write ToolCalled ─────────────────────────────
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
        step7_start = time.monotonic()
        try:
            async def _run() -> Any:
                return await Sandbox.invoke(tool_fn, input, timeout_ms=tool_def.timeout_ms)

            output, retry_count = await RetryRunner.execute_with_retry(
                _run,
                policy=tool_def.retry_policy,
            )
            duration_ms = int((time.monotonic() - step7_start) * 1000)
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
        except Exception as exc:
            duration_ms = int((time.monotonic() - step7_start) * 1000)
            retryable = bool(
                tool_def.retry_policy
                and any(
                    candidate in str(exc) or candidate in type(exc).__name__
                    for candidate in tool_def.retry_policy.retryable_errors
                )
            )
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

    # ── Helpers ──────────────────────────────────────────────────

    async def _find_confirmation_received(self, run_id: str, confirmation_id: str):
        return await self.store.find_confirmation_by_id(run_id, confirmation_id)
