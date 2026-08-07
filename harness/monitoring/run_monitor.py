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
from urllib.parse import urlparse

from harness.core.fold import fold_events
from harness.core.logger import fmtkv, monitor_logger
from harness.models.events import (
    Event,
    EventType,
    FeedbackCategory,
    FeedbackInjectedPayload,
    FeedbackSource,
)
from harness.storage.event_store import EventStore

_log_observe = monitor_logger("monitor.observe")
_log_anomaly = monitor_logger("monitor.anomaly")
_log_inject = monitor_logger("monitor.inject")


class RunMonitor:
    """Trusted component: monitors Event Store in real time via on_append callback.

    Detection rules:
      - Consecutive failures: 3+ ToolFailed/GuardrailTriggered in a row
      - Token overuse: estimated tokens exceed max_tokens * warning_ratio
      - Repeated identical tool calls: 3+ identical (tool, input_hash, output_hash)

    All feedbacks are written as FeedbackInjected events to EventStore,
    follow the fold → state.feedbacks → Scheduler path.
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
        self._consecutive_per_ep: dict[str, dict[str, int]] = {}
        self._token_totals: dict[str, int] = {}
        self._token_warning_sent: set[str] = set()

        self._pending_calls: dict[str, dict[str, tuple[str, int, str]]] = {}
        self._last_sig: dict[str, tuple[str, int, int] | None] = {}
        self._repeated_call_count: dict[str, int] = {}
        self._stuck_feedback_sent: dict[str, int] = {}

        # V0.6.1: per-tool tracking + error pattern recognition
        self._failures_per_tool: dict[str, dict[str, int]] = {}
        self._failure_error_map: dict[str, dict[str, dict[str, int]]] = {}
        self._captured_ep_key: dict[str, str | None] = {}
        self._fed_ep_keys: dict[str, set[tuple[str, str]]] = {}
        self._last_fail_sig: dict[str, tuple[str, int, str] | None] = {}
        self._repeat_fail_count: dict[str, int] = {}
        self._last_seen_seq: dict[str, int] = {}

    def attach(self) -> None:
        """Register this monitor as an EventStore on_append listener."""
        self.store.on_append(self._on_event)
        _log_observe.info("Monitor attached")

    async def _on_event(self, event: Event) -> None:
        """Real-time event handler — called synchronously on each event append."""
        try:
            await self._on_event_impl(event)
        except Exception:
            _log_observe.exception("Monitor handler crashed for %s event", event.event_type)

    async def _on_event_impl(self, event: Event) -> None:
        rid = event.run_id
        self._last_seen_seq[rid] = event.seq
        _log_observe.debug("Event: seq=%d type=%s", event.seq, event.event_type.value)

        # ── Unified failure tracking (TOOL_FAILED + GUARDRAIL_TRIGGERED + DAG_STEP_FAILED) ──────
        if event.event_type in (EventType.TOOL_FAILED, EventType.GUARDRAIL_TRIGGERED, EventType.DAG_STEP_FAILED):
            tool = event.payload.get("tool_name", "?")
            error = event.payload.get("error", "")
            error_type = self._extract_error_type(error)

            tc_id = event.payload.get("tool_call_id", "")
            tc_info = self._pending_calls.get(rid, {}).get(tc_id)
            ep_key = tc_info[2] if tc_info and len(tc_info) >= 3 else tool
            inp_hash = tc_info[1] if tc_info else hash("")

            per_ep = self._consecutive_per_ep.setdefault(rid, {})
            count = per_ep.get(ep_key, 0) + 1
            per_ep[ep_key] = count
            self._consecutive_failures[rid] = count

            per_tool = self._failures_per_tool.setdefault(rid, {})
            per_tool[tool] = per_tool.get(tool, 0) + 1

            err_map = self._failure_error_map.setdefault(rid, {}).setdefault(tool, {})
            err_map[error_type] = err_map.get(error_type, 0) + 1

            # Same input + same error repeat detection
            new_sig = (ep_key, inp_hash, error_type)
            last_sig = self._last_fail_sig.get(rid)
            if new_sig == last_sig:
                self._repeat_fail_count[rid] = self._repeat_fail_count.get(rid, 0) + 1
            else:
                self._repeat_fail_count[rid] = 0
            self._last_fail_sig[rid] = new_sig

            _log_anomaly.debug("Failure event %s", fmtkv(
                run_id=rid, seq=event.seq, ep_key=ep_key, tool=tool, error_type=error_type,
                consecutive=count, per_tool=per_tool[tool], per_error=err_map[error_type],
            ))

            if count >= 3:
                _log_anomaly.info("Anomaly threshold hit %s", fmtkv(
                    run_id=rid, tool=tool, ep_key=ep_key, error_type=error_type,
                    consecutive=count, event_type=event.event_type.value,
                ))
                await self._check_and_inject_feedback(rid, tool, ep_key, error_type, event.event_type, error_detail=error)

        # ── Repeated identical tool call detection ────────────────────────────
        elif event.event_type == EventType.TOOL_CALLED:
            tc_id = event.payload.get("tool_call_id", "")
            t_name = event.payload.get("tool_name", "")
            input_dict = event.payload.get("input", {}) or {}
            inp_hash = hash(str(input_dict))
            url = input_dict.get("url", "") if isinstance(input_dict, dict) else ""
            ep_key = self._endpoint_key(t_name, url)
            self._pending_calls.setdefault(rid, {})[tc_id] = (t_name, inp_hash, ep_key)

        elif event.event_type == EventType.TOOL_COMPLETED:
            tc_id = event.payload.get("tool_call_id", "")
            tool_name = event.payload.get("tool_name", "")
            tc_info = self._pending_calls.get(rid, {}).pop(tc_id, None)
            ep_key = tc_info[2] if tc_info and len(tc_info) >= 3 else tool_name
            payload_result_type = event.payload.get("result_type", "success")
            was = 0

            if payload_result_type == "soft_error":
                _log_observe.info("Event: seq=%d type=%s result_type=soft_error tool=%s ep_key=%s",
                                  event.seq, event.event_type.value, tool_name, ep_key)
                per_ep = self._consecutive_per_ep.setdefault(rid, {})
                count = per_ep.get(ep_key, 0) + 1
                per_ep[ep_key] = count
                self._consecutive_failures[rid] = count

                err_text = event.payload.get("error", "soft_error")
                error_type = self._extract_error_type(err_text)
                per_tool = self._failures_per_tool.setdefault(rid, {})
                per_tool[tool_name] = per_tool.get(tool_name, 0) + 1
                err_map = self._failure_error_map.setdefault(rid, {}).setdefault(tool_name, {})
                err_map[error_type] = err_map.get(error_type, 0) + 1

                _log_anomaly.debug("SOFT_ERROR %s", fmtkv(
                    run_id=rid, seq=event.seq, ep_key=ep_key, tool=tool_name,
                    error_type=error_type, consecutive=count,
                ))

                if count >= 3:
                    _log_anomaly.info("SOFT_ERROR anomaly threshold hit %s", fmtkv(
                        run_id=rid, tool=tool_name, ep_key=ep_key, error_type=error_type,
                        consecutive=count,
                    ))
                    await self._check_and_inject_feedback(rid, tool_name, ep_key, error_type, event.event_type, error_detail=err_text)
                _log_anomaly.info("SOFT_ERROR accumulated %s", fmtkv(
                    run_id=rid, ep_key=ep_key, tool=tool_name,
                    consecutive=count, per_tool=per_tool.get(tool_name, 0),
                    error_type=error_type,
                ))
            else:
                _log_observe.info("Event: seq=%d type=%s result_type=success tool=%s ep_key=%s",
                                  event.seq, event.event_type.value, tool_name, ep_key)
                per_ep = self._consecutive_per_ep.get(rid, {})
                was = per_ep.get(ep_key, 0)
                per_ep[ep_key] = 0
                self._consecutive_failures[rid] = 0
                if was > 0:
                    _log_anomaly.info("Failure streak reset (was %d) for '%s'", was, ep_key)

            # Resolution signal: only when the same endpoint succeeds
            if was >= 3 and ep_key == self._captured_ep_key.get(rid):
                _log_anomaly.info("Endpoint '%s' recovered after %d consecutive failures — sending resolution", ep_key, was)
                previous_fb_id = await self._find_last_feedback_id(rid, ep_key)
                if previous_fb_id:
                    _log_anomaly.info("Resolution linking %s", fmtkv(
                        run_id=rid, ep_key=ep_key,
                        previous_feedback_id=previous_fb_id, was=was,
                    ))
                    await self._inject_feedback(
                        rid, "low",
                        feedback_text=f"Endpoint '{ep_key}' recovered after {was} consecutive failures",
                        category=FeedbackCategory.CONDITION_RESOLVED,
                        affected_tool=ep_key,
                        resolves_feedback_id=previous_fb_id,
                        expires_at_seq=event.seq + 10,
                    )
                else:
                    _log_anomaly.warning("No previous feedback found for resolution %s", fmtkv(
                        run_id=rid, ep_key=ep_key, was=was,
                    ))

            if tc_info is not None:
                inp_hash_val = tc_info[1]
                out_hash = hash(str(event.payload.get("output")))
                new_sig = (ep_key, inp_hash_val, out_hash)
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
                            rid, "high",
                            feedback_text=f"Warning: repeated same tool call {count + 1} times ({tool_name}). "
                                          "Consider changing strategy or terminating.",
                            category=FeedbackCategory.REPEATED_CALL,
                            affected_tool=tool_name,
                            expires_at_seq=event.seq + 50,
                        )

        # ── Token overuse detection ──────────────────────────────────────────
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
                    rid, "medium",
                    feedback_text=f"Token warning: approximately {self._token_totals[rid]} tokens consumed "
                                  f"({int(self.token_warning_ratio * 100)}% threshold reached). "
                                  "Consider simplifying subsequent steps.",
                    category=FeedbackCategory.TOKEN_WARNING,
                    expires_at_seq=event.seq + 30,
                )

    # ── Unified injection logic (per-tool, no dominant derivation) ──────────

    async def _check_and_inject_feedback(
        self, rid: str, tool: str, ep_key: str, error_type: str,
        event_type: EventType, error_detail: str = "",
    ) -> None:
        err_map = self._failure_error_map.get(rid, {}).get(tool, {})
        total_errors = sum(err_map.values())

        if total_errors == 0:
            return

        dominant_error = max(err_map, key=err_map.get)
        dominant_count = err_map[dominant_error]

        current_seq = self._last_seen_seq.get(rid, 0)
        category = FeedbackCategory.GUARDRAIL_TRIGGERED if event_type == EventType.GUARDRAIL_TRIGGERED else FeedbackCategory.TOOL_FAILURE

        # In-memory dedup: one feedback per (endpoint, error_type) per run
        dedup_key = (ep_key, error_type)
        if dedup_key in self._fed_ep_keys.get(rid, set()):
            _log_anomaly.info("Dedup hit — skipping injection %s", fmtkv(
                run_id=rid, ep_key=ep_key, error_type=error_type, category=category.value,
            ))
            return

        self._fed_ep_keys.setdefault(rid, set()).add(dedup_key)
        self._captured_ep_key[rid] = ep_key

        per_tool_total = self._failures_per_tool.get(rid, {}).get(tool, 0)
        ep_count = self._consecutive_per_ep.get(rid, {}).get(ep_key, 0)
        repeat_count = self._repeat_fail_count.get(rid, 0)

        # Build feedback text dynamically — no hardcoded endpoint names
        parts = [f"Endpoint '{ep_key}' (tool '{tool}') failed {ep_count} consecutive times: '{dominant_error}'"]
        if repeat_count >= 2:
            parts.append(f"Same input parameters and same error repeated {repeat_count + 1} times. Retrying will not help.")
        if per_tool_total > ep_count:
            parts.append(f"Tool '{tool}' has {per_tool_total} total failures — endpoint may be unreachable.")

        _log_inject.info("Injecting feedback %s", fmtkv(
            run_id=rid, tool=tool, ep_key=ep_key, error_type=dominant_error,
            category=category.value, priority="high",
            ep_count=ep_count, repeat_count=repeat_count, per_tool_total=per_tool_total,
        ))

        await self._inject_feedback(
            rid, "high",
            feedback_text=" ".join(parts),
            category=category,
            affected_tool=ep_key,
            error_type=dominant_error,
            error_detail=error_detail,
            suggestion=self._generate_suggestion(tool, error_type),
            expires_at_seq=current_seq + 50,
        )

    async def _find_last_feedback_id(self, rid: str, ep_key: str) -> str | None:
        try:
            events = await self.store.get_events(rid)
        except Exception:
            _log_observe.warning("Failed to fetch events for resolution lookup")
            return None
        state = fold_events(events)
        for fb in reversed(state.feedbacks):
            if (fb.affected_tool == ep_key
                and fb.category != FeedbackCategory.CONDITION_RESOLVED
                and (fb.expires_at_seq is None or state.seq <= fb.expires_at_seq)):
                _log_observe.debug("Found last feedback %s", fmtkv(
                    run_id=rid, ep_key=ep_key, feedback_id=fb.feedback_id,
                ))
                return fb.feedback_id
        return None

    @staticmethod
    def _endpoint_key(tool: str, url: str) -> str:
        if tool == "http_request" and isinstance(url, str):
            try:
                parsed = urlparse(url)
                if parsed.netloc:
                    return parsed.netloc
            except Exception:
                pass
        return tool

    # ── Suggestion generator ─────────────────────────────────────────────────

    @staticmethod
    def _generate_suggestion(tool: str, error_type: str) -> str | None:
        """Tool + exception-class based suggestions.

        Current: 4 hardcoded patterns for ~10 tools.
        TODO: when tools exceed 10, consider FailureAdvisor registry.
        """
        suggestions = {
            ("browser", "NotImplementedError"):
                "The browser tool is unavailable on this platform. Use 'http_request' for web requests.",
            ("browser", "Timeout"):
                "Browser requests are timing out. Use 'http_request' with adjusted timeout.",
            ("http_request", "ConnectTimeout"):
                "HTTP connection timed out. Check network or try a different URL.",
            ("http_request", "InvalidURL"):
                "Invalid URL format. Check and correct the URL before retrying.",
        }
        for (t, e), s in suggestions.items():
            if tool == t and error_type.startswith(e):
                return s
        return None

    @staticmethod
    def _extract_error_type(error_text: str) -> str:
        """Extract exception class name, ignore message.

        'NotImplementedError: ...' → 'NotImplementedError'
        'PlaywrightError: Browser closed' → 'PlaywrightError'
        'TimeoutError' → 'TimeoutError'
        """
        return error_text.split(":")[0].strip() if ":" in error_text else error_text.strip()

    @staticmethod
    def _compute_feedback_id(
        run_id: str, category: FeedbackCategory,
        tool: str | None, error: str | None,
    ) -> str:
        """Deterministic hash — delegates to FeedbackInjectedPayload shared algorithm."""
        return FeedbackInjectedPayload.compute_feedback_id(
            run_id, category.value, tool or "?", error or "?",
        )

    # ── Enhanced inject ──────────────────────────────────────────────────────

    async def _inject_feedback(
        self, run_id: str, priority: str, feedback_text: str, *,
        source: FeedbackSource = FeedbackSource.MONITOR,
        category: FeedbackCategory = FeedbackCategory.OPERATOR_ADVICE,
        affected_tool: str | None = None,
        error_type: str | None = None,
        error_detail: str | None = None,
        suggestion: str | None = None,
        expires_at_seq: int | None = None,
        resolves_feedback_id: str | None = None,
    ) -> str:
        """Write a structured FeedbackInjected event to EventStore."""
        feedback_id = self._compute_feedback_id(run_id, category, affected_tool, error_type)
        payload = FeedbackInjectedPayload(
            feedback_id=feedback_id, source=source, category=category,
            feedback_text=feedback_text, priority=priority,
            affected_tool=affected_tool, error_type=error_type,
            error_detail=error_detail, suggestion=suggestion,
            expires_at_seq=expires_at_seq,
            resolves_feedback_id=resolves_feedback_id,
        )
        try:
            await self.store.append_event(run_id, EventType.FEEDBACK_INJECTED, payload.model_dump())
            _log_inject.info("Injected event %s", fmtkv(
                feedback_id=feedback_id, run_id=run_id,
                source=source.value, category=category.value,
                priority=priority, affected_tool=affected_tool,
                error_type=error_type, text_len=len(feedback_text),
                expires_at_seq=expires_at_seq,
                resolves_feedback_id=resolves_feedback_id,
                has_suggestion=suggestion is not None,
            ))
        except Exception:
            _log_inject.exception("Failed to inject feedback for %s", run_id)
        return feedback_id

    # ── Public state exposure ───────────────────────────────────────────────

    def get_state(self, run_id: str | None = None) -> dict:
        """Return a snapshot of current monitor state for observability.

        When run_id is given, return per-run details. Otherwise return
        a global summary across all monitored runs.
        """
        if run_id:
            return self._run_state(run_id)
        return self._global_state()

    def _run_state(self, rid: str) -> dict:
        per_ep = self._consecutive_per_ep.get(rid, {})
        per_tool = self._failures_per_tool.get(rid, {})
        err_map_raw = self._failure_error_map.get(rid, {})
        err_map = {t: dict(d) for t, d in err_map_raw.items()}
        return {
            "run_id": rid,
            "last_seen_seq": self._last_seen_seq.get(rid),
            "consecutive_failures": self._consecutive_failures.get(rid, 0),
            "consecutive_per_endpoint": dict(per_ep),
            "failures_per_tool": dict(per_tool),
            "failure_error_map": err_map,
            "estimated_tokens": self._token_totals.get(rid, 0),
            "token_warning_sent": rid in self._token_warning_sent,
            "repeated_call_count": self._repeated_call_count.get(rid, 0),
            "repeated_fail_count": self._repeat_fail_count.get(rid, 0),
            "stuck_feedback_sent": self._stuck_feedback_sent.get(rid, 0),
            "deduped_feedback_keys": sorted(
                list(self._fed_ep_keys.get(rid, set()))
            ) if rid in self._fed_ep_keys else [],
            "pending_calls": len(self._pending_calls.get(rid, {})),
            "config": {
                "max_tokens": self.max_tokens,
                "token_warning_ratio": self.token_warning_ratio,
            },
        }

    def _global_state(self) -> dict:
        monitored_runs = set(self._consecutive_failures.keys()) \
            | set(self._token_totals.keys()) \
            | set(self._pending_calls.keys())
        runs: list[dict] = []
        for rid in sorted(monitored_runs):
            runs.append({
                "run_id": rid,
                "last_seen_seq": self._last_seen_seq.get(rid),
                "consecutive_failures": self._consecutive_failures.get(rid, 0),
                "estimated_tokens": self._token_totals.get(rid, 0),
                "token_warning_sent": rid in self._token_warning_sent,
                "endpoints_tracked": len(self._consecutive_per_ep.get(rid, {})),
                "tools_with_failures": len(self._failures_per_tool.get(rid, {})),
                "deduped_feedback_count": len(self._fed_ep_keys.get(rid, set())),
            })
        return {
            "monitored_run_count": len(runs),
            "runs": runs,
            "config": {
                "max_tokens": self.max_tokens,
                "token_warning_ratio": self.token_warning_ratio,
            },
        }

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def cleanup(self, run_id: str) -> None:
        """Release per-run state when a run finishes or is cancelled."""
        pending_count = len(self._pending_calls.get(run_id, {}))
        was_ep_entries = self._consecutive_per_ep.pop(run_id, None)
        was_tokens = self._token_totals.pop(run_id, None)
        self._pending_calls.pop(run_id, None)
        self._last_sig.pop(run_id, None)
        self._repeated_call_count.pop(run_id, None)
        self._stuck_feedback_sent.pop(run_id, None)
        self._token_warning_sent.discard(run_id)
        self._failures_per_tool.pop(run_id, None)
        self._failure_error_map.pop(run_id, None)
        self._captured_ep_key.pop(run_id, None)
        self._fed_ep_keys.pop(run_id, None)
        self._last_fail_sig.pop(run_id, None)
        self._repeat_fail_count.pop(run_id, None)
        self._last_seen_seq.pop(run_id, None)
        self._consecutive_failures.pop(run_id, None)
        err_map_entries = sum(len(m) for m in self._failure_error_map.get(run_id, {}).values())
        ep_keys = len(was_ep_entries) if was_ep_entries else 0
        _log_observe.info("Cleaned up run %s %s", run_id, fmtkv(
            token_total=was_tokens, pending_calls=pending_count,
            error_map_entries=err_map_entries, tracked_endpoints=ep_keys,
        ))
