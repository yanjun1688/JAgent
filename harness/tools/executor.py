"""Tool Executor — 8-step execution flow with idempotency, guardrails, confirmation, and retry."""

import asyncio
import contextvars
import json
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import jsonschema

from harness.core.logger import agent_logger, guard_logger
from harness.core.recovery import classify_failure_tier, is_read_only_action
from harness.execution.base import ExecutionBackend
from harness.models.events import (
    ConfirmationReceivedPayload,
    ConfirmationRequestedPayload,
    EventType,
    GuardrailTriggeredPayload,
    OutputBlobStoredPayload,
    ToolCalledPayload,
    ToolCompletedPayload,
    ToolFailedPayload,
    ToolResultType,
    ToolTimeoutPayload,
)
from harness.models.tools import ToolDefinition
from harness.models.workspace import WorkspaceScope
from harness.monitoring.langfuse_tracer import _get_current_trace_ctx, _get_current_tracer
from harness.storage.event_store import EventStore
from harness.tools.base import current_backend
from harness.tools.guardrails import GuardrailRunner
from harness.tools.idempotency import IdempotencyKeyGenerator
from harness.tools.output_store import OutputBlobStore, build_ref_placeholder
from harness.tools.retry import RetryRunner
from harness.tools.sandbox import Sandbox
from harness.tools.semantic import SemanticEvaluator

_guard_log = guard_logger("executor")
_agent_log = agent_logger("executor")
_log_guardrails = guard_logger("executor.guardrails")
_log_idem = guard_logger("executor.idempotency")
_log_confirm = guard_logger("executor.confirm")
_log_sandbox = guard_logger("executor.sandbox")


def _log_summary(value: Any, limit: int = 240) -> str:
    """Bound and redact common secret-bearing fields before logging."""
    if isinstance(value, dict):
        value = {
            key: "<redacted>" if key.lower() in {"authorization", "cookie", "token", "api_key"} else item
            for key, item in value.items()
        }
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return text[:limit]

# Context variable that tool functions can read to discover the current run_id.
# Set by ToolExecutor.execute() before each invocation.
current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_run_id", default="")

# ADR-011: current workspace_id (trusted, injected by executor) — BrowserPool
# builds per-tenant/workspace persistent profile paths from this + tenant ctx.
current_workspace_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_workspace_id", default="")


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
    has_semantic_error: bool = False


