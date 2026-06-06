"""Agent Loop Scheduler (L3) — drives think → act → observe, auto event writing."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from harness.core.context_manager import ContextManager
from harness.core.fold import RunState, RunStatus, fold_events
from harness.core.logger import agent_logger, guard_logger
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

if TYPE_CHECKING:
    from harness.monitoring.run_monitor import RunMonitor

_agent_log = agent_logger("scheduler")
_guard_log = guard_logger("scheduler")
_sched_iter = agent_logger("scheduler.iter")
_sched_think = agent_logger("scheduler.think")
_sched_act = agent_logger("scheduler.act")
_sched_ctrl = agent_logger("scheduler.control")
_sched_breaker = guard_logger("scheduler.breaker")


@dataclass
class ThinkResult:
    thought: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    token_count: int = 0
    direct_answer: str | None = None


class AgentKernel(ABC):
    """Abstract LLM reasoning kernel — implemented in L4."""

    @abstractmethod
    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
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
        context_manager: ContextManager | None = None,
        monitor: RunMonitor | None = None,
    ):
        self.store = store
        self.executor = executor
        self.kernel = kernel
        self.tool_defs = tool_defs
        self.tool_fns = tool_fns
        self.config = config or SchedulerConfig()
        self.context_manager = context_manager
        self.monitor = monitor
        self._pause_events: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}

    # ── Main loop ──────────────────────────────────────────

    async def run(self, run_id: str, intent: str) -> RunState:
        if run_id in self._running_tasks:
            raise RuntimeError(f"Run '{run_id}' is already running")

        cancel_flag = asyncio.Event()
        self._cancel_flags[run_id] = cancel_flag
        task = asyncio.create_task(self._run_loop(run_id, intent))
        self._running_tasks[run_id] = task
        try:
            result = await task
            return result
        finally:
            self._running_tasks.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)

    async def _run_loop(self, run_id: str, intent: str) -> RunState:
        events = await self.store.get_events(run_id)
        if not events or events[0].event_type != EventType.RUN_STARTED:
            await self.store.append_event(
                run_id,
                EventType.RUN_STARTED,
                RunStartedPayload(intent=intent).model_dump(),
            )
        else:
            if self.context_manager:
                last_cp = self.context_manager.find_resume_seq(events)
                if last_cp > 0:
                    _sched_ctrl.info("Resuming from seq %d (checkpoint)", last_cp)
                else:
                    _sched_ctrl.info("No checkpoint found, starting from beginning")

        consecutive_failures = 0
        _last_iter_time = time.monotonic()

        try:
            for _iteration in range(1, self.config.max_iterations + 1):
                _iter_elapsed = (time.monotonic() - _last_iter_time) * 1000
                if _iteration > 1:
                    _sched_iter.info("[iter %d] ← iteration %d completed in %dms",
                                     _iteration - 1, _iteration - 1, _iter_elapsed)
                _last_iter_time = time.monotonic()

                if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
                    await self._fail(run_id, "Run cancelled by user")
                    return fold_events(await self.store.get_events(run_id))

                _sched_iter.info("[iter %d/%d] Starting iteration", _iteration, self.config.max_iterations)

                events = await self.store.get_events(run_id)
                state = fold_events(events)
                _sched_iter.info("[observe] Read %d events → seq=%d, thoughts=%d, results=%d, feedbacks=%d",
                                 len(events), state.seq, len(state.thought_history),
                                 len(state.tool_results), len(state.feedbacks))

                if self.context_manager:
                    await self.context_manager.maybe_compress(run_id, _iteration, state)
                    await self.context_manager.try_checkpoint(run_id, _iteration, state)

                if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    _sched_iter.info("[terminal] Run is %s, exiting loop", state.status.value)
                    return state

                if state.status == RunStatus.PAUSED:
                    _sched_ctrl.info("[ctrl] Paused, waiting for resume...")
                    event = self._pause_events.setdefault(run_id, asyncio.Event())
                    event.clear()
                    _pause_start = time.monotonic()
                    try:
                        await asyncio.wait_for(event.wait(), timeout=self.config.pause_timeout_ms / 1000.0)
                    except asyncio.TimeoutError:
                        _sched_ctrl.warning("[ctrl] Pause timed out (%dms)", self.config.pause_timeout_ms)
                    finally:
                        event.clear()
                    _pause_ms = (time.monotonic() - _pause_start) * 1000
                    if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
                        await self._fail(run_id, "Run cancelled by user")
                        return fold_events(await self.store.get_events(run_id))
                    _sched_ctrl.info("[ctrl] Resumed after %dms pause", _pause_ms)
                    events = await self.store.get_events(run_id)
                    state = fold_events(events)
                    if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                        return state

                feedback_text: str | None = None
                if self.monitor:
                    feedbacks = state.feedbacks[-5:]
                    if feedbacks:
                        feedback_text = "\n".join(f.feedback_text for f in feedbacks)
                        _sched_think.info("[think] %d feedback(s) injected into context", len(feedbacks))

                _sched_think.info("[think] Calling LLM%s", " with feedback" if feedback_text else "")
                _think_start = time.monotonic()
                think_result = await self.kernel.think(intent, self.tool_defs, state, feedback=feedback_text)
                _think_ms = (time.monotonic() - _think_start) * 1000
                if think_result.tool_name:
                    _sched_think.info("[think] LLM → tool=%s (%dms)", think_result.tool_name, _think_ms)
                else:
                    _sched_think.info("[think] LLM → stop (\"%.80s\", %dms)",
                                      (think_result.thought or "")[:80], _think_ms)
                await self.store.append_event(
                    run_id,
                    EventType.AGENT_THOUGHT,
                    AgentThoughtPayload(
                        thought=think_result.thought,
                        tool_choice=think_result.tool_name,
                        token_count=think_result.token_count,
                    ).model_dump(),
                )

                if think_result.direct_answer:
                    _sched_think.info("[answer] Agent answered directly: \"%.120s\"", think_result.direct_answer)
                    await self.store.append_event(
                        run_id,
                        EventType.RUN_COMPLETED,
                        RunCompletedPayload(
                            result_summary=think_result.direct_answer,
                        ).model_dump(),
                    )
                    return fold_events(await self.store.get_events(run_id))

                if think_result.tool_name is None:
                    _sched_think.info("[stop] Agent chose to stop, writing RunCompleted")
                    await self.store.append_event(
                        run_id,
                        EventType.RUN_COMPLETED,
                        RunCompletedPayload(result_summary=think_result.thought).model_dump(),
                    )
                    return fold_events(await self.store.get_events(run_id))

                tool_def = self._find_tool_def(think_result.tool_name)
                if tool_def is None:
                    _sched_act.error("[act] Unknown tool: '%s'", think_result.tool_name)
                    await self._fail(run_id, f"Unknown tool: '{think_result.tool_name}'")
                    return fold_events(await self.store.get_events(run_id))

                tool_fn = self.tool_fns.get(think_result.tool_name)
                if tool_fn is None:
                    _sched_act.error("[act] No handler registered for tool '%s'", think_result.tool_name)
                    await self._fail(run_id, f"Tool '{think_result.tool_name}' has no handler registered")
                    return fold_events(await self.store.get_events(run_id))

                _sched_act.info("[act] Executing tool '%s' with input=%.150s",
                                think_result.tool_name, str(think_result.tool_input)[:150])
                try:
                    result = await self.executor.execute(
                        run_id,
                        think_result.tool_name,
                        think_result.tool_input or {},
                        tool_def,
                        tool_fn,
                    )
                except Exception as exc:
                    _sched_act.error("[act] Unexpected exception: %s", exc)
                    consecutive_failures += 1
                    _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=exception",
                                           run_id, _iteration, think_result.tool_name,
                                           consecutive_failures, self.config.max_consecutive_failures)
                    if consecutive_failures >= self.config.max_consecutive_failures:
                        _sched_breaker.error("[breaker] TRIP run=%s iter=%d tool=%s reason=exception count=%d",
                                             run_id, _iteration, think_result.tool_name, consecutive_failures)
                        await self._fail(
                            run_id,
                            f"Circuit breaker: {consecutive_failures} consecutive failures",
                        )
                        return fold_events(await self.store.get_events(run_id))
                    continue
                _sched_act.info("[act] Tool '%s' → %s", think_result.tool_name, result.status.value)

                match result.status:
                    case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
                        _sched_breaker.info("[breaker] run=%s iter=%d tool=%s RESET consecutive_failures=0",
                                            run_id, _iteration, think_result.tool_name)
                        consecutive_failures = 0

                    case ExecutionStatus.CONFIRMATION_NEEDED:
                        _sched_act.info("[act] Tool needs human confirmation, pausing")
                        await self.store.append_event(
                            run_id,
                            EventType.RUN_PAUSED,
                            RunPausedPayload(reason="waiting_confirmation").model_dump(),
                        )
                        await self._wait_for_resume(run_id)
                        _sched_act.info("[act] Confirmation received, re-executing tool")
                        result = await self.executor.execute(
                            run_id,
                            think_result.tool_name,
                            think_result.tool_input or {},
                            tool_def,
                            tool_fn,
                        )
                        match result.status:
                            case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
                                _sched_breaker.info("[breaker] run=%s iter=%d tool=%s RESET (after_confirm) consecutive_failures=0",
                                                    run_id, _iteration, think_result.tool_name)
                                consecutive_failures = 0
                            case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                                consecutive_failures += 1
                                _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=confirm_fail",
                                                       run_id, _iteration, think_result.tool_name,
                                                       consecutive_failures,
                                                       self.config.max_consecutive_failures)
                                if consecutive_failures >= self.config.max_consecutive_failures:
                                    _sched_breaker.error("[breaker] TRIP run=%s iter=%d tool=%s reason=confirm_fail count=%d",
                                                         run_id, _iteration, think_result.tool_name, consecutive_failures)
                                    await self._fail(
                                        run_id,
                                        f"Circuit breaker: {consecutive_failures} consecutive failures",
                                    )
                                    return fold_events(await self.store.get_events(run_id))

                    case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                        consecutive_failures += 1
                        _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=%s",
                                               run_id, _iteration, think_result.tool_name,
                                               consecutive_failures,
                                               self.config.max_consecutive_failures, result.status.value)
                        if consecutive_failures >= self.config.max_consecutive_failures:
                            _sched_breaker.error("[breaker] TRIP run=%s iter=%d tool=%s reason=failures_exceeded count=%d",
                                                 run_id, _iteration, think_result.tool_name, consecutive_failures)
                            await self._fail(
                                run_id,
                                f"Circuit breaker: {consecutive_failures} consecutive failures",
                            )
                            return fold_events(await self.store.get_events(run_id))

            _sched_iter.error("[exhaust] Exceeded max iterations (%d)", self.config.max_iterations)
            await self._fail(run_id, f"Exceeded max iterations ({self.config.max_iterations})")
            return fold_events(await self.store.get_events(run_id))
        finally:
            if self.monitor:
                self.monitor.cleanup(run_id)

    # ── Pause / resume / cancel ───────────────────────────

    async def pause(self, run_id: str) -> None:
        events = await self.store.get_events(run_id)
        state = fold_events(events)
        if state.status != RunStatus.RUNNING:
            return
        await self.store.append_event(
            run_id,
            EventType.RUN_PAUSED,
            RunPausedPayload(reason="user_requested").model_dump(),
        )
        # Don't touch _pause_events here. The _run_loop detects PAUSED
        # status at the start of the next iteration and enters the wait
        # via its own inline code. This avoids conflicts with the
        # _wait_for_resume path used by confirmation pauses.

    async def cancel(self, run_id: str) -> None:
        flag = self._cancel_flags.get(run_id)
        if flag:
            flag.set()
        event = self._pause_events.get(run_id)
        if event:
            event.set()

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
            _sched_ctrl.warning("Confirmation timed out (%dms)", self.config.pause_timeout_ms)
            if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
                return
            await self._fail(run_id, "Confirmation timed out")
        finally:
            event.clear()

    def is_active(self, run_id: str) -> bool:
        return run_id in self._running_tasks

    def is_paused(self, run_id: str) -> bool:
        event = self._pause_events.get(run_id)
        return event is not None and event.is_set() is False

    async def _fail(self, run_id: str, error: str) -> None:
        events = await self.store.get_events(run_id)
        await self.store.append_event(
            run_id,
            EventType.RUN_FAILED,
            RunFailedPayload(final_error=error, event_count=len(events)).model_dump(),
        )
