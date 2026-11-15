"""JAgent v8 · 事件存储（append-only）。

只有 append 和 read，没有 update / delete。
seq 全局单调；digest 覆盖上一条事件的 digest，重放两次必须得到同一个尾 digest。

参考了 harness/layers/kernel/store.py 的做法（sqlite + 事务 + expected_seq 乐观并发），
但表结构按 v8 重写：v8 没有 attempt 表和 reconcile claim 表，事件按 session/run 过滤。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Sequence

from ..contracts import (
    SCHEMA_VERSION,
    Event,
    EventDraft,
    EventType,
    validate_draft,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT    NOT NULL UNIQUE,
    type           TEXT    NOT NULL,
    payload        TEXT    NOT NULL,
    session_id     TEXT    NOT NULL,
    run_id         TEXT,
    step_id        TEXT,
    at             REAL    NOT NULL,
    schema_version INTEGER NOT NULL,
    digest         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
"""


class SeqConflictError(RuntimeError):
    pass


class TamperedHistoryError(RuntimeError):
    """事件日志被改过（哈希链对不上）。只可能是外部写入了文件。"""

    def __init__(self, seq: int) -> None:
        self.seq = seq
        super().__init__(f"event log chain broken at seq {seq}: history was modified")


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- write --
    def append(self, drafts: Sequence[EventDraft], expected_seq: int | None = None) -> list[Event]:
        """原子写一批。expected_seq 不匹配时抛 SeqConflictError（乐观并发）。"""
        if not drafts:
            return []
        for draft in drafts:
            validate_draft(draft)
        # BUG2-10：严格 JSON 预序列化——NaN/inf 在拿锁开事务之前就被拒，
        # 日志里永远不会出现非标准 JSON 字面量（"写得进、投不出"比失败更糟）。
        payload_jsons = [
            json.dumps(draft.payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
            for draft in drafts
        ]

        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN IMMEDIATE")
                if expected_seq is not None:
                    actual = cur.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
                    if actual != expected_seq:
                        raise SeqConflictError(f"expected seq {expected_seq}, actual {actual}")

                row = cur.execute("SELECT seq, digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
                prev_seq, prev_digest = (row[0], row[1]) if row else (0, "")
                now = time.time()
                out: list[Event] = []
                for offset, draft in enumerate(drafts, start=1):
                    seq = prev_seq + offset
                    event_id = uuid.uuid4().hex
                    payload_json = payload_jsons[offset - 1]
                    digest = _digest(prev_digest, seq, draft.type.value, draft.payload)
                    cur.execute(
                        "INSERT INTO events (seq, event_id, type, payload, session_id, run_id, step_id, at,"
                        " schema_version, digest) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            seq, event_id, draft.type.value, payload_json, draft.session_id,
                            draft.run_id, draft.step_id, now, SCHEMA_VERSION, digest,
                        ),
                    )
                    prev_digest = digest
                    out.append(
                        Event(
                            event_id=event_id, seq=seq, type=draft.type, payload=draft.payload,
                            session_id=draft.session_id, run_id=draft.run_id, step_id=draft.step_id,
                            at=now, schema_version=SCHEMA_VERSION, digest=digest,
                        )
                    )
                cur.execute("COMMIT")
                return out
            except Exception:
                cur.execute("ROLLBACK")
                raise

    # ----------------------------------------------------------------- read --
    def read(
        self,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        since_seq: int = 0,
    ) -> list[Event]:
        # BUG2-07：哈希链从第一条锚定，任何过滤读都可能"跳过"被篡改的行。
        # 先取全量、校验整条链，再在内存里过滤——篡改无论从哪个入口读都暴露。
        rows = self._conn.execute(
            "SELECT seq, event_id, type, payload, session_id, run_id, step_id, at,"
            " schema_version, digest FROM events ORDER BY seq"
        ).fetchall()
        events = [
            Event(
                event_id=r[1], seq=r[0], type=EventType(r[2]), payload=json.loads(r[3]),
                session_id=r[4], run_id=r[5], step_id=r[6], at=r[7],
                schema_version=r[8], digest=r[9],
            )
            for r in rows
        ]
        self._verify_chain(events)
        if session_id is not None:
            events = [e for e in events if e.session_id == session_id]
        if run_id is not None:
            events = [e for e in events if e.run_id == run_id]
        if since_seq:
            events = [e for e in events if e.seq > since_seq]
        return events

    @staticmethod
    def _verify_chain(events: list[Event]) -> None:
        """篡改检测：append-only 只是不提供写接口，文件本身还是可改的（红队 RT-06）。"""
        prev = ""
        for event in events:
            expected = _digest(prev, event.seq, event.type.value, event.payload)
            if event.digest != expected:
                raise TamperedHistoryError(event.seq)
            prev = event.digest

    def max_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ ctx --
    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def replay_digest(events: Iterable[Event]) -> str:
    """重放指纹：同一事件流 => 同一 digest。用于「重放两次一致」的断言。"""
    acc = ""
    for event in events:
        acc = _digest(acc, event.seq, event.type.value, event.payload)
    return acc


def _digest(prev: str, seq: int, type_name: str, payload: dict) -> str:
    import hashlib

    body = json.dumps(
        {"prev": prev, "seq": seq, "type": type_name, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