class ToolExecutor:
    def __init__(
        self,
        store: EventStore,
        guardrail_runner: GuardrailRunner | None = None,
        output_inline_max_chars: int = 8000,
    ):
        self.store = store
        self.guardrails = guardrail_runner or GuardrailRunner(store=store)
        # v3.4 (F-2): tool outputs larger than this are offloaded to a workspace
        # blob; the event stream keeps only a {summary, ref, sha256, bytes} placeholder.
        self.output_inline_max_chars = output_inline_max_chars

    async def _offload_if_large(
        self,
        run_id: str,
        output: Any,
        *,
        tool_call_id: str,
        tool_name: str,
        step_id: str | None,
        workspace_id: str | None,
        backend: Any,
    ) -> Any:
        """Offload an oversized tool output to a blob; return the event payload value.

        The in-memory ``output`` returned to the current run's downstream steps is
        unchanged (full data stays available within the run); only the value
        persisted to the Event Store is replaced by a compact placeholder. A
        corresponding ``OutputBlobStored`` event records the ref for replay/audit.
        """
        blob_store = OutputBlobStore(backend, inline_max_chars=self.output_inline_max_chars)
        try:
            stored = await blob_store.store(
                output,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                step_id=step_id,
                workspace_id=workspace_id,
            )
        except Exception as exc:  # offload failure must never break tool execution
            _guard_log.warning("[output] blob offload failed (keeping inline output): %s", exc)
            return output
        if stored is None:
            return output
        await self.store.append_event(
            run_id,
            EventType.OUTPUT_BLOB_STORED,
            OutputBlobStoredPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                ref=stored.ref,
                sha256=stored.sha256,
                bytes=stored.bytes,
                summary=stored.summary,
                truncated=stored.truncated,
                step_id=step_id,
                workspace_id=workspace_id,
            ).model_dump(),
        )
        _log_sandbox.info(
            "[output] tool=%s call=%s large output offloaded bytes=%d ref=%s",
            tool_name,
            tool_call_id,
            stored.bytes,
            stored.ref,
        )
        return build_ref_placeholder(stored)

    # ── Langfuse tracing helpers (non-trusted observability, no-op when off) ──

    def _trace_event(self, name: str, level: str = "DEFAULT", metadata: dict | None = None) -> None:
        tracer = _get_current_tracer()
        ctx = _get_current_trace_ctx()
        if tracer is not None and ctx is not None and tracer.enabled:
            try:
                tracer.trace_event(ctx, name, level=level, metadata=metadata)
            except Exception:
                _guard_log.debug("[trace] event failed (ignored)", exc_info=True)

    def _trace_tool(
        self,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        status: str,
        duration_ms: int,
        error: str | None = None,
        cached: bool = False,
        retry_attempts: int = 0,
    ) -> None:
        tracer = _get_current_tracer()
        ctx = _get_current_trace_ctx()
        if tracer is not None and ctx is not None and tracer.enabled:
            try:
                tracer.trace_tool_execution(
                    ctx=ctx,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=tool_output,
                    status=status,
                    duration_ms=duration_ms,
                    error=error,
                    cached=cached,
                    retry_attempts=retry_attempts,
                )
            except Exception:
                _guard_log.debug("[trace] tool trace failed (ignored)", exc_info=True)

    async def execute(
        self,
        run_id: str,
        tool_name: str,
        input: dict[str, Any],
        tool_def: ToolDefinition,
        tool_fn: Callable[[dict[str, Any]], Any],
        *,
        override_tool_call_id: str | None = None,
        step_id: str | None = None,
        workspace_scope: WorkspaceScope | None = None,
        backend: ExecutionBackend | None = None,
        workspace_id: str | None = None,
    ) -> ToolExecutionResult:
        _t0 = time.monotonic()

        # ── Step 0: Generate/Reuse tool_call_id ──────────────────
        tool_call_id = override_tool_call_id or str(uuid.uuid4())
        _guard_log.debug("[setup] tool_call_id=%s", tool_call_id)

        # S02: per-operation contract overrides tool-level attributes
        # (side_effects / confirmation / idempotency fields) when present.
        op_contract = tool_def.resolve_operation(input)

        # ── Step 2: Compute idempotency key ──────────────────────
        ik_key = IdempotencyKeyGenerator.compute(
            tool_def, input, key_fields=op_contract.idempotency_key_fields if op_contract else None
        )
        _log_idem.debug("[idem] ik=%s", ik_key or "null")

        # S11 (问题十 2): 工具调用结构化日志 — tool/operation/ik/状态
        _log_sandbox.info(
            "[tool] run=%s tool=%s op=%s ik=%s status=%s input_summary=%s",
            run_id,
            tool_name,
            (
                op_contract.operation
                if op_contract
                else input.get("operation") or input.get("method") or input.get("action")
            ),
            ik_key or "none",
            "started",
            _log_summary(input),
        )

        # ── Step 1+4: Schema + Guardrails pre-checks ─────────────
        _t_gr = time.monotonic()
        gr_results = await self.guardrails.run(
            tool_def,
            input,
            run_id=run_id,
            workspace_scope=workspace_scope,
            backend=backend,
        )
        _ms_gr = (time.monotonic() - _t_gr) * 1000
        guardrail_triggers_confirmation = any(getattr(gr, "triggers_confirmation", False) for gr in gr_results)

        for gr in gr_results:
            if not gr.passed:
                _log_guardrails.warning("[guardrails] Blocked by '%s': %s (%dms)", gr.guardrail_id, gr.reason, _ms_gr)
                await self.store.append_event(
                    run_id,
                    EventType.GUARDRAIL_TRIGGERED,
                    GuardrailTriggeredPayload(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        guardrail_id=gr.guardrail_id,
                        reason=gr.reason,
                        step_id=step_id,
                        workspace_id=workspace_id,
                    ).model_dump(),
                )
                self._trace_event(
                    "guardrail_blocked",
                    level="WARNING",
                    metadata={"guardrail_id": gr.guardrail_id, "reason": gr.reason, "tool": tool_name},
                )
                self._trace_tool(
                    tool_name,
                    input,
                    None,
                    status="guardrail_blocked",
                    duration_ms=int((time.monotonic() - _t0) * 1000),
                    error=gr.reason,
                )
                return ToolExecutionResult(
                    status=ExecutionStatus.GUARDRAIL_BLOCKED,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    guardrail_id=gr.guardrail_id,
                    guardrail_reason=gr.reason,
                )
        _log_guardrails.debug("[guardrails] All %d guardrails passed (%dms)", len(gr_results), _ms_gr)

        # ── Step 3: Idempotency cache lookup ─────────────────────
        if ik_key is not None:
            existing_tc = await self.store.find_by_idempotency_key(run_id, EventType.TOOL_COMPLETED, ik_key)
            if existing_tc is not None:
                payload = ToolCompletedPayload.model_validate(existing_tc.payload)
                se_flag = payload.result_type == ToolResultType.UNSUCCESSFUL
                _log_idem.info(
                    "[idem] Cache HIT (previous result @ seq=%d) semantic=%s error=%s",
                    existing_tc.seq,
                    "UNSUCCESSFUL" if se_flag else "SUCCESS",
                    payload.error or "null",
                )
                self._trace_tool(
                    tool_name,
                    input,
                    payload.output,
                    status="completed",
                    duration_ms=payload.duration_ms,
                    error=payload.error if se_flag else None,
                    cached=True,
                )
                return ToolExecutionResult(
                    status=ExecutionStatus.IDEMPOTENCY_HIT,
                    tool_call_id=payload.tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    output=payload.output,
                    duration_ms=payload.duration_ms,
                    cached=True,
                    has_semantic_error=se_flag,
                    error=payload.error if se_flag else None,
                )
            _log_idem.debug("[idem] Cache miss")

        # ── Step 5: Confirmation check ───────────────────────────
        requires_confirmation = (
            op_contract.requires_confirmation if op_contract is not None else tool_def.requires_confirmation
        )
        needs_confirmation = requires_confirmation or guardrail_triggers_confirmation
        if needs_confirmation:
            _log_confirm.info("[confirm] Tool requires human confirmation")
            self._trace_event(
                "confirmation_needed",
                metadata={"tool": tool_name, "requires_confirmation": requires_confirmation},
            )
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
                                step_id=step_id,
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
                    _log_confirm.info(
                        "[confirm] Waiting for operator (confirmation_id=%s)", req_payload.confirmation_id
                    )
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
                    step_id=step_id,
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
                step_id=step_id,
            ).model_dump(),
        )

        # ── Step 7: Execute via Sandbox with RetryRunner ─────────
        _log_sandbox.info("[sandbox] Executing (timeout=%dms)...", tool_def.timeout_ms)
        step7_start = time.monotonic()
        token_run = current_run_id.set(run_id)
        # ADR-010 D-03: run 级 backend 经 contextvar 注入 invoker，替代按工具名
        # partial 特判（file_op）——所有工具统一 Sandbox.invoke(tool_fn, input)。
        token_backend = current_backend.set(backend)
        token_workspace = current_workspace_id.set(workspace_id or "")
        try:

            async def _run() -> Any:
                return await Sandbox.invoke(tool_fn, input, timeout_ms=tool_def.timeout_ms)

            # F-5 (v3.4): retries cover BOTH raised exceptions (legacy) and
            # retryable *semantic* failures (infra-transient success:false /
            # 5xx on a read-only op). The predicate decides, per returned
            # result, whether it is an auto-retryable semantic failure;
            # RetryRunner shares one retry_policy budget across both failure
            # kinds. A business (non-transient) or side-effecting UNSUCCESSFUL
            # returns None → not retried → escalates exactly as before.
            def _semantic_retry_reason(out: Any) -> str | None:
                rt, err = SemanticEvaluator.evaluate(out, tool_def)
                if rt != ToolResultType.UNSUCCESSFUL or not err:
                    return None
                if classify_failure_tier(err, retryable=False) != "tool_retry":
                    return None
                rp = tool_def.retry_policy
                if rp and rp.retryable_errors and not any(c in err for c in rp.retryable_errors):
                    return None
                if not is_read_only_action(tool_name, input or {}):
                    return None
                return err

            output, retry_count = await RetryRunner.execute_with_retry(
                _run,
                policy=tool_def.retry_policy,
                semantic_retry_check=_semantic_retry_reason,
            )
            duration_ms = int((time.monotonic() - step7_start) * 1000)

            # ── Step 7.5: Semantic evaluation ────────────────────
            result_type, semantic_error = SemanticEvaluator.evaluate(output, tool_def)
            if result_type == ToolResultType.UNSUCCESSFUL:
                _log_sandbox.warning(
                    "[semantic] tool=%s UNSUCCESSFUL (retries=%d): %s (%dms)",
                    tool_name,
                    retry_count,
                    semantic_error,
                    duration_ms,
                )
                if op_contract is not None and op_contract.side_effects:
                    _log_sandbox.info(
                        "[sidefx] tool=%s op=%s side_effects=%s",
                        tool_name,
                        op_contract.operation,
                        [s.value for s in op_contract.side_effects],
                    )
                elif op_contract is None and tool_def.side_effects:
                    _log_sandbox.info(
                        "[sidefx] tool=%s side_effects=%s", tool_name, [s.value for s in tool_def.side_effects]
                    )
                tp = ToolCompletedPayload(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    output=output,
                    duration_ms=duration_ms,
                    result_type=ToolResultType.UNSUCCESSFUL,
                    error=semantic_error,
                    step_id=step_id,
                )
                # UNSUCCESSFUL is intentionally NOT written with an idempotency
                # key: only deterministic (SUCCESS) results are cached. If an
                # unsuccessful result were cached, a same-input retry would hit
                # the cache and never actually re-run the tool — silently
                # defeating self-heal (Bug S1.1, AGENTS.md constraint 4).
                await self.store.append_event(run_id, EventType.TOOL_COMPLETED, tp.model_dump())
                _log_sandbox.info(
                    "[semantic] Wrote TOOL_COMPLETED(UNSUCCESSFUL) tool=%s call_id=%s (not idempotency-cached)",
                    tool_name,
                    tool_call_id,
                )
                self._trace_tool(
                    tool_name,
                    input,
                    output,
                    status="completed",
                    duration_ms=duration_ms,
                    error=semantic_error,
                    retry_attempts=retry_count,
                )
                return ToolExecutionResult(
                    status=ExecutionStatus.COMPLETED,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    idempotency_key=ik_key,
                    output=output,
                    duration_ms=duration_ms,
                    retry_attempts=retry_count,
                    has_semantic_error=True,
                    error=semantic_error,
                )

            _log_sandbox.debug(
                "[semantic] tool=%s SUCCESS indicator=%s",
                tool_name,
                tool_def.success_indicator.field if tool_def.success_indicator else "null",
            )
            _log_sandbox.info(
                "[tool] run=%s tool=%s status=%s duration_ms=%d output_summary=%s",
                run_id,
                tool_name,
                "completed",
                duration_ms,
                _log_summary(output),
            )

            if tool_def.output_schema:
                try:
                    jsonschema.validate(instance=output, schema=tool_def.output_schema)
                except jsonschema.ValidationError as exc:
                    if self._structurally_usable(output):
                        _log_sandbox.warning(
                            "[sandbox] Output schema validation failed (%s) but output is"
                            " structurally usable — accepting",
                            exc.message,
                        )
                    else:
                        _log_sandbox.warning(
                            "[sandbox] Output schema validation failed: output=%s (%dms)",
                            type(output).__name__,
                            duration_ms,
                        )
                        tp = ToolFailedPayload(
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            error=(
                                "Output schema validation failed: expected structured"
                                f" data, got {type(output).__name__}"
                            ),
                            retryable=False,
                            step_id=step_id,
                        )
                        await self.store.append_event(run_id, EventType.TOOL_FAILED, tp.model_dump())
                        self._trace_tool(
                            tool_name,
                            input,
                            None,
                            status="failed",
                            duration_ms=duration_ms,
                            error=(
                                "Output schema validation failed: expected structured"
                                f" data, got {type(output).__name__}"
                            ),
                        )
                        return ToolExecutionResult(
                            status=ExecutionStatus.FAILED,
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            idempotency_key=ik_key,
                            error=(
                                "Output schema validation failed: expected structured"
                                f" data, got {type(output).__name__}"
                            ),
                            duration_ms=duration_ms,
                        )

            if op_contract is not None and op_contract.side_effects:
                _log_sandbox.info(
                    "[sidefx] tool=%s op=%s side_effects=%s",
                    tool_name,
                    op_contract.operation,
                    [s.value for s in op_contract.side_effects],
                )
            elif op_contract is None and tool_def.side_effects:
                _log_sandbox.info(
                    "[sidefx] tool=%s side_effects=%s", tool_name, [s.value for s in tool_def.side_effects]
                )

            # v3.4 (F-2): offload oversized output to a blob; persist only the
            # compact placeholder to the Event Store (full body stays in-memory
            # for this run's downstream steps and is retrievable via fetch_output).
            persisted_output = await self._offload_if_large(
                run_id,
                output,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                step_id=step_id,
                workspace_id=workspace_id,
                backend=backend,
            )
            tp = ToolCompletedPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                output=persisted_output,
                duration_ms=duration_ms,
                step_id=step_id,
            )
            await self.store.append_event(run_id, EventType.TOOL_COMPLETED, tp.model_dump(), idempotency_key=ik_key)
            self._trace_tool(
                tool_name,
                input,
                output,
                status="completed",
                duration_ms=duration_ms,
                retry_attempts=retry_count,
            )
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
            _log_sandbox.warning(
                "[tool] run=%s tool=%s status=%s duration_ms=%d output_summary=%s",
                run_id,
                tool_name,
                "timeout",
                duration_ms,
                "<none>",
            )
            tp = ToolTimeoutPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                timeout_ms=tool_def.timeout_ms,
                step_id=step_id,
            )
            await self.store.append_event(run_id, EventType.TOOL_TIMEOUT, tp.model_dump())
            self._trace_tool(
                tool_name,
                input,
                None,
                status="timeout",
                duration_ms=duration_ms,
                error=f"Tool timed out after {duration_ms}ms",
            )
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
            _log_confirm.info(
                "[confirm] Tool '%s' needs confirmation (id=%s) — propagating from inner call",
                exc.tool_name,
                exc.confirmation_id,
            )
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
            _log_sandbox.error(
                "[tool] run=%s tool=%s status=%s duration_ms=%d output_summary=%s error=%s",
                run_id,
                tool_name,
                "failed",
                duration_ms,
                "<none>",
                str(exc)[:240],
            )
            tp = ToolFailedPayload(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                error=str(exc),
                retryable=retryable,
                step_id=step_id,
            )
            await self.store.append_event(run_id, EventType.TOOL_FAILED, tp.model_dump())
            self._trace_tool(
                tool_name,
                input,
                None,
                status="failed",
                duration_ms=duration_ms,
                error=str(exc),
            )
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
            current_run_id.reset(token_run)
            current_backend.reset(token_backend)
            current_workspace_id.reset(token_workspace)

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _structurally_usable(output: Any) -> bool:
        """Return True if output is structurally navigable by downstream steps.

        Dicts and lists pass (navigable via variable resolution / LLM extraction).
        None, bool, str, int, float fail (not navigable).

        bool is explicitly excluded before the dict/list check because
        ``isinstance(False, int)`` is True in Python.
        """
        if output is None:
            return False
        if isinstance(output, bool):
            return False
        if isinstance(output, (dict, list)):
            return True
        return False

    async def _find_confirmation_received(self, run_id: str, confirmation_id: str):
        return await self.store.find_confirmation_by_id(run_id, confirmation_id)
