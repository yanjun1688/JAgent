"""S07 — API 契约层（caller 契约透传 + 抽取兜底）测试。

覆盖 D-02（方案 A caller 显式契约 + 方案 B 抽取兜底）、C-01（provenance）、
D-04（抽取失败 → contracts=[] + unverified，不阻断 Run）、幂等重放一致。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from harness.api.app import app, get_hapi
from harness.api.deps import HarnessAPI
from harness.core.fold import fold_events
from harness.core.llm_client import MockLLMClient
from harness.models.events import EventType
from harness.models.intent import DeliverySource
from harness.storage.event_store import EventStore
from harness.tools.executor import ToolExecutor
from harness.tools.file_op import FileOpTool
from harness.tools.registry import ToolRegistry


@pytest.fixture
async def api_and_client():
    store = EventStore(":memory:")
    await store.initialize()
    api = HarnessAPI(store=store, executor=ToolExecutor(store))
    api.llm_client = MockLLMClient(responses=[])
    registry = ToolRegistry()
    # ADR-010: 使用真实 FileOpTool 契约（声明 operations → operation 判别键必填）。
    registry._register(
        FileOpTool().to_definition(),
        lambda x: {"success": True},
    )
    api.registry = registry
    app.dependency_overrides[get_hapi] = lambda: api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield api, store, client
    app.dependency_overrides.clear()
    await store.close()


async def _run_started_event(store, run_id):
    events = await store.get_events(run_id)
    return next(e for e in events if e.event_type == EventType.RUN_STARTED)


async def _wait_for_resolved_event(store, run_id, timeout=5.0):
    """S07: 抽取在后台 scheduler 执行（create_run 不再同步等待）→ 轮询等待事件落库。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        events = await store.get_events(run_id)
        resolved = [e for e in events if e.event_type == EventType.DELIVERY_CONTRACTS_RESOLVED]
        if resolved:
            return resolved[0]
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"DELIVERY_CONTRACTS_RESOLVED not written within {timeout}s for run {run_id}")
        await asyncio.sleep(0.01)


# ── 方案 A：caller 显式契约 ──────────────────────────────────────


