"""Agent Loop Scheduler (L3) — drives think → act → observe, auto event writing."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from harness.core.context_manager import ContextManager
from harness.core.fold import RunState, RunStatus, fold_events
from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger, guard_logger
from harness.core.system_prompt import build_system_prompt
from harness.models.events import (
    AgentThoughtPayload,
    EventType,
    PlanCompletedPayload,
    PlanCreatedPayload,
    PlanRevisedPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
)
from harness.models.plan import DagPlan
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
    ) -> list[ThinkResult]: ...


@dataclass
class SchedulerConfig:
    max_iterations: int = 50
    max_consecutive_failures: int = 5
    pause_timeout_ms: int = 300_000



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
    ):
        self.store = store
        self.executor = executor
        self.tool_defs = tool_defs
        self.tool_fns = tool_fns
        self.config = config or SchedulerConfig()
        self.context_manager = context_manager
        self.monitor = monitor
        self._pause_events: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}

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

    async def _handle_pause(self, run_id: str) -> None:
        _sched_ctrl.info("[ctrl] Paused, waiting for resume...")
        event = self._pause_events.setdefault(run_id, asyncio.Event())
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=self.config.pause_timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            _sched_ctrl.warning("[ctrl] Pause timed out (%dms)", self.config.pause_timeout_ms)
        finally:
            event.clear()
        if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
            return
        _sched_ctrl.info("[ctrl] Resumed")

    def _get_feedback_text(self, state: RunState) -> str | None:
        if not self.monitor:
            return None
        feedbacks = state.feedbacks[-5:]
        if feedbacks:
            return "\n".join(f.feedback_text for f in feedbacks)
        return None

    async def _refresh_state(self, run_id: str) -> RunState:
        events = await self.store.get_events(run_id)
        return fold_events(events)

    async def _complete(self, run_id: str, summary: str) -> None:
        await self.store.append_event(
            run_id, EventType.RUN_COMPLETED,
            RunCompletedPayload(result_summary=summary).model_dump(),
        )

    async def _fail(self, run_id: str, error: str) -> None:
        events = await self.store.get_events(run_id)
        state = fold_events(events)
        tc = len(state.thought_history)
        tr = len(state.tool_results)
        if tc > 0:
            summary = f"{tc} planning round(s), {tr} tool call(s) executed. {error}. Task terminated."
        else:
            summary = f"Task failed before execution. {error}. Task terminated."
        await self.store.append_event(
            run_id, EventType.RUN_FAILED,
            RunFailedPayload(
                final_error=error, event_count=len(events), result_summary=summary,
            ).model_dump(),
        )

    async def pause(self, run_id: str) -> None:
        events = await self.store.get_events(run_id)
        state = fold_events(events)
        if state.status != RunStatus.RUNNING:
            return
        await self.store.append_event(
            run_id, EventType.RUN_PAUSED,
            RunPausedPayload(reason="user_requested").model_dump(),
        )

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
            run_id, EventType.RUN_RESUMED,
            RunResumedPayload(resume_from_seq=seq).model_dump(),
        )
        event = self._pause_events.get(run_id)
        if event:
            event.set()

    def is_active(self, run_id: str) -> bool:
        return run_id in self._running_tasks

    def is_paused(self, run_id: str) -> bool:
        event = self._pause_events.get(run_id)
        return event is not None and event.is_set() is False


class AgentLoopScheduler(BaseScheduler):
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
        super().__init__(store, executor, tool_defs, tool_fns, config, context_manager, monitor)
        self.kernel = kernel

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

                feedback_text = self._get_feedback_text(state)
                if feedback_text:
                    _sched_think.info("[think] %d feedback(s) injected into context",
                                      min(5, len(state.feedbacks)))

                _sched_think.info("[think] Calling LLM%s", " with feedback" if feedback_text else "")
                _think_start = time.monotonic()
                think_results = await self.kernel.think(intent, self.tool_defs, state, feedback=feedback_text)
                _think_ms = (time.monotonic() - _think_start) * 1000

                tool_names = [r.tool_name for r in think_results if r.tool_name]
                if tool_names:
                    _sched_think.info("[think] LLM → %d tool(s): %s (%dms)", len(tool_names), ", ".join(tool_names), _think_ms)
                else:
                    _sched_think.info("[think] LLM → stop (%dms)", _think_ms)

                first = think_results[0]
                await self.store.append_event(
                    run_id,
                    EventType.AGENT_THOUGHT,
                    AgentThoughtPayload(
                        thought=first.thought,
                        tool_choice=first.tool_name,
                        token_count=first.token_count,
                        tool_calls=tool_names or None,
                    ).model_dump(),
                )

                for i, think_result in enumerate(think_results):
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

                    terminated, consecutive_failures = await self._run_tool_call(
                        run_id, think_result, _iteration, consecutive_failures,
                    )
                    if terminated:
                        return fold_events(await self.store.get_events(run_id))
                    state = fold_events(await self.store.get_events(run_id))

            _sched_iter.error("[exhaust] Exceeded max iterations (%d)", self.config.max_iterations)
            await self._fail(run_id, f"Exceeded max iterations ({self.config.max_iterations})")
            return fold_events(await self.store.get_events(run_id))
        finally:
            if self.monitor:
                self.monitor.cleanup(run_id)

    async def _run_tool_call(
        self, run_id: str, think_result: ThinkResult, _iteration: int,
        consecutive_failures: int,
    ) -> tuple[bool, int]:
        """Execute a single tool call. Returns (terminated, new_failure_count)."""
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
            if await self._breaker_tripped(run_id, consecutive_failures, _iteration, think_result.tool_name, "exception"):
                return True, consecutive_failures
            return False, consecutive_failures

        _sched_act.info("[act] Tool '%s' → %s", think_result.tool_name, result.status.value)

        match result.status:
            case ExecutionStatus.COMPLETED | ExecutionStatus.IDEMPOTENCY_HIT:
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
                        consecutive_failures = 0
                    case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                        consecutive_failures += 1
                        _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=confirm_fail",
                                               run_id, _iteration, think_result.tool_name,
                                               consecutive_failures,
                                               self.config.max_consecutive_failures)
                        if await self._breaker_tripped(run_id, consecutive_failures, _iteration, think_result.tool_name, "confirm_fail"):
                            return True, consecutive_failures

            case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                consecutive_failures += 1
                _sched_breaker.warning("[breaker] run=%s iter=%d tool=%s failures=%d/%d event=%s",
                                       run_id, _iteration, think_result.tool_name,
                                       consecutive_failures,
                                       self.config.max_consecutive_failures, result.status.value)
                if await self._breaker_tripped(run_id, consecutive_failures, _iteration, think_result.tool_name, "failures_exceeded"):
                    return True, consecutive_failures

        return False, consecutive_failures

    # ── Internal helpers ──────────────────────────────────

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

    async def _fail(self, run_id: str, error: str) -> None:
        events = await self.store.get_events(run_id)
        state = fold_events(events)
        tc = len(state.thought_history)
        tr = len(state.tool_results)
        if tc > 0:
            summary = f"{tc} thought(s), {tr} tool call(s) executed. {error}. Task terminated."
        else:
            summary = f"Task failed before execution. {error}. Task terminated."
        await self.store.append_event(
            run_id,
            EventType.RUN_FAILED,
            RunFailedPayload(
                final_error=error, event_count=len(events),
                result_summary=summary,
            ).model_dump(),
        )


class _FallbackKernel(AgentKernel):
    """Wraps LLMClient as AgentKernel for fallback from serial AgentLoopScheduler.

    Uses the old TOOL:/ARGS:/ANSWER:/<STOP> format for LLM interaction.
    """

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> list[ThinkResult]:
        from harness.core.agent_kernel import _parse_results
        from harness.models.events import EpisodeSummary

        system_prompt = build_system_prompt(intent, tool_defs)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        if feedback:
            messages.append({"role": "system", "content": f"## Monitoring Feedback\n{feedback}"})

        if state.summary:
            if isinstance(state.summary, EpisodeSummary):
                parts = []
                if state.summary.key_decisions:
                    parts.append(f"Key decisions: {', '.join(state.summary.key_decisions)}")
                if state.summary.tools_used:
                    parts.append(f"Tools used: {', '.join(state.summary.tools_used)}")
                if state.summary.key_findings:
                    parts.append(f"Key findings: {', '.join(state.summary.key_findings)}")
                if state.summary.errors_encountered:
                    parts.append(f"Errors: {', '.join(state.summary.errors_encountered)}")
                if state.summary.current_plan:
                    parts.append(f"Current plan: {state.summary.current_plan}")
                summary_text = "\n".join(parts)
            else:
                summary_text = state.summary
            messages.append({"role": "system", "content": f"Previous context summary:\n{summary_text}"})

        window = max(getattr(state, "keep_recent_count", 0), 5)
        timeline: list[tuple[str, Any]] = []
        for t in state.thought_history[-window:]:
            timeline.append(("thought", t))
        for tr in state.tool_results[-window:]:
            timeline.append(("result", tr))
        timeline.sort(key=lambda x: x[1].seq if x[0] == "thought" else x[1].event_seq)

        for kind, item in timeline:
            if kind == "thought":
                choice = f" ({item.tool_choice})" if item.tool_choice else ""
                messages.append({"role": "assistant", "content": f"THOUGHT{choice}: {item.thought}"})
            else:
                content = f"Tool '{item.tool_name}' result ({item.status}): {item.output or item.error}"
                messages.append({"role": "user", "content": content})

        response = await self.client.chat(messages)
        results = _parse_results(response)

        if len(results) == 1 and results[0].tool_name is None and not results[0].direct_answer and ("<STOP>" in response or "ANSWER:" in response):
            result = results[0]
            summary_messages = [
                {"role": "system", "content": "You are a helpful assistant. Summarize the completed task for the user in plain text."},
                *messages[1:],
                {"role": "assistant", "content": response},
                {"role": "user", "content": "The task is now complete. Provide a brief final response summarizing what was accomplished."},
            ]
            try:
                summary = await self.client.chat(summary_messages, max_tokens=512)
                summary = summary.removeprefix("ANSWER:").removeprefix("THOUGHT:").strip()
                results[0] = ThinkResult(thought=result.thought, direct_answer=summary)
            except Exception:
                pass

        return results


class PlanningExecutorScheduler(BaseScheduler):
    """V0.7 Scheduler — Plan → Execute(parallel) → Revise cycle.

    Replaces serial think→act→observe with Planner-Executor + DAG.
    Falls back to the old serial path when Planner fails to produce a valid plan.
    """

    def __init__(
        self,
        store: EventStore,
        executor: ToolExecutor,
        planner: Planner,
        dag_executor: DagExecutor,
        tool_defs: list[ToolDefinition],
        tool_fns: dict[str, Callable[[dict[str, Any]], Any]],
        config: SchedulerConfig | None = None,
        context_manager: ContextManager | None = None,
        monitor: RunMonitor | None = None,
    ):
        super().__init__(store, executor, tool_defs, tool_fns, config, context_manager, monitor)
        self.planner = planner
        self.dag_executor = dag_executor
        self._pending_plan: DagPlan | None = None

    async def _run_loop(self, run_id: str, intent: str) -> RunState:
        events = await self.store.get_events(run_id)
        is_new_run = not events or events[0].event_type != EventType.RUN_STARTED
        if is_new_run:
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

        try:
            state = await self._plan_execute_revise_loop(run_id, intent)
            return state
        finally:
            if self.monitor:
                self.monitor.cleanup(run_id)

    async def _plan_execute_revise_loop(self, run_id: str, intent: str) -> RunState:
        state = await self._refresh_state(run_id)
        consecutive_failures = 0
        self._pending_plan = None

        while state.status == RunStatus.RUNNING:
            if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
                await self._fail(run_id, "Run cancelled by user")
                return await self._refresh_state(run_id)

            if state.status == RunStatus.PAUSED:
                await self._handle_pause(run_id)
                state = await self._refresh_state(run_id)
                continue

            if self._pending_plan is not None:
                plan = self._pending_plan
                self._pending_plan = None
                _sched_think.info("[plan] Using revised plan with %d steps", len(plan.steps))
            else:
                feedback_text = self._get_feedback_text(state)
                _sched_think.info("[plan] Planning for intent: %.80s", intent)
                plan = await self._get_or_fallback(run_id, intent, state, feedback_text)
                if plan is None:
                    return await self._refresh_state(run_id)

                await self.store.append_event(
                    run_id,
                    EventType.AGENT_THOUGHT,
                    AgentThoughtPayload(
                        thought=self.planner.last_raw_response[:500] or f"Plan: {plan.intent[:200]}",
                        tool_choice="plan",
                        token_count=0,
                        tool_calls=[s.tool for s in plan.steps[:5]],
                    ).model_dump(),
                )

                if not plan.steps:
                    _sched_think.info("[plan] Empty plan — generating answer")
                    try:
                        answer = await self._generate_answer(intent, state, feedback_text)
                    except Exception as exc:
                        _sched_think.error("[plan] Answer generation failed: %s", exc)
                        await self._complete(run_id, "Task completed")
                        return await self._refresh_state(run_id)
                    await self.store.append_event(
                        run_id, EventType.AGENT_THOUGHT,
                        AgentThoughtPayload(
                            thought="ANSWER: " + answer,
                            tool_choice=None,
                            token_count=0,
                            tool_calls=None,
                        ).model_dump(),
                    )
                    await self._complete(run_id, answer)
                    return await self._refresh_state(run_id)

                if plan.dynamic:
                    state = await self._execute_dynamic_plan(run_id, intent, plan, state)
                    continue

            state, consecutive_failures = await self._execute_static_plan(run_id, plan, consecutive_failures)
            if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                return state
            if self._pending_plan is not None:
                continue

            if consecutive_failures >= self.config.max_consecutive_failures:
                _sched_breaker.error("[breaker] TRIP — consecutive_failures=%d", consecutive_failures)
                await self._fail(run_id, f"Circuit breaker: {consecutive_failures} consecutive failures")
                return await self._refresh_state(run_id)

            state = await self._refresh_state(run_id)

        return state

    async def _execute_dynamic_plan(
        self, run_id: str, intent: str, plan: DagPlan, state: RunState,
    ) -> RunState:
        _sched_ctrl.info("[plan] Dynamic plan — serial step-by-step execution")
        results = {}
        dyn_plan_id = f"plan_{run_id}_{int(time.time())}"
        layers = plan.topological_sort()
        await self.store.append_event(
            run_id, EventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=dyn_plan_id, intent=plan.intent,
                steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                layer_count=len(layers),
            ).model_dump(),
        )
        for step_idx, step in enumerate(plan.steps):
            if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
                await self._fail(run_id, "Run cancelled by user")
                return await self._refresh_state(run_id)
            if self.context_manager:
                state = await self._refresh_state(run_id)
                await self.context_manager.maybe_compress(run_id, state.seq, state)
                await self.context_manager.try_checkpoint(run_id, state.seq, state)
            step_layers = DagPlan(intent=intent, steps=[step]).topological_sort()
            try:
                ok = await self.dag_executor.execute_layer(
                    run_id, plan, dyn_plan_id, step_layers[0], step_idx, step_layers, results,
                )
            except Exception as exc:
                _sched_act.error("[dynamic] Step %s failed: %s", step.id, exc)
                await self._fail(run_id, f"Dynamic step '{step.id}' failed: {exc}")
                return await self._refresh_state(run_id)
            if not ok:
                _sched_act.error("[dynamic] Step %s had failures — revising", step.id)
                sys_state = self.dag_executor.build_dag_status_text(
                    plan, results, current_layer=step_idx,
                )
                revised = await self.planner.revise(plan, results, sys_state)
                if revised is None:
                    _sched_act.error("[dynamic] Revise failed — terminating")
                    await self._fail(run_id, f"Dynamic step '{step.id}' failed")
                    return await self._refresh_state(run_id)
                await self.store.append_event(
                    run_id, EventType.PLAN_REVISED,
                    PlanRevisedPayload(
                        plan_id=dyn_plan_id, revision_reason="dynamic_step_failure",
                        remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                    ).model_dump(),
                )
                if not revised.steps or revised.dynamic:
                    total_ok = sum(1 for r in results.values() if r.get("status") == "completed")
                    await self.store.append_event(
                        run_id, EventType.PLAN_COMPLETED,
                        PlanCompletedPayload(
                            plan_id=dyn_plan_id, completed_steps=total_ok,
                            total_layers=len(layers), summary="Dynamic task completed after revision",
                        ).model_dump(),
                    )
                    await self._finalize_with_summary(run_id, plan.intent, "Dynamic task completed after revision")
                    return await self._refresh_state(run_id)
                plan = revised
                continue
            sys_state = self.dag_executor.build_dag_status_text(
                plan, results, current_layer=step_idx,
            )
            revised = await self.planner.revise(plan, results, sys_state)
            if revised is None:
                _sched_think.error("[dynamic] Revise failed — terminating")
                await self._fail(run_id, "Planner revise failed during dynamic execution")
                return await self._refresh_state(run_id)
            await self.store.append_event(
                run_id, EventType.PLAN_REVISED,
                PlanRevisedPayload(
                    plan_id=dyn_plan_id, revision_reason="dynamic_step_completed",
                    remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                ).model_dump(),
            )
            if not revised.steps or revised.dynamic:
                total_ok = sum(1 for r in results.values() if r.get("status") == "completed")
                await self.store.append_event(
                    run_id, EventType.PLAN_COMPLETED,
                    PlanCompletedPayload(
                        plan_id=dyn_plan_id, completed_steps=total_ok,
                        total_layers=len(layers), summary="Dynamic task completed",
                    ).model_dump(),
                )
                await self._finalize_with_summary(run_id, plan.intent, "Dynamic task completed")
                return await self._refresh_state(run_id)
            plan = revised
        return await self._refresh_state(run_id)

    async def _execute_static_plan(
        self, run_id: str, plan: DagPlan, consecutive_failures: int,
    ) -> tuple[RunState, int]:
        _sched_iter.info("[execute] Executing DAG plan with %d steps", len(plan.steps))
        layers = plan.topological_sort()
        results: dict[str, Any] = {}
        plan_id = f"plan_{run_id}_{int(time.time())}"
        _sched_iter.info("[plan] PlanCreated %s: %d steps in %d layers",
                         plan_id, len(plan.steps), len(layers))
        await self.store.append_event(
            run_id, EventType.PLAN_CREATED,
            PlanCreatedPayload(
                plan_id=plan_id, intent=plan.intent,
                steps_summary=f"{len(plan.steps)} steps in {len(layers)} layers",
                layer_count=len(layers),
            ).model_dump(),
        )

        for layer_idx, layer in enumerate(layers):
            if self._cancel_flags.get(run_id, asyncio.Event()).is_set():
                await self._fail(run_id, "Run cancelled by user")
                return await self._refresh_state(run_id), consecutive_failures
            if self.context_manager:
                state = await self._refresh_state(run_id)
                await self.context_manager.maybe_compress(run_id, state.seq, state)
                await self.context_manager.try_checkpoint(run_id, state.seq, state)
            try:
                ok = await self.dag_executor.execute_layer(
                    run_id, plan, plan_id, layer, layer_idx, layers, results,
                )
            except Exception as exc:
                _sched_act.error("[execute] DAG execution failed: %s", exc)
                await self._fail(run_id, f"DAG execution failed: {exc}")
                return await self._refresh_state(run_id), consecutive_failures
            if not ok:
                _sched_act.error("[execute] Layer %d had failures — revising", layer_idx)
                sys_state = self.dag_executor.build_dag_status_text(plan, results, current_layer=layer_idx)
                revised = await self.planner.revise(plan, results, sys_state)
                if revised is None:
                    consecutive_failures += 1
                    _sched_think.error("[revise] Revise failed after layer failure, failures=%d/%d", consecutive_failures, self.config.max_consecutive_failures)
                    failed = [(sid, r.get("error", "unknown")) for sid, r in results.items() if r.get("status") != "completed"]
                    error_msg = "; ".join(f"{sid}: {err}" for sid, err in failed) if failed else "unknown error"
                    await self._fail(run_id, f"Steps failed: {error_msg}")
                    return await self._refresh_state(run_id), consecutive_failures
                reason = "step_failure_revised"
                _sched_think.info("[revise] PlanRevised after layer failure — %s", "done" if not revised.steps else f"{len(revised.steps)} steps remaining")
                await self.store.append_event(
                    run_id, EventType.PLAN_REVISED,
                    PlanRevisedPayload(
                        plan_id=plan_id, revision_reason=reason,
                        remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                    ).model_dump(),
                )
                if not revised.steps:
                    _sched_think.info("[revise] Task complete after revision")
                    await self._finalize_with_summary(run_id, plan.intent, "Task completed after revision")
                    return await self._refresh_state(run_id), consecutive_failures
                _sched_think.info("[revise] Continuing with %d remaining steps", len(revised.steps))
                self._pending_plan = revised
                return await self._refresh_state(run_id), consecutive_failures

        total_ok = sum(1 for r in results.values() if r.get("status") == "completed")
        _sched_iter.info("[plan] PlanCompleted %s: %d/%d steps",
                         plan_id, total_ok, len(plan.steps))
        await self.store.append_event(
            run_id, EventType.PLAN_COMPLETED,
            PlanCompletedPayload(
                plan_id=plan_id, completed_steps=total_ok,
                total_layers=len(layers),
                summary=f"Completed {total_ok}/{len(plan.steps)} steps",
            ).model_dump(),
        )
        all_ok = total_ok == len(plan.steps)
        if all_ok:
            consecutive_failures = 0
            _sched_think.info("[revise] All %d steps completed — checking if done", len(plan.steps))
            sys_state = self.dag_executor.build_dag_status_text(plan, results, current_layer=0)
            revised = await self.planner.revise(plan, results, sys_state)
            if revised is None:
                consecutive_failures += 1
                _sched_think.error("[revise] Revise failed, failures=%d/%d", consecutive_failures, self.config.max_consecutive_failures)
                await self._fail(run_id, "Planner revise failed")
                return await self._refresh_state(run_id), consecutive_failures
            _sched_think.info("[revise] PlanRevised — %s", "done" if not revised.steps else f"{len(revised.steps)} steps remaining")
            await self.store.append_event(
                run_id, EventType.PLAN_REVISED,
                PlanRevisedPayload(
                    plan_id=plan_id, revision_reason="dag_execution_completed",
                    remaining_steps_summary=f"{len(revised.steps)} steps remaining" if revised.steps else "task complete",
                ).model_dump(),
            )
            if not revised.steps:
                _sched_think.info("[revise] Task complete")
                await self._finalize_with_summary(run_id, plan.intent, "Task completed successfully")
                return await self._refresh_state(run_id), consecutive_failures
            _sched_think.info("[revise] Continuing with %d remaining steps", len(revised.steps))
            self._pending_plan = revised
            return await self._refresh_state(run_id), consecutive_failures
        else:
            consecutive_failures += 1
            _sched_act.error("[execute] Some steps failed in DAG, failures=%d/%d", consecutive_failures, self.config.max_consecutive_failures)
            failed = [(sid, r.get("error", "unknown")) for sid, r in results.items() if r.get("status") != "completed"]
            error_msg = "; ".join(f"{sid}: {err}" for sid, err in failed)
            await self._fail(run_id, f"Steps failed: {error_msg}")
            return await self._refresh_state(run_id), consecutive_failures

    async def _generate_answer(self, intent: str, state: RunState, feedback: str | None) -> str:
        """Call LLM to generate a conversational answer when no tools are needed."""
        return await self.planner.generate_answer(intent, state, feedback)

    async def _finalize_with_summary(self, run_id: str, intent: str, fallback_summary: str) -> None:
        """Generate a conversational answer before RunCompleted, or use fallback if LLM unavailable."""
        try:
            state = await self._refresh_state(run_id)
            feedback_text = self._get_feedback_text(state)
            answer = await self._generate_answer(intent, state, feedback_text)
            await self.store.append_event(
                run_id, EventType.AGENT_THOUGHT,
                AgentThoughtPayload(
                    thought="ANSWER: " + answer,
                    tool_choice=None, token_count=0, tool_calls=None,
                ).model_dump(),
            )
            await self._complete(run_id, answer)
        except Exception as exc:
            _sched_think.warning("[finalize] Summary generation failed: %s — using fallback", exc)
            await self._complete(run_id, fallback_summary)

    async def _fail(self, run_id: str, error: str) -> None:
        events = await self.store.get_events(run_id)
        state = fold_events(events)
        tc = len(state.thought_history)
        tr = len(state.tool_results)
        if tc > 0:
            summary = f"{tc} execution round(s), {tr} tool call(s) executed. {error}. Task terminated."
        else:
            summary = f"Task failed before execution. {error}. Task terminated."
        await self.store.append_event(
            run_id,
            EventType.RUN_FAILED,
            RunFailedPayload(
                final_error=error, event_count=len(events),
                result_summary=summary,
            ).model_dump(),
        )

    async def _get_or_fallback(
        self, run_id: str, intent: str,
        state: RunState, feedback_text: str | None,
    ) -> DagPlan | None:
        plan = await self.planner.plan(intent, state)
        if plan is not None:
            return plan

        _sched_ctrl.warning("[fallback] Planner failed — falling back to serial AgentLoopScheduler")
        fallback_kernel = _FallbackKernel(self.planner.llm)
        serial = AgentLoopScheduler(
            self.store, self.executor, fallback_kernel,
            self.tool_defs, self.tool_fns, self.config,
            self.context_manager, self.monitor,
        )
        # Fallback scheduler shares the same _cancel_flags via event store:
        # cancel() writes RUN_PAUSED/RUN_FAILED events; AgentLoopScheduler
        # reads them from Event Store on each iteration and acts accordingly.
        # _pause_events are isolated between schedulers — a pause during
        # fallback is detected by state.status check at the loop start.
        run_state = await serial.run(run_id, intent)
        _sched_ctrl.info("[fallback] Serial scheduler completed with status=%s", run_state.status.value)
        return None
