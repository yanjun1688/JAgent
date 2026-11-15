"""I7 · 事件序：全局单调、从 1 连续、无空洞。

规格依据：
- §3.1 Event Log 关键字段 ``seq``；append-only；
- store.py「seq 全局单调」；
- §3.3 重放一致依赖 seq 连续（否则折叠顺序不确定）。
"""

from __future__ import annotations


from jagent.contracts import EventDraft, EventType
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer


def test_two_hundred_events_have_contiguous_seq(harness) -> None:
    for index in range(200):
        harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": f"h{index}"})

    events = harness.store.read()

    assert len(events) == 200
    assert [event.seq for event in events] == list(range(1, 201))


def test_three_hundred_events_have_no_gaps_and_unique_ids(harness) -> None:
    # Task 要求 200 条；这里再压一轮批量写，确认空洞不会出现在批量路径上。
    for index in range(200):
        harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": f"a{index}"})
    harness.store.append(
        [
            EventDraft(EventType.TURN_RECEIVED, {"text_hash": f"b{index}"}, "s_redteam")
            for index in range(100)
        ]
    )

    events = harness.store.read()
    seqs = [event.seq for event in events]
    ids = [event.event_id for event in events]

    assert seqs == list(range(1, 301))
    assert len(set(ids)) == 300


def test_since_seq_read_returns_a_suffix_without_duplicates(harness) -> None:
    """回归护栏：增量读（since_seq>0）不得按整链校验而误报篡改。"""
    for index in range(50):
        harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": f"h{index}"})

    tail = harness.store.read(since_seq=40)

    assert [event.seq for event in tail] == list(range(41, 51))


def test_sequence_survives_reopen_and_keeps_growing(harness) -> None:
    for index in range(10):
        harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": f"h{index}"})

    other = EventStore(harness.db_path)
    try:
        assert other.max_seq() == 10
        other.append([EventDraft(EventType.TURN_RECEIVED, {"text_hash": "after-reopen"}, "s_redteam")])
        assert other.max_seq() == 11
    finally:
        other.close()

    assert [event.seq for event in harness.store.read()][-1] == 11


def test_seq_is_global_across_sessions(harness) -> None:
    second = Writer(harness.store, harness.world, "s_other")
    harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": "one"})
    second.emit(EventType.TURN_RECEIVED, {"text_hash": "two"})
    harness.writer.emit(EventType.TURN_RECEIVED, {"text_hash": "three"})

    assert [event.seq for event in harness.store.read()] == [1, 2, 3]
    only_other = harness.store.read(session_id="s_other")
    assert [event.seq for event in only_other] == [2]
