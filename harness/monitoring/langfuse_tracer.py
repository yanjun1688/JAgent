"""Langfuse tracing wrapper — non-trusted observability component.

LangfuseTracer is a pure observation layer over the Langfuse Cloud API. It is
read-only with respect to the system: it never changes Agent decisions, tool
execution order, or any trusted-component behaviour. All methods degrade to
no-ops (null-object pattern) when ``LANGFUSE_ENABLED`` is not "true" or when
API keys are missing, so the production behaviour is bit-for-bit identical to
a build without Langfuse.

Trace hierarchy (matching the Agent run lifecycle):
    Run (trace)  ── name "Run {run_id}", deterministic trace_id from run_id
    └── Iteration N (span)          — one per think/act loop iteration
        ├── LLM Call (generation)   — model, input/output, token usage
        └── Tool: {name} (span)     — input/output, status, duration
    Scores attach to the trace by trace_id (deterministic from run_id) so the
    offline evaluation pipeline can write scores for a run independently.

The active TraceContext is propagated to deep call sites (LLM client, Tool
executor) via contextvars — the Scheduler sets it for the run and for each
iteration, and the leaf components read it without any coupling to Langfuse.
"""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from langfuse import Langfuse

from harness.core.logger import monitor_logger

_log_tracer = monitor_logger("monitor.langfuse")

# ── Contextvars — propagate the active tracer + trace context to deep calls ──
# Set by the Scheduler around the run loop and per iteration. Leaf components
# (OpenAILLMClient.chat, ToolExecutor.execute) read them and, when a tracer is
# present and enabled, emit their observations. Defaults to None → no-ops.

_current_tracer: ContextVar["LangfuseTracer | None"] = ContextVar("langfuse_tracer", default=None)
_current_trace_ctx: ContextVar["TraceContext | None"] = ContextVar("langfuse_trace_ctx", default=None)


def _get_current_tracer() -> "LangfuseTracer | None":
    return _current_tracer.get()


def _get_current_trace_ctx() -> "TraceContext | None":
    return _current_trace_ctx.get()


def set_trace_context(tracer: "LangfuseTracer | None", ctx: "TraceContext | None") -> tuple[Token, Token]:
    """Push tracer + active context into the current async context."""
    return (_current_tracer.set(tracer), _current_trace_ctx.set(ctx))


def reset_trace_context(tokens: tuple[Token, Token]) -> None:
    """Pop tracer + active context pushed by set_trace_context()."""
    _current_tracer.reset(tokens[0])
    _current_trace_ctx.reset(tokens[1])


class _NullSpan:
    """Null span — all methods are no-ops, zero overhead."""

    def end(self, **kwargs) -> None:
        pass

    def update(self, **kwargs) -> None:
        pass

    def generation(self, **kwargs) -> _NullSpan:
        return _NullSpan()

    def span(self, **kwargs) -> _NullSpan:
        return _NullSpan()

    def event(self, **kwargs) -> None:
        pass

    def score(self, **kwargs) -> None:
        pass

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(self, *args) -> None:
        pass


@dataclass
class TraceContext:
    """Active trace context — wraps a Langfuse observation, or is a null object.

    When ``enabled`` is False the context is inert: ``span()``/``generation()``
    return a ``_NullSpan`` and ``event()``/``score()`` are no-ops.  Callers do
    not need to branch on ``tracer.enabled`` — the null-object pattern handles
    the disabled path transparently.
    """

    trace_id: str = ""
    enabled: bool = False
    _obs: Any = field(default=None, repr=False)  # underlying LangfuseSpan/Generation
    _client: Any = field(default=None, repr=False)  # Langfuse client (for scoring)

    def span(self, name: str, **kwargs: Any) -> Any:
        """Create a child span under this context's observation."""
        if not self.enabled or self._obs is None:
            return _NullSpan()
        child = self._obs.start_observation(name=name, as_type="span", **kwargs)
        return TraceContext(trace_id=self.trace_id, enabled=True, _obs=child, _client=self._client)

    def generation(self, name: str, **kwargs: Any) -> Any:
        """Create a child generation under this context's observation."""
        if not self.enabled or self._obs is None:
            return _NullSpan()
        child = self._obs.start_observation(name=name, as_type="generation", **kwargs)
        return TraceContext(trace_id=self.trace_id, enabled=True, _obs=child, _client=self._client)

    def event(self, name: str, level: str = "DEFAULT", metadata: dict | None = None) -> None:
        """Record an event (guardrail block, confirmation, etc.) under this observation."""
        if not self.enabled or self._obs is None:
            return
        self._obs.create_event(name=name, level=level, metadata=metadata)

    def score(self, name: str, value: float, comment: str = "") -> None:
        """Attach a score to the whole trace this context belongs to."""
        if not self.enabled or self._client is None:
            return
        self._client.create_score(name=name, value=value, trace_id=self.trace_id, comment=comment or None)

    def end(self, **kwargs: Any) -> None:
        """End the underlying observation (if any)."""
        if self.enabled and self._obs is not None:
            self._obs.end(**kwargs)

    def update(self, **kwargs: Any) -> None:
        """Update the underlying observation (if any)."""
        if self.enabled and self._obs is not None:
            self._obs.update(**kwargs)