async def test_caller_contracts_land_in_run_started(api_and_client):
    api, store, client = api_and_client
    resp = await client.post(
        "/api/v1/runs",
        json={
            "intent": "write and read blackbox.txt",
            "required_operations": [
                {"tool": "file_op", "input": {"operation": "write", "path": "blackbox.txt"}},
                {"tool": "file_op", "input": {"operation": "read", "path": "blackbox.txt"}},
            ],
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    event = await _run_started_event(store, run_id)
    payload = event.payload
    assert payload["intent_raw"] == "write and read blackbox.txt"
    contracts = payload["contracts"]
    assert len(contracts) == 2
    assert all(c["source"] == DeliverySource.CALLER.value for c in contracts)
    assert contracts[0]["input"]["path"] == "blackbox.txt"


async def test_caller_contract_folds_into_run_state(api_and_client):
    api, store, client = api_and_client
    resp = await client.post(
        "/api/v1/runs",
        json={
            "intent": "write x.txt",
            "required_operations": [{"tool": "file_op", "input": {"operation": "write", "path": "x.txt"}}],
        },
    )
    run_id = resp.json()["run_id"]
    state = fold_events(await store.get_events(run_id))
    assert len(state.delivery_contracts) == 1
    assert state.delivery_contracts[0].source == DeliverySource.CALLER
    assert state.intent_raw == "write x.txt"


# ── 方案 B：抽取兜底 ─────────────────────────────────────────────


class _DelayedMockLLMClient(MockLLMClient):
    """Mock 在返回前 sleep，用于验证抽取不阻塞 API 响应。"""

    def __init__(self, responses: list[str], delay: float = 0.2):
        super().__init__(responses)
        self.delay = delay

    async def chat(self, messages, **kwargs):
        await asyncio.sleep(self.delay)
        return await super().chat(messages, **kwargs)


async def test_extraction_fallback_source_extracted(api_and_client):
    api, store, client = api_and_client
    api.llm_client = MockLLMClient(
        responses=[
            json.dumps(
                {
                    "required_operations": [
                        {"tool": "file_op", "input": {"operation": "write", "path": "blackbox.txt"}},
                        {"tool": "file_op", "input": {"operation": "read", "path": "blackbox.txt"}},
                    ]
                }
            )
        ]
    )
    resp = await client.post("/api/v1/runs", json={"intent": "create blackbox.txt and read it"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    event = await _wait_for_resolved_event(store, run_id)
    contracts = event.payload["contracts"]
    assert len(contracts) == 2
    assert all(c["source"] == DeliverySource.EXTRACTED.value for c in contracts)


async def test_create_run_returns_before_extraction_completes(api_and_client):
    """create_run 不等待抽取 — API 响应在抽取完成前返回（异步前置修复）。"""
    api, store, client = api_and_client
    api.llm_client = _DelayedMockLLMClient(
        responses=[
            json.dumps({"required_operations": [{"tool": "file_op", "input": {"operation": "write", "path": "a.txt"}}]})
        ],
        delay=0.3,
    )
    import time as _time

    t0 = _time.monotonic()
    resp = await client.post("/api/v1/runs", json={"intent": "write a.txt"})
    elapsed = _time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 0.25, f"create_run blocked on extraction for {elapsed:.2f}s"

    run_id = resp.json()["run_id"]
    started = await _run_started_event(store, run_id)
    assert started.payload["requires_contract_extraction"] is True
    assert started.payload["contracts"] == []

    event = await _wait_for_resolved_event(store, run_id)
    assert len(event.payload["contracts"]) == 1


async def test_scheduler_resolves_contracts_before_plan(api_and_client):
    """抽取在 scheduler 首轮 plan 前完成 → fold 状态拿到 extracted 契约。"""
    api, store, client = api_and_client
    api.llm_client = MockLLMClient(
        responses=[
            json.dumps({"required_operations": [{"tool": "file_op", "input": {"operation": "write", "path": "b.txt"}}]})
        ]
    )
    resp = await client.post("/api/v1/runs", json={"intent": "write b.txt"})
    run_id = resp.json()["run_id"]
    await _wait_for_resolved_event(store, run_id)
    state = fold_events(await store.get_events(run_id))
    assert len(state.delivery_contracts) == 1
    assert state.delivery_contracts[0].source == DeliverySource.EXTRACTED
    assert state.delivery_contracts[0].input["path"] == "b.txt"


async def test_extraction_timeout_writes_unverified(api_and_client, monkeypatch):
    """抽取超时 → DELIVERY_CONTRACTS_RESOLVED(timed_out, []) → unverified（D-04）。"""
    import harness.core.contract_extractor as ce_mod

    monkeypatch.setattr(ce_mod, "CONTRACT_EXTRACT_TIMEOUT", 0.05)
    api, store, client = api_and_client
    api.llm_client = _DelayedMockLLMClient(
        responses=[
            json.dumps({"required_operations": [{"tool": "file_op", "input": {"operation": "write", "path": "c.txt"}}]})
        ],
        delay=0.3,
    )
    resp = await client.post("/api/v1/runs", json={"intent": "write c.txt"})
    run_id = resp.json()["run_id"]
    event = await _wait_for_resolved_event(store, run_id)
    assert event.payload["timed_out"] is True
    assert event.payload["contracts"] == []


async def test_extraction_invalid_items_dropped(api_and_client):
    api, store, client = api_and_client
    api.llm_client = MockLLMClient(
        responses=[
            json.dumps(
                {
                    "required_operations": [
                        {"tool": "unknown_tool", "input": {"operation": "write"}},
                        {"tool": "file_op", "input": {"operation": "write", "path": "ok.txt"}},
                        {"tool": "file_op", "input": {"path": "missing-op-key.txt"}},
                    ]
                }
            )
        ]
    )
    resp = await client.post("/api/v1/runs", json={"intent": "create files"})
    run_id = resp.json()["run_id"]
    event = await _wait_for_resolved_event(store, run_id)
    contracts = event.payload["contracts"]
    assert len(contracts) == 1
    assert contracts[0]["tool"] == "file_op"
    assert contracts[0]["input"]["path"] == "ok.txt"


async def test_extraction_empty_result_is_unverified_d04(api_and_client):
    api, store, client = api_and_client
    api.llm_client = MockLLMClient(responses=[json.dumps({"required_operations": []})])
    resp = await client.post("/api/v1/runs", json={"intent": "please write x.txt"})
    run_id = resp.json()["run_id"]
    event = await _run_started_event(store, run_id)
    assert event.payload["contracts"] == []


async def test_no_llm_client_extraction_falls_back_to_empty(api_and_client):
    api, store, client = api_and_client
    api.llm_client = None  # 无 LLM → 抽取兜底不可用 → contracts=[]（D-04）
    resp = await client.post("/api/v1/runs", json={"intent": "write x.txt"})
    run_id = resp.json()["run_id"]
    event = await _run_started_event(store, run_id)
    assert event.payload["contracts"] == []


# ── 幂等重放一致 ─────────────────────────────────────────────────


async def test_idempotent_replay_reuses_first_contracts(api_and_client):
    api, store, client = api_and_client
    body = {
        "intent": "write x.txt",
        "conversation_id": "conv-1",
        "client_request_id": "req-1",
        "required_operations": [{"tool": "file_op", "input": {"operation": "write", "path": "x.txt"}}],
    }
    first = await client.post("/api/v1/runs", json=body)
    assert first.status_code == 200
    first_run_id = first.json()["run_id"]

    # 二次提交不同契约 → 以首次为准（claim 已存在 → 返回同一 run_id）
    body["required_operations"] = [{"tool": "file_op", "input": {"operation": "delete", "path": "y.txt"}}]
    replay = await client.post("/api/v1/runs", json=body)
    assert replay.status_code == 200
    assert replay.json()["run_id"] == first_run_id

    events = await store.get_events(first_run_id)
    started = next(e for e in events if e.event_type == EventType.RUN_STARTED)
    contracts = started.payload["contracts"]
    assert contracts[0]["input"]["path"] == "x.txt"


async def test_conversation_caller_contracts_land_in_run_started(api_and_client):
    api, store, client = api_and_client
    conversation = await client.post("/api/v1/conversations", json={"title": "contracts"})
    conversation_id = conversation.json()["conversation_id"]
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "message": "write x.txt",
            "required_operations": [{"tool": "file_op", "input": {"operation": "write", "path": "x.txt"}}],
            "client_request_id": "conversation-contract-1",
        },
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    started = await _run_started_event(store, run_id)
    assert started.payload["contracts"][0]["source"] == "caller"

    replay = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "message": "write x.txt",
            "required_operations": [{"tool": "file_op", "input": {"operation": "delete", "path": "x.txt"}}],
            "client_request_id": "conversation-contract-1",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["run_id"] == run_id
    events = await store.get_events(run_id)
    assert [e for e in events if e.event_type == EventType.DELIVERY_CONTRACTS_RESOLVED] == []


# ── 非法契约请求边界 ─────────────────────────────────────────────


async def test_unknown_tool_contract_rejected(api_and_client):
    api, store, client = api_and_client
    # caller 显式契约是受信输入，未知工具必须在 RunStarted 前拒绝。
    resp = await client.post(
        "/api/v1/runs",
        json={
            "intent": "write x.txt",
            "required_operations": [{"tool": "ghost_tool", "input": {"operation": "write", "path": "x.txt"}}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_delivery_contract"


async def test_missing_operation_key_contract_rejected(api_and_client):
    api, store, client = api_and_client
    resp = await client.post(
        "/api/v1/runs",
        json={
            "intent": "do things",
            "required_operations": [{"tool": "file_op", "input": {}}],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_delivery_contract"
