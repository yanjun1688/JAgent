"""S05 — DeliveryContract 收敛（单一契约模型 + 事件 + fold）测试。

覆盖 D-02（方案 A+B 并存来源）、D-04（空契约 unverified）、D-05（多交付物）、
C-01（单一模型 + provenance）、问题一（intent 无结构化落地）。
"""

from __future__ import annotations


from harness.core.fold import fold_events
from harness.models.events import Event, EventType, RunStartedPayload
from harness.models.intent import DeliveryContract, DeliverySource, UserIntent, validate_delivery_contract_input
from harness.storage.event_store import EventStore


# ── 模型 ─────────────────────────────────────────────────────────


def test_delivery_contract_stable_contract_id():
    a = DeliveryContract(tool="file_op", input={"operation": "write", "path": "x.txt"})
    b = DeliveryContract(tool="file_op", input={"operation": "write", "path": "x.txt"})
    assert a.contract_id == b.contract_id
    assert len(a.contract_id) == 16


def test_delivery_contract_source_defaults_extracted():
    c = DeliveryContract(tool="file_op", input={})
    assert c.source == DeliverySource.EXTRACTED


def test_delivery_contract_validation_is_tool_specific():
    from harness.tools.file_op import FileOpTool

    file_def = FileOpTool().to_definition()
    assert validate_delivery_contract_input("file_op", {"operation": "write"}, file_def)
    assert validate_delivery_contract_input("file_op", {"operation": "write", "path": "x.txt"}, file_def) == []


def test_source_distinguishes_caller_extracted():
    caller = DeliveryContract(tool="file_op", input={}, source=DeliverySource.CALLER)
    extracted = DeliveryContract(tool="file_op", input={}, source=DeliverySource.EXTRACTED)
    assert caller.source == DeliverySource.CALLER
    assert extracted.source == DeliverySource.EXTRACTED


def test_multiple_contracts_supported():
    contracts = [
        DeliveryContract(tool="file_op", input={"operation": "write", "path": "a.txt"}, source=DeliverySource.CALLER),
        DeliveryContract(tool="file_op", input={"operation": "read", "path": "a.txt"}, source=DeliverySource.CALLER),
    ]
    ui = UserIntent(raw="write then read a.txt", contracts=contracts)
    assert len(ui.contracts) == 2
    assert [c.source for c in ui.contracts] == [DeliverySource.CALLER, DeliverySource.CALLER]


def test_user_intent_raw_immutable_field():
    ui = UserIntent(raw="create blackbox.txt and read it")
    assert ui.raw == "create blackbox.txt and read it"
    assert ui.contracts == []


def test_delivery_contract_after_field_removed():
    """Q-05: `after` 字段已移除 — 契约不再承载时序职责（时序归 depends_on）。"""
    c = DeliveryContract(tool="file_op", input={"operation": "write", "path": "x.txt"})
    assert not hasattr(c, "after")


def test_fold_replays_historical_contract_with_after_field():
    """Q-05: 历史事件流中契约残留 `after` 字段 → fold 正常、不报错（无需数据迁移）。"""
    payload = RunStartedPayload(
        intent="write then read",
        intent_raw="write x.txt then read it",
    ).model_dump()
    payload["contracts"] = [
        {"tool": "file_op", "input": {"operation": "write", "path": "x.txt"}, "after": ["dep"]},
        {"tool": "file_op", "input": {"operation": "read", "path": "x.txt"}, "after": ["dep"]},
    ]
    state = fold_events([_event("r1", 1, payload)])
    assert len(state.delivery_contracts) == 2
    assert state.delivery_contracts[0].tool == "file_op"
    assert state.delivery_contracts[0].input["path"] == "x.txt"


# ── RunStarted 事件 round-trip ───────────────────────────────────


def test_run_started_payload_roundtrip():
    contract = DeliveryContract(
        tool="file_op", input={"operation": "write", "path": "x.txt"}, source=DeliverySource.CALLER
    )
    payload = RunStartedPayload(intent="intent", intent_raw="raw", contracts=[contract])
    dumped = payload.model_dump()
    restored = RunStartedPayload(**dumped)
    assert restored.intent_raw == "raw"
    assert len(restored.contracts) == 1
    assert restored.contracts[0].tool == "file_op"
    assert restored.contracts[0].source == DeliverySource.CALLER


def test_run_started_payload_defaults_empty_contracts():
    payload = RunStartedPayload(intent="i")
    assert payload.intent_raw is None
    assert payload.contracts == []


# ── fold 折叠 ────────────────────────────────────────────────────


async def _make_store():
    store = EventStore(":memory:")
    await store.initialize()
    return store


def _event(run_id: str, seq: int, payload: dict) -> Event:
    return Event(
        run_id=run_id,
        seq=seq,
        event_type=EventType.RUN_STARTED,
        payload=payload,
        created_at=0.0,
    )


def test_fold_folds_contracts():
    contracts = [
        DeliveryContract(tool="file_op", input={"operation": "write", "path": "a.txt"}, source=DeliverySource.CALLER),
        DeliveryContract(tool="file_op", input={"operation": "read", "path": "a.txt"}, source=DeliverySource.EXTRACTED),
    ]
    payload = RunStartedPayload(intent="write and read", intent_raw="write a.txt then read", contracts=contracts)
    state = fold_events([_event("r1", 1, payload.model_dump())])
    assert state.intent_raw == "write a.txt then read"
    assert len(state.delivery_contracts) == 2
    assert state.delivery_contracts[0].source == DeliverySource.CALLER


def test_fold_falls_back_to_intent_when_no_intent_raw():
    payload = RunStartedPayload(intent="plain intent")
    state = fold_events([_event("r1", 1, payload.model_dump())])
    assert state.intent_raw == "plain intent"
    assert state.delivery_contracts == []


async def test_event_store_roundtrip_contracts():
    store = await _make_store()
    contract = DeliveryContract(tool="file_op", input={"operation": "read", "path": "a.txt"})
    payload = RunStartedPayload(intent="read a.txt", intent_raw="read a.txt", contracts=[contract])
    await store.append_event("r1", EventType.RUN_STARTED, payload.model_dump())
    events = await store.get_events("r1")
    state = fold_events(events)
    assert state.intent_raw == "read a.txt"
    assert len(state.delivery_contracts) == 1
    assert state.delivery_contracts[0].input["path"] == "a.txt"
    await store.close()


def test_empty_contracts_request_marks_unverified_ready():
    """D-04: 空契约 → contracts=[]，字段就位（判定逻辑在 S06）。"""
    payload = RunStartedPayload(intent="no contract")
    state = fold_events([_event("r1", 1, payload.model_dump())])
    assert state.delivery_contracts == []
