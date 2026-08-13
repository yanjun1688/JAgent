"""P0-04 Bug case coverage: Conversation 事件与 Run 事件共享 run_id 列空间.

验证两个关键场景：
1. 创建 conversation -> 发消息 -> run 结束。查询 WHERE conversation_id = ?
   返回所有关联事件（ConversationStarted + 用户消息 + RunStarted +
   AgentThought + assistant 消息）
2. 两个不同 conversation 下的 Run 事件在 conversation_id 列上正确隔离。

不修改存量 test_conversation.py::test_get_events_for_conversation —— 该测试
仅断言 legacy 行为（2 条 conversation 级事件），保留以防回归。
"""

from __future__ import annotations

import pytest

from harness.models.events import (
    AgentThoughtPayload,
    ConversationMessagePayload,
    ConversationStartedPayload,
    EventType,
    RunStartedPayload,
)
from harness.storage.event_store import EventStore


@pytest.fixture
async def store():
    store = EventStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


class TestConversationIdColumn:
    """Bug case 1: cross-run timeline assembled under conversation_id column."""

    async def test_conversation_timeline_includes_run_events(self, store: EventStore):
        import asyncio

        # 1. Create conversation: writes ConversationStarted under run_id = conv_id
        conv_id = "conv-1"
        await store.append_event(
            conv_id,
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id=conv_id, title="T").model_dump(),
        )
        await asyncio.sleep(0.001)

        # 2. User message on the conversation stream
        run_id = "run-abc12345"
        await store.append_event(
            conv_id,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=conv_id, run_id=run_id, role="user", content="hello"
            ).model_dump(),
        )
        await asyncio.sleep(0.001)

        # 3. RunStarted carries conversation_id in payload; store should write
        #    both cache and column.
        await store.append_event(
            run_id,
            EventType.RUN_STARTED,
            RunStartedPayload(intent="do X", conversation_id=conv_id).model_dump(),
        )
        await asyncio.sleep(0.001)

        # 4. Subsequent run event WITHOUT conversation_id in payload: store must
        #    fall back to run-level cache so the column is still filled.
        await store.append_event(
            run_id,
            EventType.AGENT_THOUGHT,
            AgentThoughtPayload(thought="thinking", token_count=10).model_dump(),
        )
        await asyncio.sleep(0.001)

        # 5. Assistant message closes the conversation timeline
        await store.append_event(
            conv_id,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=conv_id, run_id=run_id, role="assistant", content="done"
            ).model_dump(),
        )

        # Fetch the full conversation timeline
        events = await store.get_events_for_conversation(conv_id)

        # Expected: all 5 events returned, in created_at order
        types = [e.event_type for e in events]
        assert types == [
            EventType.CONVERSATION_STARTED,
            EventType.CONVERSATION_MESSAGE,
            EventType.RUN_STARTED,
            EventType.AGENT_THOUGHT,
            EventType.CONVERSATION_MESSAGE,
        ], f"timeline order mismatch: {types}"

    async def test_run_events_inherit_conversation_id_via_cache(self, store: EventStore):
        """After RunStarted sets the mapping, later run events pull from cache."""
        conv_id = "conv-X"
        run_id = "run-multi1"

        await store.append_event(
            run_id,
            EventType.RUN_STARTED,
            RunStartedPayload(intent="i", conversation_id=conv_id).model_dump(),
        )
        # AGENT_THOUGHT has no conversation_id field; cache must fill the column.
        await store.append_event(
            run_id,
            EventType.AGENT_THOUGHT,
            AgentThoughtPayload(thought="t", token_count=5).model_dump(),
        )

        # Append a conversation-level assistant message to ensure timeline lookup hits
        await store.append_event(
            conv_id,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=conv_id, run_id=run_id, role="assistant", content="ok"
            ).model_dump(),
        )

        events = await store.get_events_for_conversation(conv_id)
        # RunStarted + AgentThought + assistant message = 3 events
        assert len(events) == 3
        types = [e.event_type for e in events]
        assert EventType.AGENT_THOUGHT in types, (
            "AGENT_THOUGHT should be associated to conversation via cache -> column"
        )


