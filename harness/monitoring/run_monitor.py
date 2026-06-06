"""Trusted monitoring component — subscribes to Event Store, injects feedback.

RunMonitor is a trusted component. It listens to EventStore.on_append callbacks
in real time, detects anomalies (consecutive failures, token overuse), and writes
FeedbackInjected events into the Event Store. The Scheduler pulls these feedbacks
before each THINK step and injects them into the Agent's System Prompt.

The Agent never knows about the monitoring mechanism — feedback appears like
"ambient information" in its perception.
"""

from __future__ import annotations

from typing import Any

from harness.core.logger import monitor_logger
from harness.models.events import Event, EventType, FeedbackInjectedPayload
from harness.storage.event_store import EventStore

_log_observe = monitor_logger("monitor.observe")
_log_anomaly = monitor_logger("monitor.anomaly")
_log_inject = monitor_logger("monitor.inject")


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

        self._pending_calls: dict[str, dict[str, tuple[str, int]]] = {}
        self._last_sig: dict[str, tuple[str, int, int] | None] = {}
        self._repeated_call_count: dict[str, int] = {}
        self._stuck_feedback_sent: dict[str, int] = {}

    def attach(self) -> None:
        """Register this monitor as an EventStore on_append listener."""
        _log_observe.info("Monitor attached")
        self.store.on_append(self._on_event)

    async def _on_event(self, event: Event) -> None:
        """Real-time event handler — called synchronously on each event append."""
        try:
            await self._on_event_impl(event)
        except Exception:
            _log_observe.exception("Monitor handler crashed for %s event", event.event_type)

    async def _on_event_impl(self, event: Event) -> None:
        rid = event.run_id
        _log_observe.debug("Event: seq=%d type=%s", event.seq, event.event_type.value)

        if event.event_type == EventType.TOOL_FAILED:
            count = self._consecutive_failures.get(rid, 0) + 1
            self._consecutive_failures[rid] = count
            if count >= 3 and rid not in self._failure_feedback_sent:
                _log_anomaly.warning("Anomaly: %d consecutive tool failures (threshold=3), injecting high-priority feedback",
                                     count)
                self._failure_feedback_sent.add(rid)
                await self._inject_feedback(
                    rid,
                    "high",
                    "Warning: 3 consecutive tool failures detected. "
                    "Consider checking input parameters or terminating the task.",
                )

        elif event.event_type == EventType.TOOL_CALLED:
            tc_id = event.payload.get("tool_call_id", "")
            t_name = event.payload.get("tool_name", "")
            inp_hash = hash(str(event.payload.get("input", {})))
            self._pending_calls.setdefault(rid, {})[tc_id] = (t_name, inp_hash)

        elif event.event_type == EventType.TOOL_COMPLETED:
            was = self._consecutive_failures.get(rid, 0)
            self._consecutive_failures[rid] = 0
            self._failure_feedback_sent.discard(rid)
            if was > 0:
                _log_anomaly.info("Failure streak reset (was %d)", was)

            tc_id = event.payload.get("tool_call_id", "")
            tc_info = self._pending_calls.get(rid, {}).pop(tc_id, None)
            if tc_info is not None:
                tool_name, inp_hash = tc_info
                out_hash = hash(str(event.payload.get("output")))
                new_sig = (tool_name, inp_hash, out_hash)
                last_sig = self._last_sig.get(rid)
                if new_sig == last_sig:
                    self._repeated_call_count[rid] = self._repeated_call_count.get(rid, 0) + 1
                else:
                    self._repeated_call_count[rid] = 0
                    self._last_sig[rid] = new_sig
                    self._stuck_feedback_sent[rid] = 0
                count = self._repeated_call_count[rid]
                if count >= 3 and count % 3 == 0:
                    sent_cnt = self._stuck_feedback_sent.get(rid, 0)
                    expected = count // 3
                    if sent_cnt < expected:
                        self._stuck_feedback_sent[rid] = expected
                        _log_anomaly.warning(
                            "Anomaly: %d repeated identical tool calls (tool=%s), injecting feedback",
                            count + 1, tool_name,
                        )
                        await self._inject_feedback(
                            rid,
                            "high",
                            f"Warning: repeated same tool call {count + 1} times ({tool_name}). "
                            "Consider changing strategy or terminating.",
                        )

        elif event.event_type == EventType.GUARDRAIL_TRIGGERED:
            count = self._consecutive_failures.get(rid, 0) + 1
            self._consecutive_failures[rid] = count
            if count >= 3 and rid not in self._failure_feedback_sent:
                _log_anomaly.warning("Anomaly: %d consecutive failures (incl. guardrails), injecting high-priority feedback",
                                     count)
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
            _log_anomaly.debug("Token total: ~%d (threshold: %d)", self._token_totals[rid], threshold)
            if self._token_totals[rid] > threshold and rid not in self._token_warning_sent:
                _log_anomaly.warning("Anomaly: ~%d tokens exceeds %d (%.0f%%), injecting medium-priority feedback",
                                     self._token_totals[rid], threshold, self.token_warning_ratio * 100)
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
            _log_inject.info("Injected [%s] feedback: %.60s", priority, feedback_text)
        except Exception:
            _log_inject.exception("Failed to inject feedback for %s", run_id)

    def cleanup(self, run_id: str) -> None:
        """Release per-run state when a run finishes or is cancelled."""
        was_fail = self._consecutive_failures.pop(run_id, None)
        was_tokens = self._token_totals.pop(run_id, None)
        self._pending_calls.pop(run_id, None)
        self._last_sig.pop(run_id, None)
        self._repeated_call_count.pop(run_id, None)
        self._stuck_feedback_sent.pop(run_id, None)
        self._token_warning_sent.discard(run_id)
        self._failure_feedback_sent.discard(run_id)
        _log_observe.debug("Cleaned up run %s (failures=%s, tokens=%s)", run_id, was_fail, was_tokens)
