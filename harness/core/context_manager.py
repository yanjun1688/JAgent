"""Context Manager (V0.5 + V3.0 Phase 1) — trusted component for automatic context compression and checkpointing.

Monitors context token usage via pluggable TokenCounter, triggers 3-tier
compression strategy (lazy_clear / episode_archive / emergency_compact),
and periodically writes checkpoints for resume.
Agent is never aware of compression — it is pure infrastructure behavior.
"""

from __future__ import annotations

import json
import time

from harness.core.fold import RunState, ThoughtEntry, ToolResult
from harness.core.llm_client import LLMClient
from harness.core.logger import fmtkv, guard_logger
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.core.token_counter import TokenCounter, create_token_counter
from harness.models.events import (
    ContextCheckpointedPayload,
    ContextPrunedPayload,
    Episode,
    EpisodeArchivedPayload,
    Event,
    EventType,
)

_log_monitor = guard_logger("context.monitor")
_log_compress = guard_logger("context.compress")
_log_checkpoint = guard_logger("context.checkpoint")


class ContextManager:
    """Monitors context size and triggers compression / checkpointing.

    Plugs into AgentLoopScheduler — called each iteration before think.
    Uses TokenCounter for accurate token estimation with auto-fallback.
    3-tier compression: lazy_clear (>50%) → episode_archive (>70%) → emergency (>90%).
    """

    def __init__(
        self,
        store,
        llm_client: LLMClient | None = None,
        token_counter: TokenCounter | None = None,
        token_limit: int = 128_000,
        checkpoint_interval: int = 10,
        compression_threshold_ratio: float = 0.7,
        emergency_threshold_ratio: float = 0.9,
        lazy_clear_ratio: float = 0.5,
    ):
        self.store = store
        self.llm_client = llm_client
        self.token_counter = token_counter or create_token_counter()
        self.token_limit = token_limit
        self.checkpoint_interval = checkpoint_interval
        self.compression_threshold_ratio = compression_threshold_ratio
        self.emergency_threshold_ratio = emergency_threshold_ratio
        self.lazy_clear_ratio = lazy_clear_ratio
        self.compression_threshold = int(token_limit * compression_threshold_ratio)
        self.emergency_threshold = int(token_limit * emergency_threshold_ratio)
        self.lazy_clear_threshold = int(token_limit * lazy_clear_ratio)
        self._last_compressed_iteration: dict[str, int] = {}

    async def _async_estimate_context_tokens(self, state: RunState) -> int:
        """Async TokenCounter-based estimation."""
        total = 0
        for t in state.thought_history:
            total += await self.token_counter.count(t.thought)
        for tr in state.tool_results:
            if tr.output is not None:
                total += await self.token_counter.count(str(tr.output))
            if tr.error:
                total += await self.token_counter.count(tr.error)
        return max(1, total)

    async def _async_estimate_text_tokens(self, text: str) -> int:
        """Async TokenCounter-based estimation for plain text."""
        return max(1, await self.token_counter.count(text))

    async def maybe_compress(self, run_id: str, iteration: int, state: RunState) -> None:
        """3-tier compression strategy unified entry point.

        < 50%: no action
        50-70%: lazy_clear — remove low-importance processed events
        70-90%: episode_archive — generate structured Episode, write event
        > 90%: emergency_compact — aggressive compression, keep recent 3
        """
        if state.plan_boundary_seqs and state.seq < state.plan_boundary_seqs[-1]:
            return
        estimate = await self._async_estimate_context_tokens(state)
        ratio = estimate / self.token_limit if self.token_limit > 0 else 0

        _log_monitor.info(
            "COMPRESSION_CHECK %s",
            fmtkv(
                run_id=run_id,
                iteration=iteration,
                token_estimate=estimate,
                token_limit=self.token_limit,
                ratio=f"{ratio:.2%}",
                thoughts=len(state.thought_history),
                results=len(state.tool_results),
                lazy_threshold=f"{self.lazy_clear_ratio:.0%}",
                archive_threshold=f"{self.compression_threshold_ratio:.0%}",
                emergency_threshold=f"{self.emergency_threshold_ratio:.0%}",
            ),
        )

        if ratio <= 0.5:
            _log_monitor.debug("COMPRESSION_SKIP %s", fmtkv(run_id=run_id, reason="below_50%"))
            return

        last = self._last_compressed_iteration.get(run_id, 0)
        cooldown = self.checkpoint_interval
        if last > 0 and iteration - last < cooldown:
            _log_monitor.debug(
                "COMPRESSION_SKIP %s",
                fmtkv(run_id=run_id, reason="cooldown", last_iter=last, current_iter=iteration, cooldown=cooldown),
            )
            return
        self._last_compressed_iteration[run_id] = iteration

        if ratio > 0.9:
            _log_compress.info(
                "COMPRESSION_DECIDE %s", fmtkv(run_id=run_id, strategy="emergency_compact", ratio=f"{ratio:.2%}")
            )
            await self._emergency_compact(run_id, state, estimate)
        elif ratio > 0.7:
            _log_compress.info(
                "COMPRESSION_DECIDE %s", fmtkv(run_id=run_id, strategy="episode_archive", ratio=f"{ratio:.2%}")
            )
            await self._archive_episode(run_id, state, estimate)
        else:
            _log_compress.info(
                "COMPRESSION_DECIDE %s", fmtkv(run_id=run_id, strategy="lazy_clear", ratio=f"{ratio:.2%}")
            )
            await self._lazy_clear(run_id, state, estimate)

    async def _lazy_clear(self, run_id: str, state: RunState, token_count: int) -> None:
        """Remove low-importance processed events from RunState (via event)."""
        pruned_refs, pruned_tokens = await self._select_low_importance_events(state)
        if not pruned_refs:
            _log_monitor.info(
                "LAZY_CLEAR_SKIP %s",
                fmtkv(run_id=run_id, reason="no_low_importance_events", token_count=token_count),
            )
            return

        _log_compress.info(
            "LAZY_CLEAR_START %s",
            fmtkv(
                run_id=run_id,
                pruned_event_count=len(pruned_refs),
                pruned_token_estimate=pruned_tokens,
                pruned_seqs=pruned_refs[:10],
                original_tokens=token_count,
            ),
        )

        if self.store:
            await self.store.append_event(
                run_id,
                EventType.CONTEXT_PRUNED,
                ContextPrunedPayload(
                    pruned_event_refs=pruned_refs,
                    pruned_token_count=pruned_tokens,
                    pruned_seq_count=len(pruned_refs),
                    reason="lazy_clear",
                ).model_dump(),
            )
            _log_compress.info(
                "LAZY_CLEAR_DONE %s",
                fmtkv(
                    run_id=run_id,
                    event_type="ContextPruned",
                    pruned_count=len(pruned_refs),
                ),
            )

    async def _archive_episode(self, run_id: str, state: RunState, token_count: int) -> None:
        """Generate structured Episode and write archive event.

        Only archives non-recent events (excludes last `keep` rounds),
        matching the old ContextCompressed behavior. Recent events stay
        in working memory and are not included in the Episode.
        """
        keep = 2
        compress_thoughts = state.thought_history[:-keep] if len(state.thought_history) > keep else []
        compress_results = state.tool_results[:-keep] if len(state.tool_results) > keep else []

        if not compress_thoughts and not compress_results:
            _log_monitor.info(
                "EPISODE_ARCHIVE_SKIP %s",
                fmtkv(run_id=run_id, reason="nothing_to_archive", keep_recent=keep),
            )
            return

        thought_seqs = [t.seq for t in compress_thoughts]
        result_seqs = [tr.event_seq for tr in compress_results]
        archived_refs = sorted(set(thought_seqs + result_seqs))
        episode_range = (archived_refs[0], archived_refs[-1]) if archived_refs else (0, 0)

        _log_compress.info(
            "EPISODE_ARCHIVE_START %s",
            fmtkv(
                run_id=run_id,
                original_tokens=token_count,
                thoughts_to_archive=len(compress_thoughts),
                results_to_archive=len(compress_results),
                archived_event_count=len(archived_refs),
                episode_range=f"{episode_range[0]}-{episode_range[1]}",
                keep_recent=keep,
                has_llm=self.llm_client is not None,
            ),
        )

        _t_gen = time.monotonic()
        episode = await self._generate_episode(
            state,
            episode_range=episode_range,
            original_event_refs=archived_refs,
            original_tokens=token_count,
            compress_thoughts=compress_thoughts,
            compress_results=compress_results,
        )
        _gen_ms = (time.monotonic() - _t_gen) * 1000
        summary_tokens = await self._async_estimate_text_tokens(episode.model_dump_json())

        _log_compress.info(
            "EPISODE_GENERATED %s",
            fmtkv(
                run_id=run_id,
                episode_title=episode.title[:50] if episode.title else None,
                format=episode.format,
                importance_score=episode.importance_score,
                key_decisions_count=len(episode.key_decisions),
                tools_used_count=len(episode.tools_used),
                key_findings_count=len(episode.key_findings),
                errors_count=len(episode.errors_encountered),
                generation_ms=_gen_ms,
            ),
        )

        if self.store:
            await self.store.append_event(
                run_id,
                EventType.EPISODE_ARCHIVED,
                EpisodeArchivedPayload(
                    original_tokens=token_count,
                    compressed_tokens=summary_tokens,
                    episode=episode,
                    keep_recent_count=keep,
                    archived_event_refs=archived_refs,
                ).model_dump(),
            )

            compression_ratio = (1 - summary_tokens / token_count) * 100 if token_count > 0 else 0
            _log_compress.info(
                "EPISODE_ARCHIVE_DONE %s",
                fmtkv(
                    run_id=run_id,
                    event_type="EpisodeArchived",
                    original_tokens=token_count,
                    compressed_tokens=summary_tokens,
                    compression_ratio=f"{compression_ratio:.1f}%",
                    archived_refs_count=len(archived_refs),
                    keep_recent=keep,
                ),
            )

    async def _emergency_compact(self, run_id: str, state: RunState, token_count: int) -> None:
        """Aggressive compression: keep recent 3, ignore importance."""
        keep_count = 3
        compress_thoughts = state.thought_history[:-keep_count] if len(state.thought_history) > keep_count else []
        compress_results = state.tool_results[:-keep_count] if len(state.tool_results) > keep_count else []

        thought_seqs = [t.seq for t in compress_thoughts]
        result_seqs = [tr.event_seq for tr in compress_results]
        archived_refs = sorted(set(thought_seqs + result_seqs))
        episode_range = (archived_refs[0], archived_refs[-1]) if archived_refs else (0, 0)

        _log_compress.warning(
            "EMERGENCY_COMPACT_START %s",
            fmtkv(
                run_id=run_id,
                original_tokens=token_count,
                thoughts_to_archive=len(compress_thoughts),
                results_to_archive=len(compress_results),
                archived_event_count=len(archived_refs),
                episode_range=f"{episode_range[0]}-{episode_range[1]}",
                keep_recent=keep_count,
                reason="above_90%_threshold",
            ),
        )

        _t_gen = time.monotonic()
        episode = await self._generate_episode(
            state,
            episode_range=episode_range,
            original_event_refs=archived_refs,
            original_tokens=token_count,
            compress_thoughts=compress_thoughts,
            compress_results=compress_results,
            emergency=True,
        )
        _gen_ms = (time.monotonic() - _t_gen) * 1000
        summary_tokens = await self._async_estimate_text_tokens(episode.model_dump_json())

        _log_compress.warning(
            "EMERGENCY_EPISODE_GENERATED %s",
            fmtkv(
                run_id=run_id,
                episode_title=episode.title[:50] if episode.title else None,
                format=episode.format,
                generation_ms=_gen_ms,
            ),
        )

        if self.store:
            await self.store.append_event(
                run_id,
                EventType.EPISODE_ARCHIVED,
                EpisodeArchivedPayload(
                    original_tokens=token_count,
                    compressed_tokens=summary_tokens,
                    episode=episode,
                    keep_recent_count=keep_count,
                    archived_event_refs=archived_refs,
                ).model_dump(),
            )

            compression_ratio = (1 - summary_tokens / token_count) * 100 if token_count > 0 else 0
            _log_compress.warning(
                "EMERGENCY_COMPACT_DONE %s",
                fmtkv(
                    run_id=run_id,
                    event_type="EpisodeArchived",
                    original_tokens=token_count,
                    compressed_tokens=summary_tokens,
                    compression_ratio=f"{compression_ratio:.1f}%",
                    archived_refs_count=len(archived_refs),
                    keep_recent=keep_count,
                ),
            )

    def _score_event_importance(self, entry: ThoughtEntry | ToolResult) -> float:
        """System-enforced importance scoring, no LLM required.

        Note: Thought scoring uses keyword-based heuristics (decision_markers).
        This is a Phase 1 MVE — future versions should integrate PlanCreated /
        PlanRevised events for more accurate decision detection.
        """
        if isinstance(entry, ThoughtEntry):
            thought_lower = entry.thought.lower()
            decision_markers = ["decided", "choose", "select", "plan", "strategy", "approach"]
            if any(m in thought_lower for m in decision_markers):
                return 0.7
            return 0.5

        if isinstance(entry, ToolResult):
            status_val = entry.status.value if hasattr(entry.status, "value") else str(entry.status)
            if status_val in ("failed", "timeout", "guardrail_blocked"):
                return 0.8
            if status_val == "unsuccessful":
                return 0.6
            return 0.2

        raise TypeError(f"Expected ThoughtEntry or ToolResult, got {type(entry).__name__}")

    async def _select_low_importance_events(self, state: RunState) -> tuple[list[int], int]:
        """Select low-importance events for lazy_clear pruning.

        Returns (pruned_seq_refs, estimated_pruned_tokens).
        Only prunes events with importance <= 0.2 that are not in the recent window.
        """
        keep_recent = max(state.keep_recent_count, 2)
        recent_thought_seqs = (
            {t.seq for t in state.thought_history[-keep_recent:]} if len(state.thought_history) > keep_recent else set()
        )
        recent_result_seqs = (
            {tr.event_seq for tr in state.tool_results[-keep_recent:]}
            if len(state.tool_results) > keep_recent
            else set()
        )

        pruned_refs = []
        pruned_tokens = 0

        old_thoughts = state.thought_history[:-keep_recent] if len(state.thought_history) > keep_recent else []
        for t in old_thoughts:
            if t.seq in recent_thought_seqs:
                continue
            score = self._score_event_importance(t)
            if score <= 0.2:
                pruned_refs.append(t.seq)
                pruned_tokens += await self.token_counter.count(t.thought)

        old_results = state.tool_results[:-keep_recent] if len(state.tool_results) > keep_recent else []
        for tr in old_results:
            if tr.event_seq in recent_result_seqs:
                continue
            score = self._score_event_importance(tr)
            if score <= 0.2:
                pruned_refs.append(tr.event_seq)
                pruned_tokens += await self.token_counter.count(str(tr.output or ""))

        return pruned_refs, pruned_tokens

    async def _generate_episode(
        self,
        state: RunState,
        episode_range: tuple[int, int] = (0, 0),
        original_event_refs: list[int] | None = None,
        original_tokens: int = 0,
        compress_thoughts: list | None = None,
        compress_results: list | None = None,
        emergency: bool = False,
    ) -> Episode:
        """Call LLM to generate a structured Episode.

        Returns Episode with full fields. Without LLM: format="legacy".
        LLM non-JSON response: format="legacy", raw text in current_plan.
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
                out = raw[:2000] + "\n...(truncated)..."
            else:
                out = raw
            activity_lines.append(f"Tool '{tr.tool_name}' → {out}")

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
        tools_used = list(dict.fromkeys(tr.tool_name for tr in results)) if results else []
        errors = [str(tr.error) for tr in results if tr.error] if results else []

        if self.llm_client is None:
            if len(activity_text) > 2000:
                activity_text = activity_text[:1000] + "\n...(truncated)...\n" + activity_text[-1000:]
            compressed_tokens = await self._async_estimate_text_tokens(activity_text)
            _log_compress.info(
                "EPISODE_GENERATE_NO_LLM %s",
                fmtkv(
                    activity_chars=len(activity_text),
                    compressed_tokens=compressed_tokens,
                    format="legacy",
                ),
            )
            return Episode(
                episode_range=episode_range,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                key_decisions=[],
                tools_used=tools_used,
                key_findings=[],
                errors_encountered=errors,
                current_plan=activity_text or None,
                original_event_refs=refs,
                title="Legacy summary (no LLM)",
                summary=activity_text[:500] if activity_text else "",
                importance_score=self._compute_episode_importance(state),
                format="legacy",
            )

        _log_compress.info(
            "EPISODE_LLM_CALL_START %s",
            fmtkv(
                activity_chars=len(activity_text),
                activity_tokens=await self._async_estimate_text_tokens(activity_text),
                thoughts_count=len(thoughts),
                results_count=len(results),
            ),
        )

        chat_resp = await self.llm_client.chat(
            [
                {"role": "system", "content": get_prompt(AgentPhase.SUMMARIZE)},
                {"role": "user", "content": activity_text},
            ],
            temperature=0.0,
            max_tokens=2048,
        )
        response = chat_resp.content

        _log_compress.info(
            "EPISODE_LLM_CALL_DONE %s",
            fmtkv(
                response_chars=len(response),
                response_preview=response[:100].replace("\n", " "),
            ),
        )

        try:
            data = json.loads(response)
            _log_compress.info(
                "EPISODE_JSON_PARSE_OK %s",
                fmtkv(parsed_keys=list(data.keys()) if isinstance(data, dict) else "not_dict"),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            data = None
            _log_compress.warning("EPISODE_JSON_PARSE_FAIL %s", fmtkv(error=str(e)[:100], fallback="legacy"))

        compressed_tokens = await self._async_estimate_text_tokens(response.strip())

        if data is None:
            return Episode(
                episode_range=episode_range,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                key_decisions=[],
                tools_used=tools_used,
                key_findings=[],
                errors_encountered=[],
                current_plan=response.strip(),
                original_event_refs=refs,
                title="Legacy summary (LLM non-JSON)",
                summary=response.strip()[:500],
                importance_score=self._compute_episode_importance(state),
                format="legacy",
            )

        title = data.get("title", "") or "Episode summary"
        summary_text = data.get("summary", "") or ""
        key_decisions = data.get("key_decisions", [])
        if not key_decisions:
            key_decisions = ["No explicit decisions recorded"]

        return Episode(
            episode_range=episode_range,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            key_decisions=key_decisions,
            tools_used=data.get("tools_used", tools_used),
            key_findings=data.get("key_findings", []),
            errors_encountered=data.get("errors_encountered", errors),
            current_plan=data.get("current_plan"),
            original_event_refs=refs,
            title=title,
            summary=summary_text,
            importance_score=self._compute_episode_importance(state),
            format="structured",
        )

    def _compute_episode_importance(self, state: RunState) -> float:
        """Compute overall episode importance from constituent events."""
        if not state.thought_history and not state.tool_results:
            return 0.0
        scores = []
        for t in state.thought_history:
            scores.append(self._score_event_importance(t))
        for tr in state.tool_results:
            scores.append(self._score_event_importance(tr))
        return round(sum(scores) / len(scores), 2) if scores else 0.0

    async def try_checkpoint(self, run_id: str, iteration: int, state: RunState) -> None:
        """Write ContextCheckpointed every N iterations."""
        if iteration <= 0 or iteration % self.checkpoint_interval != 0:
            return

        token_count = await self._async_estimate_context_tokens(state)

        _log_checkpoint.info(
            "CHECKPOINT_WRITE %s",
            fmtkv(
                run_id=run_id,
                iteration=iteration,
                seq=state.seq,
                token_count=token_count,
                thoughts=len(state.thought_history),
                results=len(state.tool_results),
            ),
        )
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
