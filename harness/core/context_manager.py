"""Context Manager (V0.5) — trusted component for automatic context compression and checkpointing.

Monitors context token usage, triggers LLM-based summary compression when
approaching token limits, and periodically writes checkpoints for resume.
Agent is never aware of compression — it is pure infrastructure behavior.
"""

from __future__ import annotations

import json
import time

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.logger import guard_logger
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import (
    ContextCheckpointedPayload,
    ContextCompressedPayload,
    EpisodeSummary,
    Event,
    EventType,
)

_log_monitor = guard_logger("context.monitor")
_log_compress = guard_logger("context.compress")
_log_checkpoint = guard_logger("context.checkpoint")



class ContextManager:
    """Monitors context size and triggers compression / checkpointing.

    Plugs into AgentLoopScheduler — called each iteration before think.
    Uses a heuristic for token estimation (char_count × 0.25) with
    a comment noting that a future version should use LLM tokenize API.
    """

    def __init__(
        self,
        store,
        llm_client: LLMClient | None = None,
        token_limit: int = 128_000,
        checkpoint_interval: int = 10,
        compression_threshold_ratio: float = 0.8,
        emergency_threshold_ratio: float = 0.9,
    ):
        self.store = store
        self.llm_client = llm_client
        self.token_limit = token_limit
        self.checkpoint_interval = checkpoint_interval
        self.compression_threshold = int(token_limit * compression_threshold_ratio)
        self.emergency_threshold = int(token_limit * emergency_threshold_ratio)
        self._last_compressed_iteration: dict[str, int] = {}

    def select_compression_window(self, state: RunState, precomputed_estimate: int | None = None) -> dict | None:
        """Determine which events to compress and how many recent ones to keep.

        Returns dict with compress_thoughts, compress_results, keep_count fields,
        or None when no compression is needed.
          - Normal compression: compress everything, keep_count=2
          - Emergency compression (estimated tokens exceed the hard token limit,
            or a custom overflow threshold above it): compress oldest 50% of
            events, keep recent 3 rounds (keep_count=3)
        """
        estimate = precomputed_estimate if precomputed_estimate is not None else self._estimate_context_tokens(state)
        if estimate < self.compression_threshold:
            _log_monitor.debug("~%d tokens < %d threshold, skipping compression", estimate, self.compression_threshold)
            return None

        # Boundary rule: emergency is reserved for actual overflow beyond the
        # configured token limit.  Estimates between compression_threshold and
        # token_limit use normal compression.  emergency_threshold_ratio values
        # greater than 1.0 can still raise the overflow boundary when needed.
        overflow_threshold = max(self.emergency_threshold, self.token_limit)
        if estimate <= overflow_threshold:
            _log_compress.info("~%d tokens exceeds %d threshold → normal compression "
                               "(keep %d recent, compress %d thoughts + %d results)",
                               estimate, self.compression_threshold, 2,
                               len(state.thought_history), len(state.tool_results))
            return {
                "compress_thoughts": state.thought_history,
                "compress_results": state.tool_results,
                "keep_count": 2,
            }

        mid = len(state.thought_history) // 2
        if mid < 1:
            _log_compress.info("~%d tokens exceeds emergency threshold → normal compression (mid<1)", estimate)
            return {
                "compress_thoughts": state.thought_history,
                "compress_results": state.tool_results,
                "keep_count": 2,
            }

        # ── Plan boundary alignment ──────────────────────────────────
        if state.plan_boundary_seqs:
            mid_seq = state.thought_history[mid].seq
            span = state.thought_history[-1].seq - state.thought_history[0].seq
            if span > 0:
                nearest = min(state.plan_boundary_seqs, key=lambda s: abs(s - mid_seq))
                if abs(nearest - mid_seq) < span * 0.2:
                    new_mid = max(
                        (i + 1 for i, t in enumerate(state.thought_history) if t.seq <= nearest),
                        default=mid,
                    )
                    if 0 < new_mid < len(state.thought_history):
                        _log_compress.info(
                            "Aligned to plan boundary seq=%d: thought[%d] → thought[%d]",
                            nearest, mid, new_mid,
                        )
                        mid = new_mid

        _log_compress.info("~%d tokens exceeds %d overflow threshold → emergency compression "
                           "(compress oldest %d, keep %d recent)",
                           estimate, overflow_threshold, mid, 3)
        return {
            "compress_thoughts": state.thought_history[:mid],
            "compress_results": state.tool_results[:mid],
            "keep_count": 3,
        }

    async def maybe_compress(self, run_id: str, iteration: int, state: RunState) -> None:
        """Check context size and trigger LLM summary compression if needed.

        When plan boundaries exist (V0.7 DAG flow), only compresses at the most
        recent PlanCompleted boundary. Without plan boundaries (legacy flow),
        compresses on threshold only (backward compatible).

        Writes a ContextCompressed event when token estimate exceeds threshold.
        Guards against repeat compression: only fires once per checkpoint_interval
        iterations for the same run_id.
        The actual compression (truncation of history in AgentKernel) happens
        on the next iteration when fold_events sets state.summary from the event.
        """
        if state.plan_boundary_seqs and state.seq < state.plan_boundary_seqs[-1]:
            return
        estimate = self._estimate_context_tokens(state)
        _log_monitor.debug("Token estimate: ~%d tokens (threshold: %d)", estimate, self.compression_threshold)
        if estimate < self.compression_threshold:
            return

        last = self._last_compressed_iteration.get(run_id, 0)
        cooldown = self.checkpoint_interval
        if last > 0 and iteration - last < cooldown:
            _log_monitor.debug("Skipping: last compression at iter %d (cooldown %d, current iter %d)",
                               last, cooldown, iteration)
            return
        self._last_compressed_iteration[run_id] = iteration

        window = self.select_compression_window(state, precomputed_estimate=estimate)
        if window is None:
            return

        _log_compress.info("Compressing context at iteration %d (~%d tokens)", iteration, estimate)

        compress_thoughts = window["compress_thoughts"]
        compress_results = window["compress_results"]

        thought_seqs = [t.seq for t in compress_thoughts if hasattr(t, "seq")]
        result_seqs = [tr.event_seq for tr in compress_results if hasattr(tr, "event_seq")]
        all_seqs = sorted(set(thought_seqs + result_seqs))
        episode_range = (all_seqs[0], all_seqs[-1]) if all_seqs else (0, 0)
        original_event_refs = all_seqs

        _t_gen = time.monotonic()
        summary = await self._generate_summary(
            state,
            episode_range=episode_range,
            original_event_refs=original_event_refs,
            original_tokens=estimate,
            compress_thoughts=compress_thoughts,
            compress_results=compress_results,
        )
        _gen_ms = (time.monotonic() - _t_gen) * 1000
        summary_tokens = self._estimate_text_tokens(summary.model_dump_json())
        _log_compress.info("Compressed: %d → %d tokens (%dms, keep=%d recent)",
                           estimate, summary_tokens, _gen_ms, window["keep_count"])

        await self.store.append_event(
            run_id,
            EventType.CONTEXT_COMPRESSED,
            ContextCompressedPayload(
                original_tokens=estimate,
                compressed_tokens=summary_tokens,
                summary_ref=summary,
                keep_recent_count=window["keep_count"],
            ).model_dump(),
        )

    async def try_checkpoint(self, run_id: str, iteration: int, state: RunState) -> None:
        """Write ContextCheckpointed every N iterations."""
        if iteration <= 0 or iteration % self.checkpoint_interval != 0:
            return

        token_count = self._estimate_context_tokens(state)

        _log_checkpoint.info("Checkpoint at iter %d: seq=%d, ~%d tokens", iteration, state.seq, token_count)
        await self.store.append_event(
            run_id,
            EventType.CONTEXT_CHECKPOINTED,
            ContextCheckpointedPayload(
                checkpoint_seq=state.seq,
                snapshot_ref=f"checkpoint_iter_{iteration}",
                token_count=token_count,
            ).model_dump(),
        )

    @staticmethod
    def find_resume_seq(events: list[Event]) -> int:
        """Find the seq of the latest ContextCheckpointed event.

        Returns 0 if no checkpoint is found (start folding from beginning).
        """
        last = 0
        for e in events:
            if e.event_type == EventType.CONTEXT_CHECKPOINTED:
                p = ContextCheckpointedPayload(**e.payload)
                if p.checkpoint_seq > last:
                    last = p.checkpoint_seq
        return last

    def _estimate_context_tokens(self, state: RunState) -> int:
        """Heuristic token estimation based on character count.

        Uses char_count × 0.25 (roughly 4 chars per token for mixed
        English/Chinese content).

        TODO(V0.5+): Replace with actual LLM tokenize API for accurate counting.
        """
        total_chars = 0
        for t in state.thought_history:
            total_chars += len(t.thought)
        for tr in state.tool_results:
            if tr.output is not None:
                total_chars += len(str(tr.output))
            if tr.error:
                total_chars += len(tr.error)
        return max(1, int(total_chars * 0.25))

    def _estimate_text_tokens(self, text: str) -> int:
        """Heuristic token estimation for a plain text string."""
        return max(1, int(len(text) * 0.25))

    async def _generate_summary(
        self,
        state: RunState,
        episode_range: tuple[int, int] = (0, 0),
        original_event_refs: list[int] | None = None,
        original_tokens: int = 0,
        compress_thoughts: list | None = None,
        compress_results: list | None = None,
    ) -> EpisodeSummary:
        """Call LLM to generate a compressed summary of activity.

        Returns an EpisodeSummary with full fields:
          - Content fields (key_decisions, tools_used, ...) populated by LLM
          - Metadata fields (episode_range, original_tokens, ...) passed in by caller

        Without LLM: content fields are empty, raw text stored in current_plan.
        LLM non-JSON response: similarly degraded to current_plan.
        """
        thoughts = compress_thoughts if compress_thoughts is not None else state.thought_history
        results = compress_results if compress_results is not None else state.tool_results
        refs = sorted(original_event_refs) if original_event_refs else []

        activity_lines = []
        for t in thoughts:
            activity_lines.append(f"Thought: {t.thought[:500]}")
        for tr in results:
            raw = str(tr.output or tr.error or "")
            if len(raw) > 2000:
                if raw.startswith("{") or raw.startswith("["):
                    out = raw[:2000] + "\n...(truncated)..."
                else:
                    out = raw[:2000] + "\n...(truncated)..."
            else:
                out = raw
            activity_lines.append(f"Tool '{tr.tool_name}' → {out}")

        # Preserve PlanCreated/PlanRevised event details (compression whitelist)
        for plan_entry in state.plan_history:
            plan_id = plan_entry.get("plan_id", "?")
            plan_intent = plan_entry.get("intent", "")[:80]
            activity_lines.append(f"[Plan] {plan_id}: {plan_intent}")
            revision = plan_entry.get("revision_reason")
            if revision:
                activity_lines.append(f"[Plan] Revision reason: {revision}")
            for step in plan_entry.get("steps", []):
                sid = step.get("step_id", "?")
                tool = step.get("tool_name", "?")
                st = step.get("status", "?")
                detail = ""
                if st == "completed":
                    detail = f" → {step.get('output_summary', '')[:100]}"
                elif st == "failed":
                    detail = f" ✗ {step.get('error', '')[:100]}"
                activity_lines.append(f"  Step {sid}({tool}): {st}{detail}")

        activity_text = "\n".join(activity_lines)

        if self.llm_client is None:
            if len(activity_text) > 2000:
                activity_text = activity_text[:1000] + "\n...(truncated)...\n" + activity_text[-1000:]
            return EpisodeSummary(
                episode_range=episode_range,
                original_tokens=original_tokens,
                compressed_tokens=self._estimate_text_tokens(activity_text),
                key_decisions=[],
                tools_used=list(dict.fromkeys(tr.tool_name for tr in results)) if results else [],
                key_findings=[],
                errors_encountered=[str(tr.error) for tr in results if tr.error] if results else [],
                current_plan=activity_text or None,
                original_event_refs=refs,
            )

        _log_compress.info("[summarize] === ACTIVITY TEXT (%d chars) ===\n%s\n=== END ACTIVITY TEXT ===",
                            len(activity_text), activity_text)

        chat_resp = await self.llm_client.chat(
            [
                {"role": "system", "content": get_prompt(AgentPhase.SUMMARIZE)},
                {"role": "user", "content": activity_text},
            ],
            temperature=0.0,
            max_tokens=2048,
        )
        response = chat_resp.content

        try:
            data = json.loads(response)
        except (json.JSONDecodeError, TypeError, ValueError):
            data = None

        if data is None:
            return EpisodeSummary(
                episode_range=episode_range,
                original_tokens=original_tokens,
                compressed_tokens=self._estimate_text_tokens(response.strip()),
                key_decisions=[],
                tools_used=list(dict.fromkeys(tr.tool_name for tr in results)) if results else [],
                key_findings=[],
                errors_encountered=[],
                current_plan=response.strip(),
                original_event_refs=refs,
            )

        return EpisodeSummary(
            episode_range=episode_range,
            original_tokens=original_tokens,
            compressed_tokens=self._estimate_text_tokens(response),
            key_decisions=data.get("key_decisions", []),
            tools_used=data.get("tools_used", []),
            key_findings=data.get("key_findings", []),
            errors_encountered=data.get("errors_encountered", []),
            current_plan=data.get("current_plan"),
            original_event_refs=refs,
        )
