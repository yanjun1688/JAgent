from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient

from harness.api.app import HarnessAPI, app, get_hapi
from harness.models.conversation import (
    Conversation,
    ConversationDetail,
    ConversationMessageItem,
    CreateConversationRequest,
    SendMessageRequest,
    UpdateConversationRequest,
    _build_conversation_context,
)
from harness.models.events import (
    ConversationMessagePayload,
    ConversationStartedPayload,
    EventType,
    RunStartedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


class TestConversationModels:
    def test_conversation(self):
        now = time.time()
        c = Conversation(conversation_id="c1", title="Test", created_at=now, updated_at=now)
        assert c.conversation_id == "c1"
        assert c.user_id == "default"
        assert c.title == "Test"
        assert c.status == "active"
        assert c.message_count == 0

    def test_conversation_message_item(self):
        now = time.time()
        m = ConversationMessageItem(seq=1, run_id="r1", role="user", content="hello", created_at=now, status="completed")
        assert m.run_id == "r1"
        assert m.role == "user"
        assert m.content == "hello"
        assert m.seq == 1

    def test_conversation_detail(self):
        now = time.time()
        c = Conversation(conversation_id="c1", title="Test", created_at=now, updated_at=now)
        m = ConversationMessageItem(seq=1, run_id="r1", role="user", content="hi", created_at=now, status="completed")
        d = ConversationDetail(conversation=c, messages=[m])
        assert d.conversation.title == "Test"
        assert len(d.messages) == 1

    def test_create_conversation_request_model(self):
        r = CreateConversationRequest(title="My Chat")
        assert r.title == "My Chat"

    def test_create_conversation_request_default(self):
        r = CreateConversationRequest()
        assert r.title is None

    def test_send_message_request_model(self):
        r = SendMessageRequest(message="How are you?")
        assert r.message == "How are you?"

    def test_update_conversation_request_model(self):
        r = UpdateConversationRequest(title="New title")
        assert r.title == "New title"
        assert r.status is None


class TestConversationStore:
    async def test_upsert_creates(self, store: EventStore):
        await store.upsert_conversation("conv-1", "Test")
        c = await store.get_conversation("conv-1")
        assert c is not None
        assert c["conversation_id"] == "conv-1"
        assert c["title"] == "Test"
        assert c["status"] == "active"
        assert c["message_count"] == 0

    async def test_upsert_idempotent(self, store: EventStore):
        await store.upsert_conversation("conv-1", "First")
        await store.upsert_conversation("conv-1", "Second")
        c = await store.get_conversation("conv-1")
        assert c["title"] == "Second"

    async def test_list_conversations(self, store: EventStore):
        await store.upsert_conversation("conv-a", "A")
        await store.upsert_conversation("conv-b", "B")
        rows = await store.list_conversations(limit=10, offset=0)
        ids = [r["conversation_id"] for r in rows]
        assert "conv-a" in ids
        assert "conv-b" in ids

    async def test_list_conversations_limit_offset(self, store: EventStore):
        for i in range(5):
            await store.upsert_conversation(f"conv-{i}", f"Title {i}")
        rows = await store.list_conversations(limit=3, offset=0)
        assert len(rows) == 3

    async def test_get_conversation_not_found(self, store: EventStore):
        c = await store.get_conversation("nonexistent")
        assert c is None

    async def test_delete_conversation(self, store: EventStore):
        await store.upsert_conversation("conv-1", "Test")
        await store.delete_conversation("conv-1")
        c = await store.get_conversation("conv-1")
        assert c is None or c["status"] != "active"

    async def test_update_conversation(self, store: EventStore):
        await store.upsert_conversation("conv-1", "Old")
        ok = await store.update_conversation("conv-1", title="New")
        assert ok
        c = await store.get_conversation("conv-1")
        assert c["title"] == "New"

    async def test_increment_message_count(self, store: EventStore):
        await store.upsert_conversation("conv-1", "Test")
        await store.increment_message_count("conv-1")
        await store.increment_message_count("conv-1")
        c = await store.get_conversation("conv-1")
        assert c["message_count"] == 2

    async def test_get_events_for_conversation(self, store: EventStore):
        await store.append_event(
            "conv-1",
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id="conv-1", title="T").model_dump(),
        )
        await store.append_event(
            "conv-1",
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(conversation_id="conv-1", run_id="r1", role="user", content="hello").model_dump(),
        )
        events = await store.get_events_for_conversation("conv-1")
        assert len(events) == 2
        types = [e.event_type for e in events]
        assert EventType.CONVERSATION_STARTED in types
        assert EventType.CONVERSATION_MESSAGE in types

    async def test_total_conversation_count(self, store: EventStore):
        await store.upsert_conversation("c1", "A")
        await store.upsert_conversation("c2", "B")
        total = await store.total_conversation_count()
        assert total == 2


