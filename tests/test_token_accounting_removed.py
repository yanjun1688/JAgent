"""R4 (v3.5) regression: removal of all token accounting.

Pins the permanent deletion:
- model fields: token_count / original_tokens / compressed_tokens /
  pruned_token_count and the two analysis aggregate fields are gone;
- FeedbackCategory.TOKEN_WARNING is gone;
- analysis service no longer sums token_count and the monitor no longer
  carries token-overuse detection;
- episodic summarization output cap is the EPISODE_MAX_TOKENS constant;
- behaviorally: a long thought injects no feedback, and an archived event
  carries no token-shaped keys and survives a payload round-trip.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from harness.core.context_manager import EPISODE_MAX_TOKENS, ContextManager
from harness.core.fold import RunState, ThoughtEntry, fold_events
from harness.core.token_counter import HeuristicTokenCounter
from harness.models.events import (
    AgentThoughtPayload,
    ContextCheckpointedPayload,
    ContextPrunedPayload,
    Episode,
    EpisodeArchivedPayload,
    FeedbackCategory,
    EventType,
)
from harness.monitoring.run_monitor import RunMonitor
from harness.storage.event_store import EventStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
ANALYSIS_SERVICE_SRC = (ROOT / "harness/analysis/service.py").read_text(encoding="utf-8")
MONITOR_SRC = (ROOT / "harness/monitoring/run_monitor.py").read_text(encoding="utf-8")
CONTEXT_MANAGER_SRC = (ROOT / "harness/core/context_manager.py").read_text(encoding="utf-8")


def _contains_token_key(obj: object) -> bool:
    if isinstance(obj, dict):
        return any("token" in str(k) or _contains_token_key(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_token_key(v) for v in obj)
    return False


class TestModelFieldsRemoved:
    @pytest.mark.parametrize(
        ("model", "field"),
        [
            (AgentThoughtPayload, "token_count"),
            (Episode, "original_tokens"),
            (Episode, "compressed_tokens"),
            (EpisodeArchivedPayload, "original_tokens"),
            (EpisodeArchivedPayload, "compressed_tokens"),
            (ContextPrunedPayload, "pruned_token_count"),
            (ContextCheckpointedPayload, "token_count"),
        ],
    )
    def test_field_gone(self, model, field):
        assert field not in model.model_fields

    def test_analysis_fields_gone(self):
        from harness.analysis.schemas import DashboardOverview, RunAnalysisSummary

        assert "total_tokens_consumed" not in DashboardOverview.model_fields
        assert "total_tokens" not in RunAnalysisSummary.model_fields

    def test_feedback_enum_member_gone(self):
        assert not hasattr(FeedbackCategory, "TOKEN_WARNING")
        assert "token_warning" not in {c.value for c in FeedbackCategory}


class TestSourceSeamsRemoved:
    def test_analysis_service_has_no_token_sum(self):
        assert "$.token_count" not in ANALYSIS_SERVICE_SRC
        assert "token_sum" not in ANALYSIS_SERVICE_SRC

    def test_monitor_has_no_token_detection(self):
        assert "_token_totals" not in MONITOR_SRC
        assert "token_warning" not in MONITOR_SRC
        assert "HEURISTIC_CHARS" not in MONITOR_SRC

    def test_monitor_source_parses(self):
        ast.parse(MONITOR_SRC)

    def test_episode_max_tokens_constant(self):
        assert EPISODE_MAX_TOKENS == 4096
        assert "max_tokens=EPISODE_MAX_TOKENS" in CONTEXT_MANAGER_SRC


class TestBehaviorRemoved:
    @pytest.mark.asyncio
    async def test_long_thought_injects_no_feedback(self):
        async with EventStore(":memory:") as store:
            monitor = RunMonitor(store)
            monitor.attach()
            await store.append_event(
                "run1",
                EventType.AGENT_THOUGHT,
                AgentThoughtPayload(thought="word " * 5000).model_dump(),
            )
            state = fold_events(await store.get_events("run1"))
            assert state.feedbacks == []
            snapshot = monitor.get_state("run1")
            assert "estimated_tokens" not in snapshot
            assert "token_warning_sent" not in snapshot
            assert "config" not in monitor.get_state()

    @pytest.mark.asyncio
    async def test_archived_event_has_no_token_keys_and_round_trips(self):
        async with EventStore(":memory:") as store:
            cm = ContextManager(
                store=store,
                token_counter=HeuristicTokenCounter(),
                token_limit=100,
            )
            state = RunState(run_id="r1")
            state.thought_history = [
                ThoughtEntry(seq=1, thought="x" * 400),
                ThoughtEntry(seq=2, thought="y" * 400),
            ]
            await cm.maybe_compress("r1", iteration=1, state=state)
            events = await store.get_events("r1")
            archived = [e for e in events if e.event_type == EventType.CONTEXT and e.phase == "archived"]
            assert len(archived) == 1
            payload = archived[0].payload
            assert not _contains_token_key(payload)

            validated = EpisodeArchivedPayload.model_validate(payload)
            assert validated.model_dump() == EpisodeArchivedPayload.model_validate(validated.model_dump()).model_dump()
