"""Base scheduler infrastructure (L3) — shared lifecycle, pause/resume/cancel/fail."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from harness.core.context_manager import ContextManager
from harness.core.fold import RunState, RunStatus, fold_events
from harness.core.logger import agent_logger, fmtkv, guard_logger
from harness.execution.base import ExecutionBackend
from harness.models.events import (
    Event,
    EventType,
    FeedbackCategory,
    FeedbackSource,
    LateEventRejectedPayload,
    PhaseTimedOutPayload,
    RunCommandPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunPausedPayload,
    RunResumedPayload,
    RunStartedPayload,
    TaskCleanupTimeoutPayload,
)
from harness.models.tools import ToolDefinition
from harness.models.workspace import Workspace
from harness.monitoring.langfuse_tracer import (
    _get_current_trace_ctx,
    reset_trace_context,
    set_trace_context,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ExecutionStatus, ToolExecutor

if TYPE_CHECKING:
    from harness.monitoring.langfuse_tracer import LangfuseTracer, TraceContext
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
    max_revise_retries: int = 2
    """v2.2 (E, U1): 退化修订守卫的重试上限 — 拒绝"重复已失败动作"的修订并重试的次数。"""
    pause_timeout_ms: int = 300_000
    confirm_timeout_ms: int = 0
    max_confirm_retries: int = 10
    """Max RE-TRIES after the initial confirmation attempt (total = 1 + N)."""
    run_timeout_ms: int = 0
    """Q-07: Run 全局总预算（唯一 deadline，毫秒）。

    0 = 禁用。>0 时整个 Run（plan/revise/tool/answer/classify + pause/confirm
    等待）共享这一个 deadline；各阶段使用剩余时间，不再维护独立 phase timeout。
    超时后 watchdog 强制写结构化 RunFailed（"run_timed_out"）。
    """
    cancel_grace_ms: int = 5000
    """S10 (C-03): 取消宽限期 — watchdog/超时/取消时 await 子任务回收的最长等待。
    宽限期后仍 pending → 写结构化 TASK_CLEANUP_TIMEOUT 并强制 cleanup，不无限等待。
    清理时间不计入 Run 执行预算。"""

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
        tracer: LangfuseTracer | None = None,
        run_end_cb: Callable[[str], None] | None = None,
        workspace: Workspace | None = None,
        backend: ExecutionBackend | None = None,
    ):
        self.store = store
        self.executor = executor
        self.tool_defs = tool_defs
        self.tool_fns = tool_fns
        self.config = config or SchedulerConfig()
        self.context_manager = context_manager
        self.monitor = monitor
        self.tracer = tracer
        self._run_end_cb = run_end_cb or (lambda rid: None)
        self.workspace = workspace
        self.backend = backend
        self._pause_events: dict[str, asyncio.Event] = {}
        self._confirm_events: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._resume_lock = asyncio.Lock()
        # S10: 子任务注册表（run_id -> set[asyncio.Task]）— watchdog/超时/取消时
        # 统一取消并 await（宽限期），回收不完写结构化告警（C-03）。
        self._phase_tasks: dict[str, set[asyncio.Task]] = {}
        # Q-07: Run 全局 deadline（run_id -> time.monotonic() 绝对时刻）。None/缺省
        # 表示 run_timeout_ms=0（总预算禁用），各阶段无超时预算。
        self._deadlines: dict[str, float] = {}
        # Trusted control-plane bookkeeping: RUN_COMMAND events are enforced by
        # Scheduler infrastructure, never by Agent cooperation.  The value is
        # the 1-based ordinal of the latest processed RUN_COMMAND within a run.
        self._last_processed_command_seq: dict[str, int] = {}

    @abstractmethod
    async def _run_loop(self, run_id: str, intent: str, conversation_context: str = "") -> RunState: ...

    # ── Langfuse run-level trace helpers ────────────────────────────
    # scheduler_mode is overridden by subclasses ("serial" / "planning").

    scheduler_mode: str = "serial"

    def _begin_run_trace(self, run_id: str, intent: str) -> TraceContext | None:
        """Create the Run-level trace and activate it in the current context.

        The active context is propagated via contextvars so deep calls (LLM
        client, Tool executor) can read it. When tracing is disabled this is a
        no-op returning None.
        """
        if self.tracer is None or not self.tracer.enabled:
            return None
        ctx = self.tracer.start_run(run_id, intent, self.scheduler_mode)
        self._trace_tokens = set_trace_context(self.tracer, ctx)
        return ctx

    async def _end_run_trace(self, run_id: str) -> None:
        """End the Run-level trace, writing final status, then flush async."""
        ctx = getattr(self, "_trace_ctx", None)
        if ctx is None:
            return
        try:
            state = await self._refresh_state(run_id)
            self.tracer.end_run(
                ctx,
                state.status.value,
                output=str(state.summary or ""),
                error=state.last_error,
            )
        finally:
            self._trace_ctx = None
            tokens = getattr(self, "_trace_tokens", None)
            if tokens is not None:
                reset_trace_context(tokens)
                self._trace_tokens = None
        await self.tracer.flush_async()

    def _begin_iteration_trace(self, iteration: int) -> TraceContext | None:
        """Create an iteration span under the run trace and activate it."""
        if self.tracer is None or not self.tracer.enabled:
            return None
        trace_ctx = getattr(self, "_trace_ctx", None)
        if trace_ctx is None:
            return None
        iter_ctx = self.tracer.start_iteration(trace_ctx, iteration)
        if iter_ctx is not None:
            self._iter_tokens = set_trace_context(self.tracer, iter_ctx)
        return iter_ctx

    def _end_iteration_trace(self, iter_ctx: TraceContext | None) -> None:
        """End the iteration span and restore the run-level context."""
        if iter_ctx is not None:
            self.tracer.end_iteration(iter_ctx)
        tokens = getattr(self, "_iter_tokens", None)
        if tokens is not None:
            reset_trace_context(tokens)
            self._iter_tokens = None

    def _trace_event(
        self,
        name: str,
        level: str = "DEFAULT",
        metadata: dict | None = None,
    ) -> None:
        """Record an observation event under the current trace context (no-op when disabled)."""
        if self.tracer is None or not self.tracer.enabled:
            return
        ctx = _get_current_trace_ctx()
        if ctx is not None:
            self.tracer.trace_event(ctx, name, level=level, metadata=metadata)

    async def run(self, run_id: str, intent: str, conversation_context: str = "") -> RunState:
        if run_id in self._running_tasks:
            raise RuntimeError(f"Run '{run_id}' is already running")
        cancel_flag = asyncio.Event()
        self._cancel_flags[run_id] = cancel_flag
        self._trace_ctx = self._begin_run_trace(run_id, intent)
        task = asyncio.create_task(self._run_loop(run_id, intent, conversation_context))
        self._running_tasks[run_id] = task
        self._phase_tasks.setdefault(run_id, set()).add(task)
        cleanup_done = False
        # Q-07: Run 全局 deadline — 唯一总预算。各阶段/等待共享剩余时间。
        timeout_s = (self.config.run_timeout_ms / 1000.0) if self.config.run_timeout_ms > 0 else None
        if timeout_s is not None:
            self._deadlines[run_id] = time.monotonic() + timeout_s
        try:
            if timeout_s is None:
                result = await task
            else:
                result = await asyncio.wait_for(task, timeout=timeout_s)
            return result
        except asyncio.TimeoutError:
            _sched_breaker.error(
                "[breaker] Run %s exceeded %dms watchdog — forcing RunFailed (run_timed_out)",
                run_id,
                self.config.run_timeout_ms,
            )
            # S10 (D-06 / C-03): watchdog 触发后取消并回收全部子任务（宽限期）。
            try:
                await self._cancel_and_reap(run_id)
                cleanup_done = True
            except Exception as reap_exc:
                _sched_breaker.exception("[breaker] _cancel_and_reap failed for run=%s: %r", run_id, reap_exc)
            try:
                await self._fail(run_id, f"Run timed out after {self.config.run_timeout_ms}ms (watchdog)")
            except Exception as fail_exc:
                _sched_breaker.exception("[breaker] RunFailed write also failed for run=%s: %r", run_id, fail_exc)
            return await self._refresh_state(run_id)
        except Exception as exc:
            # L3 trusted-component barrier: any exception that escapes the
            # subclass _run_loop (LLM timeouts, unexpected runtime errors,
            # etc.) is converted into a structured RunFailed event so the
            # Event Store always reflects the true run terminal state and
            # the failure never becomes a silent "Task exception was never
            # retrieved" (AGENTS.md §6.1, §3.5).
            _sched_breaker.exception("[breaker] UNHANDLED exception in run=%s: %r", run_id, exc)
            # S10: 非超时异常同样回收子任务，避免资源泄漏。
            try:
                await self._cancel_and_reap(run_id)
                cleanup_done = True
            except Exception as reap_exc:
                _sched_breaker.exception("[breaker] _cancel_and_reap failed for run=%s: %r", run_id, reap_exc)
            try:
                await self._fail(run_id, f"Unhandled scheduler error: {exc!r}")
            except Exception as fail_exc:
                _sched_breaker.exception("[breaker] RunFailed write also failed for run=%s: %r", run_id, fail_exc)
            return await self._refresh_state(run_id)
        finally:
            # External cancellation of BaseScheduler.run() does not enter the
            # TimeoutError/Exception handlers above. Reap registered phase
            # tasks before dropping the registry so cancellation cannot orphan
            # work that the watchdog can no longer see.
            active_tasks = self._phase_tasks.get(run_id, set())
            if not cleanup_done and (task.cancelled() or not task.done() or any(not t.done() for t in active_tasks)):
                try:
                    await self._cancel_and_reap(run_id)
                except Exception as reap_exc:
                    _sched_breaker.exception(
                        "[breaker] external cancellation cleanup failed for run=%s: %r",
                        run_id,
                        reap_exc,
                    )
            self._running_tasks.pop(run_id, None)
            self._cancel_flags.pop(run_id, None)
            self._pause_events.pop(run_id, None)
            self._confirm_events.pop(run_id, None)
            self._last_processed_command_seq.pop(run_id, None)
            self._deadlines.pop(run_id, None)
            if not task.done():
                task.cancel()
            self._phase_tasks.pop(run_id, None)
            # S10 (问题七): pending_calls 硬断言 — cleanup 后应归零（C-03：目标而非阻塞）。
            if self.monitor:
                pending_before = getattr(self.monitor, "_pending_calls", {}).get(run_id, {})
                if pending_before:
                    _sched_breaker.warning(
                        "[breaker] pending_calls=%d before cleanup for run=%s (goal: 0)",
                        len(pending_before),
                        run_id,
                    )
                self.monitor.cleanup(run_id)
            # Evict the run-level conversation_id cache so the in-memory
            # mapping doesn't grow unbounded across runs (P0-04 follow-up).
            self.store.evict_run_to_conv(run_id)
            self._run_end_cb(run_id)
            try:
                await self._end_run_trace(run_id)
            except Exception as trace_exc:
                _sched_breaker.exception("[breaker] Langfuse trace end failed for run=%s: %r", run_id, trace_exc)

    async def _handle_pause(self, run_id: str) -> None:
        exists = run_id in self._pause_events
        _sched_ctrl.info(
            "[ctrl] _handle_pause ENTER for run=%s, event existed=%s, pause_reason=%s",
            run_id,
            exists,
            (await self._refresh_state(run_id)).pause_reason,
        )
        event = self._pause_events.setdefault(run_id, asyncio.Event())
        event.clear()
        wait_s = self.config.pause_timeout_ms / 1000.0
        remaining_s = self._run_remaining_s(run_id)
        if remaining_s is not None:
            wait_s = min(wait_s, remaining_s)
        _sched_ctrl.info(
            "[ctrl] _handle_pause WAITING for run=%s (pause_timeout=%.0fms, remaining=%.0fms)",
            run_id,
            wait_s * 1000,
            (remaining_s * 1000) if remaining_s is not None else -1,
        )
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            _sched_ctrl.warning("[ctrl] _handle_pause TIMEOUT for run=%s (%.0fms)", run_id, wait_s * 1000)
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

    def _get_feedback_text(
        self, state: RunState, *, for_revise: bool = False, since_seq: int | None = None
    ) -> str | None:
        """Get active feedbacks, rendered as structured text for Agent consumption.

        for_revise=True: only high-priority + operator feedbacks (avoid noise for Planner).
        since_seq: only return feedbacks injected after this seq (avoids re-reading consumed feedbacks).
        """
        if not self.monitor:
            _sched_think.debug("No monitor — skipping feedback")
            return None

        all_feedbacks = state.feedbacks
        active = [
            fb
            for fb in all_feedbacks
            if (fb.expires_at_seq is None or state.seq <= fb.expires_at_seq)
            and fb.category != FeedbackCategory.CONDITION_RESOLVED
            and fb.consumed_at_seq is None
        ]

        # Hide feedbacks that have been resolved
        resolved_ids = {
            fb.resolves_feedback_id
            for fb in all_feedbacks
            if fb.category == FeedbackCategory.CONDITION_RESOLVED and fb.resolves_feedback_id
        }
        active = [fb for fb in active if fb.feedback_id not in resolved_ids]

        # Filter by since_seq: only return feedbacks injected after the given seq
        if since_seq is not None:
            active = [fb for fb in active if fb.injected_at_seq is not None and fb.injected_at_seq > since_seq]

        # Sort: operator first, then by priority
        priority_score = {"high": 3, "medium": 2, "low": 1}
        active.sort(
            key=lambda fb: (
                1 if fb.source == FeedbackSource.OPERATOR else 0,
                priority_score.get(fb.priority, 0),
            ),
            reverse=True,
        )

        if for_revise:
            active = [fb for fb in active if fb.priority == "high" or fb.source == FeedbackSource.OPERATOR]

        _sched_think.debug(
            "Feedbacks filtered %s",
            fmtkv(
                total=len(all_feedbacks),
                active=len(active),
                resolved=len(resolved_ids),
                for_revise=for_revise,
            ),
        )

        if not active:
            return None

        fb_ids = [fb.feedback_id for fb in active[:5]]
        priorities = [fb.priority for fb in active[:5]]
        sources = [fb.source.value for fb in active[:5]]
        _sched_think.info(
            "Feedback context built %s",
            fmtkv(
                count=len(active[:5]),
                total_total=len(all_feedbacks),
                feedback_ids=",".join(fb_ids),
                priorities=",".join(priorities),
                sources=",".join(sources),
                for_revise=for_revise,
            ),
        )

        rendered = [self._format_feedback(fb) for fb in active[:5]]
        separator = "\n" + "\u2501" * 30 + "\n"
        return "## Monitoring Feedback\n" + separator.join(rendered)

    async def _refresh_state(self, run_id: str) -> RunState:
        events = await self.store.get_events(run_id)
        return fold_events(events)

    async def _is_run_terminal(self, run_id: str) -> bool:
        """S09 (C-05): 折叠 run 事件流判断是否已终态（受信软检查，非 EventStore 全局硬拒）。"""
        try:
            events = await self.store.get_events(run_id)
        except Exception as exc:
            _sched_breaker.warning("[breaker] _is_run_terminal read failed for run=%s: %s", run_id, exc)
            return False
        if not events:
            return False
        status = fold_events(events).status
        return status in (RunStatus.FAILED, RunStatus.COMPLETED)

    async def _record_late_event_rejection(self, run_id: str, event_type: EventType, reason: str) -> None:
        """S09 (D-06 / L-03): 记录终态后迟到事件拦截的结构化事件。"""
        try:
            events = await self.store.get_events(run_id)
            seq = events[-1].seq if events else 0
        except Exception:
            seq = 0
        try:
            await self.store.append_event(
                run_id,
                EventType.LATE_EVENT_REJECTED,
                LateEventRejectedPayload(seq=seq, event_type=event_type.value, reason=reason).model_dump(),
            )
        except Exception as exc:
            _sched_breaker.warning("[breaker] LATE_EVENT_REJECTED write failed for run=%s: %s", run_id, exc)

    async def _record_phase_timeout(self, run_id: str, phase: str, budget_ms: int) -> None:
        """S10 (问题八): 记录分阶段超时的结构化事件。"""
        try:
            await self.store.append_event(
                run_id,
                EventType.PHASE_TIMED_OUT,
                PhaseTimedOutPayload(phase=phase, budget_ms=budget_ms).model_dump(),
            )
        except Exception as exc:
            _sched_breaker.warning("[breaker] PHASE_TIMED_OUT write failed for run=%s: %s", run_id, exc)

    def _run_remaining_s(self, run_id: str) -> float | None:
        """Q-07: Run 全局 deadline 的剩余秒数；无 deadline 时返回 None（总预算禁用）。"""
        deadline = self._deadlines.get(run_id)
        if deadline is None:
            return None
        return deadline - time.monotonic()

    async def _record_task_cleanup_timeout(self, run_id: str, pending_count: int) -> None:
        """S10 (C-03): 宽限期后仍 pending → 结构化告警 + 强制继续（不无限等待）。"""
        try:
            await self.store.append_event(
                run_id,
                EventType.TASK_CLEANUP_TIMEOUT,
                TaskCleanupTimeoutPayload(
                    pending_count=pending_count,
                    grace_ms=self.config.cancel_grace_ms,
                ).model_dump(),
            )
        except Exception as exc:
            _sched_breaker.warning("[breaker] TASK_CLEANUP_TIMEOUT write failed for run=%s: %s", run_id, exc)

    async def _phase_call(self, run_id: str, phase: str, coro):
        """Q-07: 阶段调用 — 使用 Run 全局 deadline 的剩余时间作为预算。

        无 deadline（run_timeout_ms=0）→ 直接 await（总预算禁用）。
        剩余时间耗尽 → PHASE_TIMED_OUT → 抛 TimeoutError。调用方（run() 的
        watchdog）负责将超时收敛为 RunFailed + 取消流程。
        """
        deadline = self._deadlines.get(run_id)
        if deadline is None:
            return await coro
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            await self._record_phase_timeout(run_id, phase, 0)
            raise asyncio.TimeoutError
        task = asyncio.create_task(coro, name=f"{run_id}:{phase}")
        tasks = self._phase_tasks.setdefault(run_id, set())
        tasks.add(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining_ms / 1000.0)
        except asyncio.TimeoutError:
            _sched_breaker.error(
                "[breaker] Phase '%s' exhausted run budget for run=%s — entering cancellation",
                phase,
                run_id,
            )
            await self._record_phase_timeout(run_id, phase, remaining_ms)
            task.cancel()
            raise
        finally:
            if task.done():
                tasks.discard(task)

    async def _cancel_and_reap(self, run_id: str) -> None:
        """S10 (D-06 / C-03): 取消所有子任务并 await 回收（宽限期）。

        宽限期后仍 pending → 写结构化 TASK_CLEANUP_TIMEOUT 并强制继续（C-03：
        watchdog 不能从"卡在长尾"变成"卡在清理"）。同时显式关闭 backend 资源。
        """
        tasks = self._phase_tasks.get(run_id, set())
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            try:
                _, pending = await asyncio.wait(tasks, timeout=self.config.cancel_grace_ms / 1000.0)
            except Exception as exc:
                _sched_breaker.warning("[breaker] _cancel_and_reap wait failed for run=%s: %s", run_id, exc)
                pending = {t for t in tasks if not t.done()}
            if pending:
                _sched_breaker.error(
                    "[breaker] %d sub-task(s) still pending after %dms grace for run=%s — forcing cleanup",
                    len(pending),
                    self.config.cancel_grace_ms,
                    run_id,
                )
                await self._record_task_cleanup_timeout(run_id, len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.wait(pending, timeout=self.config.cancel_grace_ms / 1000.0)
        # 显式释放执行载体资源（backend 容器/SFTP 等）
        if self.backend is not None:
            try:
                await self.backend.close()
            except Exception as exc:
                _sched_breaker.warning("[breaker] backend.close failed for run=%s: %s", run_id, exc)

    async def _append_run_event(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        allow_after_terminal: bool = False,
    ) -> Event | None:
        """S09: 受信终态守卫包装 — run 已 terminal 后拒绝非收尾事件写入。

        L-03: 仅作用于 run 事件流；workspace/conversation 审计事件经 EventStore
        直写，不受此守卫影响。终态事件（RUN_COMPLETED/RUN_FAILED）由
        ``_complete``/``_fail`` 自身幂等守卫处理，不在此路径。
        """
        if event_type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED):
            # 终态事件同样受守卫：已 terminal 则拒绝（幂等），避免经此路径写重复终态。
            if await self._is_run_terminal(run_id):
                _sched_breaker.warning(
                    "[breaker] Late terminal event rejected for run=%s type=%s (already terminal)",
                    run_id,
                    event_type.value,
                )
                await self._record_late_event_rejection(run_id, event_type, "run already terminal")
                return None
            return await self.store.append_event(run_id, event_type, payload, idempotency_key=idempotency_key)
        if not allow_after_terminal and await self._is_run_terminal(run_id):
            _sched_breaker.warning(
                "[breaker] Late event rejected for run=%s type=%s (already terminal)",
                run_id,
                event_type.value,
            )
            await self._record_late_event_rejection(run_id, event_type, "run already terminal")
            return None
        return await self.store.append_event(run_id, event_type, payload, idempotency_key=idempotency_key)

    async def _refresh_authoritative_state(self, run_id: str) -> RunState:
        """Rebuild final-answer evidence without applying context pruning.

        Context pruning is valid for intermediate Planner prompts, but it must
        never remove facts from the final user-facing execution summary.
        """
        events = await self.store.get_events(run_id)
        compressed_types = {
            EventType.CONTEXT_COMPRESSED,
            EventType.CONTEXT_PRUNED,
            EventType.EPISODE_ARCHIVED,
        }
        authoritative_events = [event for event in events if event.event_type not in compressed_types]
        state = fold_events(authoritative_events)
        current_state = fold_events(events)
        if current_state.summary is not None:
            state.summary = current_state.summary
        return state

    async def _complete(
        self,
        run_id: str,
        summary: str,
        all_normal: bool = True,
        unmet_step_ids: list[str] | None = None,
        completion: Any = None,
    ) -> None:
        # S09 (C-05): 终态幂等守卫 — Run 已 terminal 则直接返回，不重复写终态事件。
        async with self._resume_lock:
            if await self._is_run_terminal(run_id):
                _sched_breaker.warning(
                    "[breaker] _complete ignored for run=%s (already terminal)", run_id
                )
                return
            # v2.2 (D5): RUN_COMPLETED 携带机械达成证据 — 完成门聚合结果。
            # S06 (D-03/D-04): 携带交付契约维度（deliverable_met/status/summary）。
            deliverable_summary = []
            if completion is not None and getattr(completion, "deliverables", None):
                deliverable_summary = [
                    {
                        "contract_id": v.contract_id,
                        "status": v.status,
                        "matched_step_ids": list(v.matched_step_ids),
                    }
                    for v in completion.deliverables
                ]
            await self.store.append_event(
                run_id,
                EventType.RUN_COMPLETED,
                RunCompletedPayload(
                    result_summary=summary,
                    all_normal=all_normal,
                    unmet_step_ids=list(unmet_step_ids or []),
                    deliverable_met=getattr(completion, "deliverable_met", None) if completion else None,
                    deliverable_status=(
                        getattr(completion, "deliverable_status", "unverified") if completion else "unverified"
                    ),
                    deliverable_summary=deliverable_summary,
                ).model_dump(),
            )

    async def _fail(self, run_id: str, error: str) -> None:
        async with self._resume_lock:
            # S09 (C-05): 终态幂等守卫 — 已 terminal 的 Run 拒绝再次写 RunFailed。
            if await self._is_run_terminal(run_id):
                _sched_breaker.warning(
                    "[breaker] _fail ignored for run=%s (already %s)",
                    run_id,
                    (await self._refresh_state(run_id)).status.value,
                )
                return
            events = await self.store.get_events(run_id)
            state = fold_events(events)
            tc = len(state.thought_history)
            tr = len(state.tool_results)
            is_dag = any(e.event_type == EventType.PLAN_CREATED for e in events)
            if is_dag:
                completed = sum(1 for e in events if e.event_type == EventType.DAG_STEP_COMPLETED)
                summary = (
                    f"DAG execution: {completed}/{tr} step(s) completed, {tr} tool call(s). {error}. Task terminated."
                )
            else:
                summary = (
                    f"{tc} thought(s), {tr} tool call(s) executed. {error}. Task terminated."
                    if tc > 0
                    else f"Task failed before execution. {error}. Task terminated."
                )
            user_message = "任务未能完成，请检查任务要求或稍后重试。"
            if "cancel" in error.lower():
                user_message = "任务已取消。"
            elif "confirmation" in error.lower():
                user_message = "任务因未获得必要确认而未能完成。"
            await self.store.append_event(
                run_id,
                EventType.RUN_FAILED,
                RunFailedPayload(
                    final_error=error,
                    event_count=len(events),
                    result_summary=summary,
                    user_facing_message=user_message,
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
                run_id,
                EventType.RUN_PAUSED,
                RunPausedPayload(reason="user_requested").model_dump(),
            )
            _sched_ctrl.info(
                "[ctrl] PAUSE written for run=%s, _pause_events exists=%s, _confirm_events exists=%s",
                run_id,
                run_id in self._pause_events,
                run_id in self._confirm_events,
            )
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
            _sched_ctrl.info(
                "[ctrl] RESUME called for run=%s, folded status=%s, pause_reason=%s, "
                "_pause_events exists=%s, _confirm_events exists=%s",
                run_id,
                state.status.value,
                state.pause_reason,
                run_id in self._pause_events,
                run_id in self._confirm_events,
            )
            if state.status != RunStatus.PAUSED:
                _sched_ctrl.warning("[ctrl] RESUME rejected — run %s is %s, not PAUSED", run_id, state.status.value)
                return False
            seq = state.seq
            await self.store.append_event(
                run_id,
                EventType.RUN_RESUMED,
                RunResumedPayload(resume_from_seq=seq).model_dump(),
            )
            event = self._pause_events.get(run_id)
            if event:
                _sched_ctrl.info("[ctrl] RESUME setting _pause_events for run=%s", run_id)
                event.set()
            else:
                _sched_ctrl.info(
                    "[ctrl] RESUME _pause_events NOT FOUND for run=%s (loop not in _handle_pause yet?)", run_id
                )
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
                run_id,
                EventType.RUN_STARTED,
                RunStartedPayload(intent=intent, intent_raw=intent).model_dump(),
            )
        elif self.context_manager:
            last_cp = self.context_manager.find_resume_seq(events)
            if last_cp > 0:
                _sched_ctrl.info("Resuming from seq %d (checkpoint)", last_cp)

    def _is_cancelled(self, run_id: str) -> bool:
        return self._cancel_flags.get(run_id, asyncio.Event()).is_set()

    async def _run_tool_call(
        self,
        run_id: str,
        think_result: ThinkResult,
        _iteration: int,
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

        _sched_act.info("[act] Executing tool '%s' with input=%s", think_result.tool_name, str(think_result.tool_input))
        try:
            result = await self._phase_call(
                run_id,
                "tool",
                self.executor.execute(
                    run_id,
                    think_result.tool_name,
                    think_result.tool_input or {},
                    tool_def,
                    tool_fn,
                    override_tool_call_id=think_result.tool_call_id,
                    workspace_scope=self.workspace.scope if self.workspace else None,
                    backend=self.backend,
                    workspace_id=self.workspace.workspace_id if self.workspace else None,
                ),
            )
        except asyncio.TimeoutError:
            _sched_breaker.error(
                "[breaker] tool phase timed out for run=%s tool=%s — failing",
                run_id,
                think_result.tool_name,
            )
            await self._fail(run_id, f"Tool phase timed out: {think_result.tool_name}")
            return True, consecutive_failures
        except Exception as exc:
            _sched_act.error("[act] Unexpected exception: %s", exc)
            consecutive_failures += 1
            _sched_breaker.warning(
                "[breaker] run=%s iter=%d tool=%s failures=%d/%d event=exception",
                run_id,
                _iteration,
                think_result.tool_name,
                consecutive_failures,
                self.config.max_consecutive_failures,
            )
            if await self._breaker_tripped(
                run_id, consecutive_failures, _iteration, think_result.tool_name, "exception"
            ):
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
                        _sched_breaker.error(
                            "[breaker] Max confirmation retries (%d) exceeded", self.config.max_confirm_retries
                        )
                        await self._fail(
                            run_id, f"Max confirmation retries ({self.config.max_confirm_retries}) exceeded"
                        )
                        return True, consecutive_failures
                    _sched_ctrl.info(
                        "[ctrl] Confirmation loop: writing RUN_PAUSED for run=%s (attempt %d/%d)",
                        run_id,
                        confirm_retries + 1,
                        self.config.max_confirm_retries,
                    )
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
                    result = await self._phase_call(
                        run_id,
                        "tool",
                        self.executor.execute(
                            run_id,
                            think_result.tool_name,
                            think_result.tool_input or {},
                            tool_def,
                            tool_fn,
                            workspace_scope=self.workspace.scope if self.workspace else None,
                            backend=self.backend,
                            workspace_id=self.workspace.workspace_id if self.workspace else None,
                        ),
                    )
                    if result.status in (ExecutionStatus.COMPLETED, ExecutionStatus.IDEMPOTENCY_HIT):
                        consecutive_failures = 0
                        break
                    if result.status == ExecutionStatus.CONFIRMATION_NEEDED:
                        _sched_act.info(
                            "[act] Still needs confirmation — pausing again (attempt %d/%d)",
                            confirm_retries + 1,
                            self.config.max_confirm_retries,
                        )
                        confirm_retries += 1
                        continue
                    # FAILED | TIMEOUT | GUARDRAIL_BLOCKED
                    consecutive_failures += 1
                    _sched_breaker.warning(
                        "[breaker] run=%s iter=%d tool=%s failures=%d/%d event=confirm_fail",
                        run_id,
                        _iteration,
                        think_result.tool_name,
                        consecutive_failures,
                        self.config.max_consecutive_failures,
                    )
                    if await self._breaker_tripped(
                        run_id, consecutive_failures, _iteration, think_result.tool_name, "confirm_fail"
                    ):
                        return True, consecutive_failures
                    break

            case ExecutionStatus.FAILED | ExecutionStatus.TIMEOUT | ExecutionStatus.GUARDRAIL_BLOCKED:
                consecutive_failures += 1
                _sched_breaker.warning(
                    "[breaker] run=%s iter=%d tool=%s failures=%d/%d event=%s",
                    run_id,
                    _iteration,
                    think_result.tool_name,
                    consecutive_failures,
                    self.config.max_consecutive_failures,
                    result.status.value,
                )
                if await self._breaker_tripped(
                    run_id, consecutive_failures, _iteration, think_result.tool_name, "failures_exceeded"
                ):
                    return True, consecutive_failures

        return False, consecutive_failures

    async def _breaker_tripped(
        self, run_id: str, consecutive_failures: int, _iteration: int, tool_name: str, reason: str
    ) -> bool:
        if consecutive_failures >= self.config.max_consecutive_failures:
            _sched_breaker.error(
                "[breaker] TRIP run=%s iter=%d tool=%s reason=%s count=%d",
                run_id,
                _iteration,
                tool_name,
                reason,
                consecutive_failures,
            )
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
        _sched_ctrl.info(
            "[ctrl] _wait_for_resume ENTER for run=%s (confirmation wait), event existed=%s", run_id, exists
        )
        event = self._confirm_events.setdefault(run_id, asyncio.Event())
        event.clear()
        wait_s = self.config.confirm_timeout_ms / 1000.0
        remaining_s = self._run_remaining_s(run_id)
        if remaining_s is not None:
            wait_s = min(wait_s, remaining_s)
        _sched_ctrl.info(
            "[ctrl] _wait_for_resume WAITING for run=%s (confirm_timeout=%.0fms, remaining=%.0fms)",
            run_id,
            wait_s * 1000,
            (remaining_s * 1000) if remaining_s is not None else -1,
        )
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_s)
        except asyncio.TimeoutError:
            _sched_ctrl.warning(
                "[ctrl] _wait_for_resume TIMEOUT for run=%s (%.0fms)", run_id, wait_s * 1000
            )
            if not self._is_cancelled(run_id):
                await self._fail(run_id, "Confirmation timed out")
        finally:
            event.clear()
        if self._is_cancelled(run_id):
            _sched_ctrl.info("[ctrl] _wait_for_resume CANCELLED for run=%s", run_id)
            return
        _sched_ctrl.info("[ctrl] _wait_for_resume SIGNALLED for run=%s", run_id)