class LangfuseTracer:
    """Langfuse tracing façade — controlled by LANGFUSE_* environment variables.

    Environment variables:
        LANGFUSE_ENABLED   — "true" enables the tracer (default: disabled)
        LANGFUSE_PUBLIC_KEY — public API key
        LANGFUSE_SECRET_KEY — secret API key
        LANGFUSE_BASE_URL   — Langfuse host (default: https://cloud.langfuse.com)
    """

    def __init__(self) -> None:
        self._enabled = os.getenv("LANGFUSE_ENABLED", "").lower() == "true"
        self._pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self._sk = os.getenv("LANGFUSE_SECRET_KEY", "")
        self._host = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

        if self._enabled and self._pk and self._sk:
            self._client = Langfuse(
                public_key=self._pk,
                secret_key=self._sk,
                base_url=self._host,
            )
        else:
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ── Run-level lifecycle ─────────────────────────────────────────────────

    def start_run(self, run_id: str, intent: str, scheduler_mode: str) -> TraceContext:
        """Create the Run-level trace with a deterministic trace_id.

        The trace_id is derived from run_id (``jagent:{run_id}`` seed) so the
        offline evaluation pipeline can attach scores to the correct trace.
        """
        if self._client is None:
            return TraceContext()
        trace_id = self._client.create_trace_id(seed=f"jagent:{run_id}")
        root = self._client.start_observation(
            name=f"Run {run_id}",
            as_type="span",
            trace_context={"trace_id": trace_id},
            input=intent,
            metadata={"run_id": run_id, "scheduler_mode": scheduler_mode},
        )
        return TraceContext(trace_id=trace_id, enabled=True, _obs=root, _client=self._client)

    def end_run(self, ctx: TraceContext, status: str, output: str = "", error: str | None = None) -> None:
        """End the trace, writing the final status and summary."""
        if self._client is None or ctx is None or not ctx.enabled or ctx._obs is None:
            return
        ctx._obs.update(output=output, metadata={"status": status, "error": error})
        ctx._obs.end()

    def start_iteration(self, ctx: TraceContext, iteration: int) -> TraceContext | None:
        """Create an iteration span nested under the Run trace."""
        if self._client is None or ctx is None or not ctx.enabled or ctx._obs is None:
            return None
        span = ctx._obs.start_observation(
            name=f"Iteration {iteration}",
            as_type="span",
            metadata={"iteration": iteration},
        )
        return TraceContext(trace_id=ctx.trace_id, enabled=True, _obs=span, _client=self._client)

    def end_iteration(self, iter_ctx: TraceContext | None) -> None:
        """End an iteration span created by start_iteration()."""
        if iter_ctx is not None and iter_ctx.enabled and iter_ctx._obs is not None:
            iter_ctx._obs.end()

    # ── Observation-level spans ─────────────────────────────────────────────

    def trace_llm_generation(
        self,
        ctx: TraceContext,
        model: str,
        messages: list[dict],
        response_content: str,
        tool_calls: list[str],
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: float,
    ) -> None:
        """Record a single LLM call as a generation observation."""
        if self._client is None or ctx is None or not ctx.enabled or ctx._obs is None:
            return
        gen = ctx._obs.start_observation(
            name="LLM Call",
            as_type="generation",
            model=model,
            input=messages,
            output={"content": response_content, "tool_calls": tool_calls},
            usage_details={
                "input": prompt_tokens,
                "output": completion_tokens,
                "total": prompt_tokens + completion_tokens,
            },
            metadata={"duration_ms": duration_ms, "model": model},
        )
        gen.end()

    def trace_tool_execution(
        self,
        ctx: TraceContext,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        status: str,
        duration_ms: int,
        error: str | None = None,
        cached: bool = False,
        retry_attempts: int = 0,
    ) -> None:
        """Record a tool execution as a span observation."""
        if self._client is None or ctx is None or not ctx.enabled or ctx._obs is None:
            return
        if status in ("failed", "timeout"):
            level = "ERROR"
        elif status == "guardrail_blocked":
            level = "WARNING"
        else:
            level = "DEFAULT"
        sp = ctx._obs.start_observation(
            name=f"Tool: {tool_name}",
            as_type="tool",
            input=tool_input,
            output=tool_output,
            metadata={
                "status": status,
                "cached": cached,
                "retry_attempts": retry_attempts,
                "duration_ms": duration_ms,
                "error": error,
            },
            level=level,
        )
        sp.end()

    def trace_event(
        self,
        ctx: TraceContext,
        name: str,
        level: str = "DEFAULT",
        metadata: dict | None = None,
    ) -> None:
        """Record an event (guardrail, confirmation, plan created/revised, etc.)."""
        if self._client is None or ctx is None or not ctx.enabled or ctx._obs is None:
            return
        ctx._obs.create_event(name=name, level=level, metadata=metadata)

    def score(self, ctx: TraceContext, name: str, value: float, comment: str = "") -> None:
        """Attach a score to the run's trace."""
        if self._client is None or ctx is None or not ctx.enabled:
            return
        self._client.create_score(name=name, value=value, trace_id=ctx.trace_id, comment=comment or None)

    def attach_score(self, run_id: str, name: str, value: float, comment: str = "") -> None:
        """Attach a score to a run's trace by run_id (deterministic trace_id).

        Used by the offline evaluation pipeline to score a run without holding
        the run's TraceContext. No-op when tracing is disabled.
        """
        if self._client is None:
            return
        trace_id = self._client.create_trace_id(seed=f"jagent:{run_id}")
        self._client.create_score(name=name, value=value, trace_id=trace_id, comment=comment or None)

    # ── Flush ───────────────────────────────────────────────────────────────

    async def flush_async(self) -> None:
        """Asynchronously flush buffered observations to Langfuse.

        The Langfuse SDK flush() is synchronous — offload it to the thread pool
        so it never blocks the Agent event loop. Bounded by a timeout so slow
        or unavailable networks cannot stall the caller (non-trusted observer).
        """
        if self._client is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self._client.flush), timeout=10.0)
        except asyncio.TimeoutError:
            _log_tracer.warning("Langfuse flush timed out after 10s — dropping buffered observations")


__all__ = ["LangfuseTracer", "TraceContext", "_NullSpan"]
