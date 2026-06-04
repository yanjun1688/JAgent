"""Trusted monitoring component — subscribes to Event Store, injects feedback.

RunMonitor is a trusted component. It listens to EventStore.on_append callbacks
in real time, detects anomalies (consecutive failures, token overuse), and writes
FeedbackInjected events into the Event Store. The Scheduler pulls these feedbacks
before each THINK step and injects them into the Agent's System Prompt.

The Agent never knows about the monitoring mechanism — feedback appears like
"ambient information" in its perception.
"""

from __future__ import annotations

import logging
from typing import Any

from harness.models.events import Event, EventType, FeedbackInjectedPayload
from harness.storage.event_store import EventStore

_logger = logging.getLogger(__name__)


class RunMonitor:
    """Trusted component: monitors Event Store in real time via on_append callback.

    Detection rules (MVP):
      - Consecutive failures: 3+ ToolFailed in a row → high priority feedback
      - Token overuse: estimated tokens exceed max_tokens * warning_ratio → medium priority
    """

    def __init__(
        self,
        store: EventStore,
        max_tokens: int = 5000,
        token_warning_ratio: float = 0.8,
    ) -> None:
        self.store = store
        self.max_tokens = max_tokens
        self.token_warning_ratio = token_warning_ratio

        self._consecutive_failures: dict[str, int] = {}
        self._token_totals: dict[str, int] = {}
        self._token_warning_sent: set[str] = set()
        self._failure_feedback_sent: set[str] = set()

    def attach(self) -> None:
        """Register this monitor as an EventStore on_append listener."""
        self.store.on_append(self._on_event)

    async def _on_event(self, event: Event) -> None:
        """Real-time event handler — called synchronously on each event append.

        Must never throw: any exception propagates back through EventStore.append_event
        and would corrupt the original event write. All failures are logged and swallowed.
        """
        try:
            await self._on_event_impl(event)
        except Exception:
            _logger.exception("RunMonitor._on_event failed for %s (event=%s)", event.run_id, event.event_type)

    async def _on_event_impl(self, event: Event) -> None:
        rid = event.run_id

        if event.event_type == EventType.TOOL_FAILED:
            count = self._consecutive_failures.get(rid, 0) + 1
            self._consecutive_failures[rid] = count
            if count >= 3 and rid not in self._failure_feedback_sent:
                self._failure_feedback_sent.add(rid)
                await self._inject_feedback(
                    rid,
                    "high",
                    "Warning: 3 consecutive tool failures detected. "
                    "Consider checking input parameters or terminating the task.",
                )

        elif event.event_type in (EventType.TOOL_COMPLETED, EventType.TOOL_TIMEOUT):
            self._consecutive_failures[rid] = 0
            self._failure_feedback_sent.discard(rid)

        elif event.event_type == EventType.GUARDRAIL_TRIGGERED:
            count = self._consecutive_failures.get(rid, 0) + 1
            self._consecutive_failures[rid] = count
            if count >= 3 and rid not in self._failure_feedback_sent:
                self._failure_feedback_sent.add(rid)
                await self._inject_feedback(
                    rid,
                    "high",
                    "Warning: 3 consecutive tool failures detected "
                    "(including Guardrail blocks). "
                    "Consider checking input parameters or terminating the task.",
                )

        if event.event_type == EventType.AGENT_THOUGHT:
            thought_text = event.payload.get("thought", "") if isinstance(event.payload, dict) else ""
            tokens = max(1, int(len(thought_text) * 0.25))
            self._token_totals[rid] = self._token_totals.get(rid, 0) + tokens
            threshold = int(self.max_tokens * self.token_warning_ratio)
            if self._token_totals[rid] > threshold and rid not in self._token_warning_sent:
                self._token_warning_sent.add(rid)
                await self._inject_feedback(
                    rid,
                    "medium",
                    f"Token warning: approximately {self._token_totals[rid]} tokens consumed "
                    f"({int(self.token_warning_ratio * 100)}% threshold reached). "
                    "Consider simplifying subsequent steps.",
                )

    async def _inject_feedback(self, run_id: str, priority: str, feedback_text: str) -> None:
        """Write a FeedbackInjected event to EventStore for Scheduler consumption."""
        try:
            payload = FeedbackInjectedPayload(feedback_text=feedback_text, priority=priority)
            await self.store.append_event(run_id, EventType.FEEDBACK_INJECTED, payload.model_dump())
            _logger.info(
                "Feedback injected for %s [%s]: %.60s",
                run_id, priority, feedback_text,
            )
        except Exception:
            _logger.exception("Failed to persist FeedbackInjected event for %s", run_id)

    def cleanup(self, run_id: str) -> None:
        """Release per-run state when a run finishes or is cancelled."""
        self._consecutive_failures.pop(run_id, None)
        self._token_totals.pop(run_id, None)
        self._token_warning_sent.discard(run_id)
        self._failure_feedback_sent.discard(run_id)
