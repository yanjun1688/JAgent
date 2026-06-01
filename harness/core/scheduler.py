"""Agent Loop Scheduler (L3) — drives think → act → observe, auto event writing."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from harness.core.fold import RunState, RunStatus, fold_events
from harness.models.events import (
    AgentThoughtPayload,
    EventType,
    RunCompletedPayload,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
)
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ExecutionStatus, ToolExecutor

_logger = logging.getLogger(__name__)


@dataclass
class ThinkResult:
    thought: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    token_count: int = 0


class AgentKernel(ABC):
    """Abstract LLM reasoning kernel — implemented in L4."""

    @abstractmethod
    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
    ) -> ThinkResult: ...


@dataclass
class SchedulerConfig:
    max_iterations: int = 50
    max_consecutive_failures: int = 5
    pause_timeout_ms: int = 300_000


class AgentLoopScheduler:
    """Trusted component: drives the think→act→observe→schedule loop.

    All events are written automatically — the agent kernel has no
    direct access to the Event Store.
    """

    def __init__(
        self,
        store: EventStore,
        executor: ToolExecutor,
        kernel: AgentKernel,
        tool_defs: list[ToolDefinition],
        tool_fns: dict[str, Callable[[dict[str, Any]], Any]],
        config: SchedulerConfig | None = None,
    ):
        self.store = store
        self.executor = executor
        self.kernel = kernel
        self.tool_defs = tool_defs
        self.tool_fns = tool_fns
        self.config = config or SchedulerConfig()
        self._pause_events: dict[str, asyncio.Event] = {}

    # ── Main loop ──────────────────────────────────────────

    async def run(self, run_id: str, intent: str) -> RunState:
        await self.store.append_event(
            run_id,
            EventType.RUN_STARTED,
            RunStartedPayload(intent=intent).model_dump(),
        )

        consecutive_failures = 0

        for _iteration in range(1, self.config.max_iterations + 1):
            events = await self.store.get_events(run_id)
            state = fold_events(events)

            if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return state

            # ── THINK ──────────────────────────────────────
            think_result = await self.kernel.think(intent, self.tool_defs, state)
            await self.store.append_event(
                run_id,
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought=think_result.thought,
                    tool_choice=think_result.tool_name,
                    token_count=think_result.token_count,
                ).model_dump(),
            )

            # ── Stop signal ────────────────────────────────
            if think_result.tool_name is None:
                await self.store.append_event(
                    run_id,
                    EventType.RUN_COMPLETED,
                    RunCompletedPayload(result_summary=think_result.thought).model_dump(),
                )
                return fold_events(await self.store.get_events(run_id))

            # ── ACT ────────────────────────────────────────
            tool_def = self._find_tool_def(think_result.tool_name)
            if tool_def is None:
                await self._fail(run_id, f"Unknown tool: '{think_result.tool_name}'")
                return fold_events(await self.store.get_events(run_id))

            tool_fn = self.tool_fns.get(think_result.tool_name)
            if tool_fn is None:
                await self._fail(run_id, f"Tool '{think_result.tool_name}' has no handler registered")
                return fold_events(await self.store.get_events(run_id))

            result = await self.executor.execute(
                run_id,
                think_result.tool_name,
                think_result.tool_input or {},
                tool_def,
                tool_fn,
            )

            # ── SCHEDULE ───────────────────────────────────
            match result.status:
                case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
                    consecutive_failures = 0  # reset on any success

                case ExecutionStatus.CONFIRMATION_NEEDED:
                    await self.store.append_event(
                        run_id,
                        EventType.RUN_PAUSED,
                        RunPausedPayload(reason="waiting_confirmation").model_dump(),
                    )
                    await self._wait_for_resume(run_id)
                    # Re-execute the same tool call — Executor detects ConfirmationReceived
                    # and skips the confirmation step, proceeding directly to execution.
                    result = await self.executor.execute(
                        run_id,
                        think_result.tool_name,
                        think_result.tool_input or {},
                        tool_def,
                        tool_fn,
                    )
                    match result.status:
                        case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
                            consecutive_failures = 0
                        case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                            consecutive_failures += 1
                            if consecutive_failures >= self.config.max_consecutive_failures:
                                await self._fail(
                                    run_id,
                                    f"Circuit breaker: {consecutive_failures} consecutive failures",
                                )
                                return fold_events(await self.store.get_events(run_id))

                case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                    consecutive_failures += 1  # only reset by success (above); pause timeout → _fail() terminates loop
                    if consecutive_failures >= self.config.max_consecutive_failures:
                        await self._fail(
                            run_id,
                            f"Circuit breaker: {consecutive_failures} consecutive failures",
                        )
                        return fold_events(await self.store.get_events(run_id))

        # ── Exceeded max iterations ───────────────────────
        await self._fail(run_id, f"Exceeded max iterations ({self.config.max_iterations})")
        return fold_events(await self.store.get_events(run_id))

    # ── Pause / resume ────────────────────────────────────

    async def resume(self, run_id: str) -> None:
        seq = await self.store.get_latest_seq(run_id)
        await self.store.append_event(
            run_id,
            EventType.RUN_RESUMED,
            RunResumedPayload(resume_from_seq=seq).model_dump(),
        )
        event = self._pause_events.get(run_id)
        if event:
            event.set()

    # ── Internal helpers ──────────────────────────────────

    def _find_tool_def(self, name: str) -> ToolDefinition | None:
        for td in self.tool_defs:
            if td.name == name:
                return td
        return None

    async def _wait_for_resume(self, run_id: str) -> None:
        event = self._pause_events.setdefault(run_id, asyncio.Event())
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=self.config.pause_timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            await self._fail(run_id, "Confirmation timed out")
        finally:
            event.clear()

    async def _fail(self, run_id: str, error: str) -> None:
        events = await self.store.get_events(run_id)
        await self.store.append_event(
            run_id,
            EventType.RUN_FAILED,
            RunFailedPayload(final_error=error, event_count=len(events)).model_dump(),
        )