class TestConversationIsolation:
    """Bug case 2: two conversations correctly separated in conversation_id column."""

    async def test_two_conversations_are_isolated(self, store: EventStore):
        conv_a = "conv-a"
        conv_b = "conv-b"
        run_a = "run-aaa12345"
        run_b = "run-bbb12345"

        # Conversation A timeline
        await store.append_event(
            conv_a,
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id=conv_a, title="A").model_dump(),
        )
        await store.append_event(
            conv_a,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=conv_a, run_id=run_a, role="user", content="A-user"
            ).model_dump(),
        )
        await store.append_event(
            run_a,
            EventType.RUN_STARTED,
            RunStartedPayload(intent="A", conversation_id=conv_a).model_dump(),
        )
        await store.append_event(
            run_a,
            EventType.AGENT_THOUGHT,
            AgentThoughtPayload(thought="A-thinking", token_count=1).model_dump(),
        )

        # Conversation B timeline
        await store.append_event(
            conv_b,
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id=conv_b, title="B").model_dump(),
        )
        await store.append_event(
            conv_b,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id=conv_b, run_id=run_b, role="user", content="B-user"
            ).model_dump(),
        )
        await store.append_event(
            run_b,
            EventType.RUN_STARTED,
            RunStartedPayload(intent="B", conversation_id=conv_b).model_dump(),
        )
        await store.append_event(
            run_b,
            EventType.AGENT_THOUGHT,
            AgentThoughtPayload(thought="B-thinking", token_count=1).model_dump(),
        )

        events_a = await store.get_events_for_conversation(conv_a)
        events_b = await store.get_events_for_conversation(conv_b)

        # Each conversation sees exactly its own timeline (4 events each).
        assert len(events_a) == 4, f"conv_a timeline size wrong: {len(events_a)}"
        assert len(events_b) == 4, f"conv_b timeline size wrong: {len(events_b)}"

        # No run from B leaks into A's timeline
        run_ids_a = {e.run_id for e in events_a}
        assert run_b not in run_ids_a, "conv_b run leaked into conv_a timeline"
        run_ids_b = {e.run_id for e in events_b}
        assert run_a not in run_ids_b, "conv_a run leaked into conv_b timeline"


class TestListRunsExcludesConversations:
    """list_runs / total_run_count must not contain conversation-level event streams.

    Bug condition: Previously, conversation rows (run_id = conversation_id) leaked
    into list_runs because the only mitigation was a `conv_` prefix convention.

    Filter invariant: events included only if
        conversation_id IS NULL  OR  run_id != conversation_id
    - run events: pass via `run_id != conversation_id`
    - conversation events: run_id == conversation_id, column set -> excluded
    - legacy conversation events: column NULL, run_id == conversation_id ->
      included (acceptable known limitation; new writes always set column)
    """

    async def test_list_runs_excludes_conversation_level_events(self, store: EventStore):
        conv_id = "conv-list1"
        run_id = "run-list123"

        # Conversation events: run_id == conversation_id, column set
        await store.append_event(
            conv_id,
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id=conv_id, title="T").model_dump(),
        )
        await store.append_event(
            conv_id,
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(conversation_id=conv_id, run_id=run_id, role="user", content="hi").model_dump(),
        )

        # Run event: run_id != conversation_id, column set
        await store.append_event(
            run_id,
            EventType.RUN_STARTED,
            RunStartedPayload(intent="i", conversation_id=conv_id).model_dump(),
        )

        rows = await store.list_runs()
        run_ids = [r["run_id"] for r in rows]
        assert run_ids == [run_id], f"list_runs must exclude conversation-level events; got {run_ids}"

        total = await store.total_run_count()
        assert total == 1

    async def test_list_runs_still_lists_runs_without_conversation(self, store: EventStore):
        """A run not associated with any conversation still appears."""
        run_id = "run-standalone"
        await store.append_event(
            run_id,
            EventType.RUN_STARTED,
            RunStartedPayload(intent="solo").model_dump(),
        )
        rows = await store.list_runs()
        assert [r["run_id"] for r in rows] == [run_id]
        assert await store.total_run_count() == 1


class TestLegacyPersistenceCompat:
    """Existing on-disk DBs predate the column; DDL migration adds it as NULL.

    After migration, reads via the new column miss those rows (acceptable per
    design decision A). Conversation-level events written to legacy DBs are
    caught by the `run_id = ?` fallback arm of get_events_for_conversation.
    """

    async def test_legacy_conversation_event_reachable_via_run_id_match(self, store: EventStore):
        """Simulate a legacy conversation event: conversation_id column is NULL
        but its run_id coincides with the conversation_id."""
        conv_id = "conv-legacy"

        # Inject a row that bypasses payload-driven column writing,
        # forcing conversation_id = NULL (simulating legacy data).
        import time as _t

        await store.conn.execute(
            "INSERT INTO events (run_id, seq, event_type, payload, idempotency_key, "
            "created_at, conversation_id) VALUES (?, ?, ?, ?, NULL, ?, NULL)",
            (
                conv_id,
                1,
                EventType.CONVERSATION_STARTED.value,
                ConversationStartedPayload(conversation_id=conv_id, title="L").model_dump_json(),
                _t.time(),
            ),
        )
        await store.conn.commit()

        events = await store.get_events_for_conversation(conv_id)
        # Fallback arm `run_id = ?` catches it despite NULL conversation_id column
        assert len(events) == 1
        assert events[0].event_type == EventType.CONVERSATION_STARTED
