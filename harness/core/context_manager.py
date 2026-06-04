"""Context Manager (V0.5) — trusted component for automatic context compression and checkpointing.

Monitors context token usage, triggers LLM-based summary compression when
approaching token limits, and periodically writes checkpoints for resume.
Agent is never aware of compression — it is pure infrastructure behavior.
"""

from __future__ import annotations

import json
import logging

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.models.events import (
    ContextCheckpointedPayload,
    ContextCompressedPayload,
    EpisodeSummary,
    Event,
    EventType,
)

_logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = (
    'You are a context compression system. Summarize the following agent activity log. '
    'Output your response as a JSON object with these exact fields:\n'
    '- "key_decisions": list of strings — the key decisions the agent made\n'
    '- "tools_used": list of strings — which tools were called\n'
    '- "key_findings": list of strings — important information discovered\n'
    '- "errors_encountered": list of strings — any errors or warnings\n'
    '- "current_plan": string or null — the plan at this point (if any)\n'
    'Be factual and concise. Return ONLY valid JSON, no markdown or explanation.'
)


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
          - Emergency compression (over 80% threshold):
              compress oldest 50% of events, keep recent 3 rounds (keep_count=3)
        """
        estimate = precomputed_estimate if precomputed_estimate is not None else self._estimate_context_tokens(state)
        if estimate < self.compression_threshold:
            return None

        if estimate < self.emergency_threshold:
            return {
                "compress_thoughts": state.thought_history,
                "compress_results": state.tool_results,
                "keep_count": 2,
            }

        mid = len(state.thought_history) // 2
        if mid < 1:
            return {
                "compress_thoughts": state.thought_history,
                "compress_results": state.tool_results,
                "keep_count": 2,
            }

        _logger.info(
            "Emergency compression for %s: compressing oldest %d thoughts, keeping recent 3 rounds",
            state.run_id, mid,
        )
        return {
            "compress_thoughts": state.thought_history[:mid],
            "compress_results": state.tool_results[:mid],
            "keep_count": 3,
        }

    async def maybe_compress(self, run_id: str, iteration: int, state: RunState) -> None:
        """Check context size and trigger LLM summary compression if needed.

        Writes a ContextCompressed event when token estimate exceeds threshold.
        Guards against repeat compression: only fires once per checkpoint_interval
        iterations for the same run_id.
        The actual compression (truncation of history in AgentKernel) happens
        on the next iteration when fold_events sets state.summary from the event.
        """
        estimate = self._estimate_context_tokens(state)
        if estimate < self.compression_threshold:
            return

        last = self._last_compressed_iteration.get(run_id, 0)
        cooldown = self.checkpoint_interval
        if last > 0 and iteration - last < cooldown:
            _logger.debug(
                "Skipping compress for %s (iteration %d, last %d, cooldown %d)",
                run_id, iteration, last, cooldown,
            )
            return
        self._last_compressed_iteration[run_id] = iteration

        window = self.select_compression_window(state, precomputed_estimate=estimate)
        if window is None:
            return

        _logger.info(
            "Context compression triggered for %s: ~%d tokens (threshold %d)",
            run_id, estimate, self.compression_threshold,
        )

        summary = await self._generate_summary(
            state,
            compress_thoughts=window["compress_thoughts"],
            compress_results=window["compress_results"],
        )
        summary_tokens = self._estimate_text_tokens(
            summary if isinstance(summary, str) else summary.model_dump_json()
        )

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
        compress_thoughts: list | None = None,
        compress_results: list | None = None,
    ) -> EpisodeSummary | str:
        """Call LLM to generate a compressed summary of activity.

        With LLM: asks for JSON output matching EpisodeSummary content fields,
        returns a fully populated EpisodeSummary (non-content fields filled by caller).
        Without LLM: returns a plain-text concatenation as fallback.

        The optional compress_thoughts/compress_results parameters limit which
        events are summarized (used by emergency compression).
        """
        thoughts = compress_thoughts if compress_thoughts is not None else state.thought_history
        results = compress_results if compress_results is not None else state.tool_results

        activity_lines = []
        for t in thoughts:
            activity_lines.append(f"Thought: {t.thought[:500]}")
        for tr in results:
            out = str(tr.output or tr.error or "")[:300]
            activity_lines.append(f"Tool '{tr.tool_name}' → {out}")

        activity_text = "\n".join(activity_lines)

        if self.llm_client is None:
            if len(activity_text) <= 2000:
                return activity_text or "No activity recorded."
            return activity_text[:1000] + "\n...(truncated)...\n" + activity_text[-1000:]

        response = await self.llm_client.chat(
            [
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user", "content": activity_text},
            ],
            temperature=0.0,
            max_tokens=2048,
        )

        try:
            data = json.loads(response)
            # NOTE: episode_range and original_event_refs are placeholders
            # (not yet populated by caller). They are defined in the model for
            # future traceability but not relied on by any consumer today.
            return EpisodeSummary(
                episode_range=(0, 0),
                original_tokens=0,
                compressed_tokens=0,
                key_decisions=data.get("key_decisions", []),
                tools_used=data.get("tools_used", []),
                key_findings=data.get("key_findings", []),
                errors_encountered=data.get("errors_encountered", []),
                current_plan=data.get("current_plan"),
                original_event_refs=[],
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return response.strip()
