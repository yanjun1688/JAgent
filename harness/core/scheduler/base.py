"""Base scheduler infrastructure (L3) — shared lifecycle, pause/resume/cancel/fail."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from harness.core.context_manager import ContextManager
from harness.core.fold import RunState, RunStatus, fold_events
from harness.core.logger import fmtkv
from harness.core.logger import agent_logger, guard_logger
from harness.models.events import (
    AgentThoughtPayload,
    DagStepCompletedPayload,
    DagStepFailedPayload,
    EpisodeSummary,
    EventType,
    FeedbackCategory,
    FeedbackSource,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanRevisedPayload,
    RunCommandPayload,
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
    tool_call_id: str | None = None


class AgentKernel(ABC):
    """Abstract LLM reasoning kernel — implemented in L4."""

    @abstractmethod
    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> list[ThinkResult]: ...


@dataclass
class SchedulerConfig:
    max_iterations: int = 50
    max_consecutive_failures: int = 5
    pause_timeout_ms: int = 300_000
    confirm_timeout_ms: int = 0
    max_confirm_retries: int = 10
    """Max RE-TRIES after the initial confirmation attempt (total = 1 + N)."""

    def __post_init__(self):
        if self.confirm_timeout_ms == 0:
            self.confirm_timeout_ms = self.pause_timeout_ms



class BaseScheduler(ABC):
    """Shared infrastructure for all scheduler types — pause/resume/cancel/fail lifecycle."""

    def __init__(
        self,
        store: EventStore,
        executor: ToolExecutor,
        tool_defs: list[ToolDefinition],
        tool_fns: dict[str, Callable[[dict[str, Any]], Any]],
        config: SchedulerConfig | None = None,
        context_manager: ContextManager | None = None,
        monitor: RunMonitor | None = None,
        run_end_cb: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.executor = executor
        self.tool_defs = tool_defs
        self.tool_fns = tool_fns
        self.config = config or SchedulerConfig()
        self.context_manager = context_manager
        self.monitor = monitor
        self._run_end_cb = run_end_cb or (lambda rid: None)
        self._pause_events: dict[str, asyncio.Event] = {}
        self._confirm_events: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._resume_lock = asyncio.Lock()
        # Trusted control-plane bookkeeping: RUN_COMMAND events are enforced by
        # Scheduler infrastructure, never by Agent cooperation.  The value is
        # the 1-based ordinal of the latest processed RUN_COMMAND within a run.
        self._last_processed_command_seq: dict[str, int] = {}

    @abstractmethod
    async def _run_loop(self, run_id: str, intent: str) -> RunState: ...

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
            self._pause_events.pop(run_id, None)
            self._confirm_events.pop(run_id, None)
            self._last_processed_command_seq.pop(run_id, None)
            if not task.done():
                task.cancel()
            if self.monitor:
                self.monitor.cleanup(run_id)
            # Evict the run-level conversation_id cache so the in-memory
            # mapping doesn't grow unbounded across runs (P0-04 follow-up).
            self.store.evict_run_to_conv(run_id)
            self._run_end_cb(run_id)

    async def _handle_pause(self, run_id: str) -> None:
        exists = run_id in self._pause_events
        _sched_ctrl.info("[ctrl] _handle_pause ENTER for run=%s, event existed=%s, pause_reason=%s",
                         run_id, exists, (await self._refresh_state(run_id)).pause_reason)
        event = self._pause_events.setdefault(run_id, asyncio.Event())
        event.clear()
        _sched_ctrl.info("[ctrl] _handle_pause WAITING for run=%s (pause_timeout=%dms)",
                         run_id, self.config.pause_timeout_ms)
        try:
            await asyncio.wait_for(event.wait(), timeout=self.config.pause_timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            _sched_ctrl.warning("[ctrl] _handle_pause TIMEOUT for run=%s (%dms)", run_id, self.config.pause_timeout_ms)
        finally:
            event.clear()
        if self._is_cancelled(run_id):
            _sched_ctrl.info("[ctrl] _handle_pause CANCELLED for run=%s", run_id)
            return
        _sched_ctrl.info("[ctrl] _handle_pause RESUMED for run=%s", run_id)

    def _format_feedback(self, fb) -> str:
        """Render a single FeedbackInjectedPayload as structured text."""
        if fb.category == FeedbackCategory.CONDITION_RESOLVED:
            return f"[RESOLVED] {fb.feedback_text}"

        level = "!!" if fb.priority == "high" else "!" if fb.priority == "medium" else ""
        source_tag = "[Operator]" if fb.source == FeedbackSource.OPERATOR else ""
        parts = [f"{level} {source_tag}[{fb.priority.upper()}] {fb.feedback_text}"]

        if fb.affected_tool and fb.error_type:
            parts.append(f"   Tool: {fb.affected_tool}  Error: {fb.error_type}")
        if fb.error_detail:
            parts.append(f"   Detail: {fb.error_detail}")
        if fb.suggestion:
            parts.append(f"   \u2192 {fb.suggestion}")
        return "\n".join(parts)

    def _get_feedback_text(self, state: RunState, *, for_revise: bool = False, since_seq: int | None = None) -> str | None:
        """Get active feedbacks, rendered as structured text for Agent consumption.

        for_revise=True: only high-priority + operator feedbacks (avoid noise for Planner).
        since_seq: only return feedbacks injected after this seq (avoids re-reading consumed feedbacks).
        """
        if not self.monitor:
            _sched_think.debug("No monitor — skipping feedback")
            return None

        all_feedbacks = state.feedbacks
        active = [
            fb for fb in all_feedbacks
            if (fb.expires_at_seq is None or state.seq <= fb.expires_at_seq)
            and fb.category != FeedbackCategory.CONDITION_RESOLVED
            and fb.consumed_at_seq is None
        ]

        # Hide feedbacks that have been resolved
        resolved_ids = {
            fb.resolves_feedback_id for fb in all_feedbacks
            if fb.category == FeedbackCategory.CONDITION_RESOLVED and fb.resolves_feedback_id
        }
        active = [fb for fb in active if fb.feedback_id not in resolved_ids]

        # Filter by since_seq: only return feedbacks injected after the given seq
        if since_seq is not None:
            active = [fb for fb in active if fb.injected_at_seq is not None and fb.injected_at_seq > since_seq]

        # Sort: operator first, then by priority
        priority_score = {"high": 3, "medium": 2, "low": 1}
        active.sort(key=lambda fb: (
            1 if fb.source == FeedbackSource.OPERATOR else 0,
            priority_score.get(fb.priority, 0),
        ), reverse=True)

        if for_revise:
            active = [fb for fb in active if fb.priority == "high" or fb.source == FeedbackSource.OPERATOR]

        _sched_think.debug("Feedbacks filtered %s", fmtkv(
            total=len(all_feedbacks), active=len(active),
            resolved=len(resolved_ids), for_revise=for_revise,
        ))

        if not active:
            return None

        fb_ids = [fb.feedback_id for fb in active[:5]]
        priorities = [fb.priority for fb in active[:5]]
        sources = [fb.source.value for fb in active[:5]]
        _sched_think.info("Feedback context built %s", fmtkv(
            count=len(active[:5]), total_total=len(all_feedbacks),
            feedback_ids=",".join(fb_ids),
            priorities=",".join(priorities),
            sources=",".join(sources),
            for_revise=for_revise,
        ))

        rendered = [self._format_feedback(fb) for fb in active[:5]]
        separator = "\n" + "\u2501" * 30 + "\n"
        return "## Monitoring Feedback\n" + separator.join(rendered)

    async def _refresh_state(self, run_id: str) -> RunState:
        events = await self.store.get_events(run_id)
        return fold_events(events)

    async def _complete(self, run_id: str, summary: str) -> None:
        await self.store.append_event(
            run_id, EventType.RUN_COMPLETED,
            RunCompletedPayload(result_summary=summary).model_dump(),
        )

    async def _fail(self, run_id: str, error: str) -> None:
        async with self._resume_lock:
            events = await self.store.get_events(run_id)
            state = fold_events(events)
            tc = len(state.thought_history)
            tr = len(state.tool_results)
            is_dag = any(e.event_type == EventType.PLAN_CREATED for e in events)
            if is_dag:
                completed = sum(1 for e in events if e.event_type == EventType.DAG_STEP_COMPLETED)
                summary = (
                    f"DAG execution: {completed}/{tr} step(s) completed, {tr} tool call(s). "
                    f"{error}. Task terminated."
                )
            else:
                summary = (
                    f"{tc} thought(s), {tr} tool call(s) executed. {error}. Task terminated."
                    if tc > 0 else f"Task failed before execution. {error}. Task terminated."
                )
            await self.store.append_event(
                run_id, EventType.RUN_FAILED,
                RunFailedPayload(
                    final_error=error, event_count=len(events), result_summary=summary,
                ).model_dump(),
            )
            flag = self._cancel_flags.get(run_id)
            if flag:
                flag.set()
            pevent = self._pause_events.get(run_id)
            if pevent:
                pevent.set()
            cevent = self._confirm_events.get(run_id)
            if cevent:
                cevent.set()
            task = self._running_tasks.get(run_id)
            current = asyncio.current_task()
            if task and not task.done() and task is not current:
                task.cancel()

    async def pause(self, run_id: str) -> bool:
        async with self._resume_lock:
            events = await self.store.get_events(run_id)
            state = fold_events(events)
            if state.status != RunStatus.RUNNING:
                _sched_ctrl.info("[ctrl] PAUSE rejected — run=%s status=%s (not RUNNING)", run_id, state.status.value)
                return False
            await self.store.append_event(
                run_id, EventType.RUN_PAUSED,
                RunPausedPayload(reason="user_requested").model_dump(),
            )
            _sched_ctrl.info("[ctrl] PAUSE written for run=%s, _pause_events exists=%s, _confirm_events exists=%s",
                             run_id, run_id in self._pause_events, run_id in self._confirm_events)
            return True

    async def cancel(self, run_id: str) -> None:
        flag = self._cancel_flags.get(run_id)
        if flag:
            flag.set()
        event = self._pause_events.get(run_id)
        if event:
            event.set()
        cevent = self._confirm_events.get(run_id)
        if cevent:
            cevent.set()

    async def _check_pending_commands(self, run_id: str) -> str | None:
        """Return the latest unprocessed RUN_COMMAND command for a run.

        RUN_COMMAND is a trusted control-plane channel.  Commands are ordered by
        their 1-based ordinal within the run's RUN_COMMAND stream (independent
        of unrelated event types sharing the same run_id).  Store failures are
        contained and reported as "no command" so a transient read error cannot
        crash the scheduler loop.
        """
        try:
            events = await self.store.get_events(run_id)
        except Exception as exc:
            _sched_ctrl.warning("[ctrl] command check failed for run=%s: %s", run_id, exc)
            return None

        last_processed = self._last_processed_command_seq.get(run_id, 0)
        latest: tuple[int, RunCommandPayload] | None = None
        command_seq = 0
        for event in events:
            if event.event_type != EventType.RUN_COMMAND:
                continue
            command_seq += 1
            if command_seq <= last_processed:
                continue
            latest = (command_seq, RunCommandPayload(**event.payload))
        return latest[1].command if latest else None

    async def _process_command(self, run_id: str, command: str) -> bool:
        """Enforce a RUN_COMMAND against scheduler lifecycle state.

        Returns True when the command was recognized and handling completed.
        Unknown commands are ignored and leave the run untouched.
        """
        handled = False
        match command:
            case "hard_abort" | "soft_abort":
                _sched_ctrl.warning("[ctrl] %s received for run=%s — failing run", command, run_id)
                await self._fail(run_id, f"Run aborted by command: {command}")
                handled = True
            case "pause":
                handled = await self.pause(run_id)
            case "resume":
                handled = await self.resume(run_id)
            case "skip_tool":
                # Reserved for tool-level skipping; acknowledged here so the
                # command is consumed rather than repeatedly reprocessed.
                _sched_ctrl.info("[ctrl] skip_tool command acknowledged for run=%s", run_id)
                handled = True
            case _:
                _sched_ctrl.warning("[ctrl] unknown command ignored for run=%s: %s", run_id, command)
                handled = True

        if handled:
            command_seq = await self._latest_command_seq(run_id, command)
            if command_seq > self._last_processed_command_seq.get(run_id, 0):
                self._last_processed_command_seq[run_id] = command_seq
        return handled

    async def _latest_command_seq(self, run_id: str, command: str) -> int:
        """Return the ordinal of the latest RUN_COMMAND matching ``command``."""
        try:
            events = await self.store.get_events(run_id)
        except Exception:
            return self._last_processed_command_seq.get(run_id, 0)
        command_seq = 0
        latest = 0
        for event in events:
            if event.event_type != EventType.RUN_COMMAND:
                continue
            command_seq += 1
            if RunCommandPayload(**event.payload).command == command:
                latest = command_seq
        return latest

    async def _handle_pending_commands(self, run_id: str) -> RunState | None:
        """Process at most one pending command and return refreshed state.

        Returning None means no command was pending; callers should continue
        their normal loop.  Returning a state means a command was enforced and
        the caller must re-evaluate terminal/paused status before proceeding.
        """
        command = await self._check_pending_commands(run_id)
        if command is None:
            return None
        await self._process_command(run_id, command)
        return await self._refresh_state(run_id)

    async def resume(self, run_id: str) -> bool:
        async with self._resume_lock:
            events = await self.store.get_events(run_id)
            state = fold_events(events)
            _sched_ctrl.info("[ctrl] RESUME called for run=%s, folded status=%s, pause_reason=%s, "
                             "_pause_events exists=%s, _confirm_events exists=%s",
                             run_id, state.status.value, state.pause_reason,
                             run_id in self._pause_events, run_id in self._confirm_events)
            if state.status != RunStatus.PAUSED:
                _sched_ctrl.warning("[ctrl] RESUME rejected — run %s is %s, not PAUSED", run_id, state.status.value)
                return False
            seq = state.seq
            await self.store.append_event(
                run_id, EventType.RUN_RESUMED,
                RunResumedPayload(resume_from_seq=seq).model_dump(),
            )
            event = self._pause_events.get(run_id)
            if event:
                _sched_ctrl.info("[ctrl] RESUME setting _pause_events for run=%s", run_id)
                event.set()
            else:
                _sched_ctrl.info("[ctrl] RESUME _pause_events NOT FOUND for run=%s (loop not in _handle_pause yet?)", run_id)
            cevent = self._confirm_events.get(run_id)
            if cevent:
                _sched_ctrl.info("[ctrl] RESUME setting _confirm_events for run=%s", run_id)
                cevent.set()
            else:
                _sched_ctrl.info("[ctrl] RESUME _confirm_events NOT FOUND for run=%s", run_id)
            return True

    def is_active(self, run_id: str) -> bool:
        return run_id in self._running_tasks

    def is_paused(self, run_id: str) -> bool:
        event = self._pause_events.get(run_id)
        return event is not None and event.is_set() is False

    async def _ensure_run_started(self, run_id: str, intent: str) -> None:
        events = await self.store.get_events(run_id)
        if not events or events[0].event_type != EventType.RUN_STARTED:
            await self.store.append_event(
                run_id, EventType.RUN_STARTED,
                RunStartedPayload(intent=intent).model_dump(),
            )
        elif self.context_manager:
            last_cp = self.context_manager.find_resume_seq(events)
            if last_cp > 0:
                _sched_ctrl.info("Resuming from seq %d (checkpoint)", last_cp)

    def _is_cancelled(self, run_id: str) -> bool:
        return self._cancel_flags.get(run_id, asyncio.Event()).is_set()

    async def _run_tool_call(
        self, run_id: str, think_result: ThinkResult, _iteration: int,
        consecutive_failures: int,
    ) -> tuple[bool, int]:
        tool_def = self._find_tool_def(think_result.tool_name)
        if tool_def is None:
            _sched_act.error("[act] Unknown tool: '%s'", think_result.tool_name)
            await self._fail(run_id, f"Unknown tool: '{think_result.tool_name}'")
            return True, consecutive_failures

        tool_fn = self.tool_fns.get(think_result.tool_name)
        if tool_fn is None:
            _sched_act.error("[act] No handler registered for tool '%s'", think_result.tool_name)
            await self._fail(run_id, f"Tool '{think_result.tool_name}' has no handler registered")
            return True, consecutive_failures

        _sched_act.info("[act] Executing tool '%s' with input=%s",
                        think_result.tool_name, str(think_result.tool_input))
        try:
            result = await self.executor.execute(
                run_id,
                think_result.tool_name,
                think_result.tool_input or {},
                tool_def,
                tool_fn,
                override_tool_call_id=think_result.tool_call_id,
            )
        except Exception as exc:
            _sched_act.error("[act] Unexpected exception: %s", exc)
            consecutive_failures += 1
            _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=exception",
                                   run_id, _iteration, think_result.tool_name,
                                   consecutive_failures, self.config.max_consecutive_failures)
            if await self._breaker_tripped(run_id, consecutive_failures, _iteration, think_result.tool_name, "exception"):
                return True, consecutive_failures
            return False, consecutive_failures

        _sched_act.info("[act] Tool '%s' → %s", think_result.tool_name, result.status.value)

        match result.status:
            case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
                consecutive_failures = 0

            case ExecutionStatus.CONFIRMATION_NEEDED:
                _sched_act.info("[act] Tool needs human confirmation, pausing")
                confirm_retries = 0
                while True:
                    if confirm_retries >= self.config.max_confirm_retries:
                        _sched_breaker.error("[breaker] Max confirmation retries (%d) exceeded",
                                             self.config.max_confirm_retries)
                        await self._fail(run_id, f"Max confirmation retries ({self.config.max_confirm_retries}) exceeded")
                        return True, consecutive_failures
                    _sched_ctrl.info("[ctrl] Confirmation loop: writing RUN_PAUSED for run=%s (attempt %d/%d)",
                                     run_id, confirm_retries + 1, self.config.max_confirm_retries)
                    await self.store.append_event(
                        run_id,
                        EventType.RUN_PAUSED,
                        RunPausedPayload(reason="waiting_confirmation").model_dump(),
                    )
                    await self._wait_for_resume(run_id)
                    if self._is_cancelled(run_id):
                        return True, consecutive_failures
                    state = await self._refresh_state(run_id)
                    if state.status in (RunStatus.FAILED, RunStatus.COMPLETED):
                        return True, consecutive_failures
                    _sched_act.info("[act] Confirmation received, re-executing tool")
                    result = await self.executor.execute(
                        run_id,
                        think_result.tool_name,
                        think_result.tool_input or {},
                        tool_def,
                        tool_fn,
                    )
                    if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
                        consecutive_failures = 0
                        break
                    if result.status == ExecutionStatus.CONFIRMATION_NEEDED:
                        _sched_act.info("[act] Still needs confirmation — pausing again (attempt %d/%d)",
                                        confirm_retries + 1, self.config.max_confirm_retries)
                        confirm_retries += 1
                        continue
                    # FAILED | TIMEOUT | GUARDRAIL_BLOCKED
                    consecutive_failures += 1
                    _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=confirm_fail",
                                           run_id, _iteration, think_result.tool_name,
                                           consecutive_failures,
                                           self.config.max_consecutive_failures)
                    if await self._breaker_tripped(run_id, consecutive_failures, _iteration, think_result.tool_name, "confirm_fail"):
                        return True, consecutive_failures
                    break

            case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                consecutive_failures += 1
                _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=%s",
                                       run_id, _iteration, think_result.tool_name,
                                       consecutive_failures,
                                       self.config.max_consecutive_failures, result.status.value)
                if await self._breaker_tripped(run_id, consecutive_failures, _iteration, think_result.tool_name, "failures_exceeded"):
                    return True, consecutive_failures

        return False, consecutive_failures

    async def _breaker_tripped(self, run_id: str, consecutive_failures: int, _iteration: int, tool_name: str, reason: str) -> bool:
        if consecutive_failures >= self.config.max_consecutive_failures:
            _sched_breaker.error("[breaker] TRIP run=%s iter=%d tool=%s reason=%s count=%d",
                                 run_id, _iteration, tool_name, reason, consecutive_failures)
            await self._fail(run_id, f"Circuit breaker: {consecutive_failures} consecutive failures")
            return True
        return False

    def _find_tool_def(self, name: str) -> ToolDefinition | None:
        for td in self.tool_defs:
            if td.name == name:
                return td
        return None

    async def _wait_for_resume(self, run_id: str) -> None:
        exists = run_id in self._confirm_events
        _sched_ctrl.info("[ctrl] _wait_for_resume ENTER for run=%s (confirmation wait), event existed=%s",
                         run_id, exists)
        event = self._confirm_events.setdefault(run_id, asyncio.Event())
        event.clear()
        _sched_ctrl.info("[ctrl] _wait_for_resume WAITING for run=%s (confirm_timeout=%dms)",
                         run_id, self.config.confirm_timeout_ms)
        try:
            await asyncio.wait_for(event.wait(), timeout=self.config.confirm_timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            _sched_ctrl.warning("[ctrl] _wait_for_resume TIMEOUT for run=%s (%dms)", run_id, self.config.confirm_timeout_ms)
            if not self._is_cancelled(run_id):
                await self._fail(run_id, "Confirmation timed out")
        finally:
            event.clear()
        if self._is_cancelled(run_id):
            _sched_ctrl.info("[ctrl] _wait_for_resume CANCELLED for run=%s", run_id)
            return
        _sched_ctrl.info("[ctrl] _wait_for_resume SIGNALLED for run=%s", run_id)