class TestConversationContextBuilder:
    async def test_empty_context(self, store: EventStore):
        await store.upsert_conversation("conv-1", "Test")
        ctx = await _build_conversation_context(store, "conv-1")
        assert ctx == ""

    async def test_with_messages(self, store: EventStore):
        await store.upsert_conversation("conv-1", "Test")
        await store.append_event(
            "conv-1",
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id="conv-1", title="T").model_dump(),
        )
        await store.append_event(
            "conv-1",
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(conversation_id="conv-1", run_id="r1", role="user", content="Hello").model_dump(),
        )
        await store.append_event(
            "conv-1",
            EventType.CONVERSATION_MESSAGE,
            ConversationMessagePayload(
                conversation_id="conv-1", run_id="r1", role="assistant", content="Hi there!"
            ).model_dump(),
        )
        ctx = await _build_conversation_context(store, "conv-1")
        assert "[user]" in ctx
        assert "Hello" in ctx
        assert "[assistant]" in ctx
        assert "Hi there!" in ctx


@pytest.fixture
async def api():
    store = EventStore(":memory:")
    await store.initialize()
    executor = ToolExecutor(store)
    hapi = HarnessAPI(store=store, executor=executor)
    app.dependency_overrides[get_hapi] = lambda: hapi
    yield hapi, store
    app.dependency_overrides.clear()
    await store.close()


@pytest.fixture
def client(api):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestConversationAPI:
    async def test_create_conversation(self, client, api):
        resp = await client.post("/api/v1/conversations", json={"title": "My Chat"})
        assert resp.status_code == 201
        data = resp.json()
        assert "conversation_id" in data
        assert data["title"] == "My Chat"

    async def test_create_conversation_no_body(self, client, api):
        resp = await client.post("/api/v1/conversations")
        assert resp.status_code == 201
        data = resp.json()
        assert "conversation_id" in data
        assert data["title"] == "New conversation"

    async def test_list_conversations_empty(self, client, api):
        resp = await client.get("/api/v1/conversations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversations"] == []
        assert data["total"] == 0

    async def test_list_conversations(self, client, api):
        await client.post("/api/v1/conversations", json={"title": "Chat 1"})
        await client.post("/api/v1/conversations", json={"title": "Chat 2"})
        resp = await client.get("/api/v1/conversations")
        data = resp.json()
        assert len(data["conversations"]) >= 2
        assert data["total"] >= 2

    async def test_get_conversation(self, client, api):
        r = await client.post("/api/v1/conversations", json={"title": "Test"})
        cid = r.json()["conversation_id"]
        resp = await client.get(f"/api/v1/conversations/{cid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversation"]["title"] == "Test"
        assert "messages" in data

    async def test_get_conversation_not_found(self, client, api):
        resp = await client.get("/api/v1/conversations/nonexistent")
        assert resp.status_code == 404

    async def test_send_message_user(self, client, api):
        r = await client.post("/api/v1/conversations", json={"title": "Test"})
        cid = r.json()["conversation_id"]
        resp = await client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={"message": "Hello world"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        assert data["conversation_id"] == cid

    async def test_send_message_not_found(self, client, api):
        resp = await client.post(
            "/api/v1/conversations/nonexistent/messages",
            json={"message": "Hi"},
        )
        assert resp.status_code == 404

    async def test_delete_conversation(self, client, api):
        r = await client.post("/api/v1/conversations", json={"title": "ToDelete"})
        cid = r.json()["conversation_id"]
        resp = await client.delete(f"/api/v1/conversations/{cid}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_delete_not_found(self, client, api):
        resp = await client.delete("/api/v1/conversations/nonexistent")
        assert resp.status_code == 404

    async def test_update_conversation(self, client, api):
        r = await client.post("/api/v1/conversations", json={"title": "Old"})
        cid = r.json()["conversation_id"]
        resp = await client.patch(
            f"/api/v1/conversations/{cid}",
            json={"title": "New Title"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    async def test_update_not_found(self, client, api):
        resp = await client.patch(
            "/api/v1/conversations/nonexistent",
            json={"title": "X"},
        )
        assert resp.status_code == 404
