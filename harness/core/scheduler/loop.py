"""AgentLoopScheduler — serial think → act → observe loop (L3)."""

from __future__ import annotations

import time
from typing import Any, Callable

from harness.core.fold import fold_events, RunState, RunStatus
from harness.core.logger import agent_logger, guard_logger
from harness.core.scheduler.base import BaseScheduler, SchedulerConfig, ThinkResult, AgentKernel
from harness.models.events import (
    AgentThoughtPayload,
    EventType,
    RunCompletedPayload,
)
from harness.models.tools import ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor

_sched_iter = agent_logger("scheduler.iter")
_sched_think = agent_logger("scheduler.think")
_sched_ctrl = agent_logger("scheduler.control")
_sched_breaker = guard_logger("scheduler.breaker")


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
        context_manager=None,
        monitor=None,
        run_end_cb: Callable[[str], None] | None = None,
    ):
        super().__init__(store, executor, tool_defs, tool_fns, config, context_manager, monitor, run_end_cb)
        self.kernel = kernel

    async def _run_loop(self, run_id: str, intent: str) -> RunState:
        await self._ensure_run_started(run_id, intent)

        consecutive_failures = 0
        _last_iter_time = time.monotonic()

        for _iteration in range(1, self.config.max_iterations + 1):
            _iter_elapsed = (time.monotonic() - _last_iter_time) * 1000
            if _iteration > 1:
                _sched_iter.info("[iter %d] ← iteration %d completed in %dms",
                                 _iteration - 1, _iteration - 1, _iter_elapsed)
            _last_iter_time = time.monotonic()

            if self._is_cancelled(run_id):
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
                _sched_ctrl.info("[ctrl] Loop detected PAUSED for run=%s, pause_reason=%s, pending_confirmations=%d",
                                 run_id, state.pause_reason, len(state.pending_confirmations))
                await self._handle_pause(run_id)
                events = await self.store.get_events(run_id)
                state = fold_events(events)
                _sched_ctrl.info("[ctrl] Loop after _handle_pause for run=%s, folded status=%s",
                                 run_id, state.status.value)
                if state.status in (RunStatus.COMPLETED, RunStatus.FAILED):
                    return state

            feedback_text = self._get_feedback_text(state)
            if feedback_text:
                _sched_think.info("[think] Feedback injected into LLM context")

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
                    _sched_think.info("[answer] Agent answered directly: \"%s\"", think_result.direct_answer)
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
