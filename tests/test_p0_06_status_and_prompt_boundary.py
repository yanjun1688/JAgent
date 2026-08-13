"""Regression coverage for JAGENT-2026-P0-06.

These tests assert the trusted boundary, rather than matching a particular UI:
the event stream stores the current request, and conversation messages receive
only user-facing terminal text.
"""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from harness.api.app import HarnessAPI, app, get_hapi
from harness.core.llm_client import MockLLMClient
from harness.models.events import (
    ConversationStartedPayload,
    EventType,
    RunFailedPayload,
    RunStartedPayload,
)
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor


async def _make_api() -> tuple[HarnessAPI, EventStore]:
    store = EventStore(":memory:")
    await store.initialize()
    api = HarnessAPI(store=store, executor=ToolExecutor(store))
    app.dependency_overrides[get_hapi] = lambda: api
    return api, store


async def test_follow_up_persists_only_current_request() -> None:
    api, store = await _make_api()
    try:
        created = await store.upsert_conversation("conv-p0", "P0")
        assert created is None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/conversations/conv-p0/messages",
                json={"message": "检查第二个请求"},
            )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        event = (await store.get_events(run_id))[0]
        assert event.payload["intent"] == "检查第二个请求"
        assert event.payload["current_request"] == "检查第二个请求"
        assert "Previous conversation:" not in event.payload["intent"]
    finally:
        app.dependency_overrides.clear()
        await store.close()


async def test_follow_up_client_request_id_is_idempotent() -> None:
    api, store = await _make_api()
    try:
        await store.upsert_conversation("conv-p0-idem", "P0")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                "/api/v1/conversations/conv-p0-idem/messages",
                json={"message": "只执行一次", "client_request_id": "click-1"},
            )
            second = await client.post(
                "/api/v1/conversations/conv-p0-idem/messages",
                json={"message": "只执行一次", "client_request_id": "click-1"},
            )
        assert first.json()["run_id"] == second.json()["run_id"]
        messages = await store.get_events_for_conversation("conv-p0-idem")
        assert len([e for e in messages if e.event_type == EventType.CONVERSATION_MESSAGE]) == 1
    finally:
        app.dependency_overrides.clear()
        await store.close()


async def test_follow_up_client_request_id_is_idempotent_concurrently() -> None:
    """Concurrent retries must claim one run before either request proceeds."""
    api, store = await _make_api()
    try:
        await store.upsert_conversation("conv-p0-race", "P0")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/v1/conversations/conv-p0-race/messages",
                        json={"message": "只执行一次", "client_request_id": "race-1"},
                    )
                    for _ in range(2)
                ]
            )
        assert responses[0].json()["run_id"] == responses[1].json()["run_id"]
        runs = await store.list_runs(limit=20, offset=0)
        assert len([run for run in runs if run["run_id"] == responses[0].json()["run_id"]]) == 1
        messages = await store.get_events_for_conversation("conv-p0-race")
        assert len([e for e in messages if e.event_type == EventType.CONVERSATION_MESSAGE]) == 1
    finally:
        app.dependency_overrides.clear()
        await store.close()


async def test_failed_run_writes_safe_user_facing_message() -> None:
    api, store = await _make_api()
    try:
        await store.upsert_conversation("conv-p0-fail", "P0")
        await store.append_event(
            "conv-p0-fail",
            EventType.CONVERSATION_STARTED,
            ConversationStartedPayload(conversation_id="conv-p0-fail", title="P0").model_dump(),
        )
        await store.append_event(
            "run-p0-fail",
            EventType.RUN_STARTED,
            RunStartedPayload(intent="读取报告", conversation_id="conv-p0-fail").model_dump(),
        )
        await store.append_event(
            "run-p0-fail",
            EventType.RUN_FAILED,
            RunFailedPayload(
                final_error="Steps failed: s1: missing file",
                event_count=2,
                result_summary="DAG execution: 0/1 step(s) completed, 1 tool call(s). Steps not achieved: s1. Task terminated.",
            ).model_dump(),
        )
        await api._write_assistant_message("run-p0-fail")
        messages = await store.get_events_for_conversation("conv-p0-fail")
        assistant = next(e for e in messages if e.event_type == EventType.CONVERSATION_MESSAGE)
        content = assistant.payload["content"]
        assert content == "任务未能完成，请检查任务要求或稍后重试。"
        for leaked in ("DAG execution:", "Steps not achieved:", "Task terminated.", "tool call(s)", "s1"):
            assert leaked not in content
    finally:
        app.dependency_overrides.clear()
        await store.close()


async def test_mock_llm_records_run_id_for_request_correlation() -> None:
    client = MockLLMClient(["ok"])
    await client.chat([{"role": "system", "content": "x"}], run_id="run-p0")
    assert client.calls[0]["run_id"] == "run-p0"


async def test_fail_maps_cancel_and_confirmation_user_messages() -> None:
    """Regression for the _fail user-facing message mapping (P0-06)."""
    store = EventStore(":memory:")
    await store.initialize()
    try:
        from harness.core.scheduler.base import BaseScheduler
        from harness.tools.executor import ToolExecutor

        class _Scheduler(BaseScheduler):
            scheduler_mode = "planning"

            async def _run_loop(self, run_id, intent, conversation_context=""):
                raise NotImplementedError

        executor = ToolExecutor(store)
        scheduler = _Scheduler(store=store, executor=executor, tool_defs=[], tool_fns={})

        for run_id in ("run-cancel", "run-confirm", "run-other"):
            await store.append_event(
                run_id,
                EventType.RUN_STARTED,
                RunStartedPayload(intent="t").model_dump(),
            )
        await scheduler._fail("run-cancel", "Run cancelled by user request")
        await scheduler._fail("run-confirm", "Tool requires confirmation")
        await scheduler._fail("run-other", "LLM returned malformed output")

        cancel_ev = next(e for e in await store.get_events("run-cancel") if e.event_type == EventType.RUN_FAILED)
        confirm_ev = next(e for e in await store.get_events("run-confirm") if e.event_type == EventType.RUN_FAILED)
        other_ev = next(e for e in await store.get_events("run-other") if e.event_type == EventType.RUN_FAILED)
        assert cancel_ev.payload["user_facing_message"] == "任务已取消。"
        assert confirm_ev.payload["user_facing_message"] == "任务因未获得必要确认而未能完成。"
        assert other_ev.payload["user_facing_message"] == "任务未能完成，请检查任务要求或稍后重试。"
        # Internal error detail must not leak into the user-facing message.
        assert cancel_ev.payload["final_error"] == "Run cancelled by user request"
    finally:
        await store.close()
