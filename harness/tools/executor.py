"""Tool Executor — 8-step execution flow with idempotency, guardrails, and confirmation."""

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
    ) -> ToolExecutionResult:
        # ── Step 0: Generate tool_call_id ────────────────────────
        tool_call_id = str(uuid.uuid4())

        # ── Step 2: Compute idempotency key ──────────────────────
        ik_key = IdempotencyKeyGenerator.compute(tool_def, input)

        # ── Step 1+4: Schema + Guardrails pre-checks ─────────────
        # SchemaGuardrail is the first guardrail inside GuardrailRunner;
        # runs before cache lookup so malformed input fails fast.
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
            existing_req = await self.store.find_by_idempotency_key(run_id, EventType.CONFIRMATION_REQUESTED, ik_key)
            if existing_req is not None:
                req_payload = ConfirmationRequestedPayload.model_validate(existing_req.payload)
                confirmed_event = await self._find_confirmation_received(run_id, req_payload.confirmation_id)
                if confirmed_event is not None:
                    cr_payload = ConfirmationReceivedPayload.model_validate(confirmed_event.payload)
                    if cr_payload.confirmed:
                        pass  # Skip confirmation, continue to execution
                    else:
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
                    idempotency_key=ik_key,
                )
                await self.store.append_event(
                    run_id,
                    EventType.CONFIRMATION_REQUESTED,
                    confirmation_payload.model_dump(),
                    idempotency_key=ik_key,
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

        # ── Step 7: Execute via Sandbox + write completion ──────────
        step7_start = time.monotonic()
        try:
            output = await Sandbox.invoke(tool_fn, input, timeout_ms=tool_def.timeout_ms)
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
            )

    # ── Helpers ──────────────────────────────────────────────────

    async def _find_confirmation_received(self, run_id: str, confirmation_id: str):
        # TODO(L5): 改为 SQL json_extract 查询，避免全量加载事件
        events = await self.store.get_events(run_id)
        for event in events:
            if event.event_type == EventType.CONFIRMATION_RECEIVED:
                payload = ConfirmationReceivedPayload.model_validate(event.payload)
                if payload.confirmation_id == confirmation_id:
                    return event
        return None
