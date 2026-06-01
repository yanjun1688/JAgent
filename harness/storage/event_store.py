from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

import aiosqlite

from harness.models.events import PAYLOAD_MODEL_MAP, Event, EventType

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    run_id          TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    event_type      TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    idempotency_key TEXT,
    created_at      REAL    NOT NULL,
    PRIMARY KEY (run_id, seq)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_idem
    ON events(run_id, event_type, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


class DuplicateIdempotencyKeyError(Exception):
    """Raised internally when a duplicate idempotency key is detected."""


class SequenceConflictError(Exception):
    """Raised when PK conflict retries are exhausted."""


class EventStore:
    """Append-Only event store backed by SQLite.

    Physical constraints:
    - No UPDATE or DELETE operations on the events table.
    - PRIMARY KEY (run_id, seq) ensures global ordering per run.
    - UNIQUE INDEX on (run_id, event_type, idempotency_key) ensures idempotency.

    Known MVP limitations:
    - seq generation (get_latest_seq + 1) is not atomic; concurrent writers
      on the same run_id may hit the PK constraint. The DB-level PRIMARY KEY
      serves as a last-resort guard. Production should use RETURNING or a
      sequence table.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def __aenter__(self) -> EventStore:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def initialize(self) -> None:
        if self._conn is not None:
            return
        if self.db_path != ":memory:":
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("EventStore not initialized. Call initialize() first.")
        return self._conn

    # ── Core API ───────────────────────────────────────────────

    async def append_event(
        self,
        run_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        max_retries: int = 3,
    ) -> Event:
        """Append a new event to the store.

        Validates payload against the Pydantic model for the event type,
        auto-computes the next seq, and enforces idempotency via unique index.

        Retries on PRIMARY KEY (run_id, seq) conflicts (up to max_retries),
        re-computing latest seq each attempt.
        """
        _validate_payload(event_type, payload)

        if idempotency_key is not None:
            existing = await self.find_by_idempotency_key(run_id, event_type, idempotency_key)
            if existing is not None:
                return existing

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            next_seq = await self.get_latest_seq(run_id) + 1
            created_at = time.time()

            sql = (
                "INSERT INTO events (run_id, seq, event_type, payload, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            )
            payload_json = json.dumps(payload, ensure_ascii=False)

            try:
                await self.conn.execute(
                    sql,
                    (run_id, next_seq, event_type.value, payload_json, idempotency_key, created_at),
                )
                await self.conn.commit()
                return Event(
                    run_id=run_id,
                    seq=next_seq,
                    event_type=event_type,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    created_at=created_at,
                )
            except sqlite3.IntegrityError as exc:
                error_str = str(exc)
                if "UNIQUE constraint" in error_str and "events.idempotency_key" in error_str:
                    existing = await self.find_by_idempotency_key(run_id, event_type, idempotency_key)
                    if existing is not None:
                        return existing
                    raise DuplicateIdempotencyKeyError(
                        f"Duplicate idempotency key '{idempotency_key}' for run '{run_id}'"
                    ) from exc
                if "PRIMARY KEY" in error_str or "events.run_id" in error_str.lower():
                    last_error = exc
                    continue
                raise

        raise SequenceConflictError(
            f"PK conflict on (run_id='{run_id}', seq) after {max_retries} retries"
        ) from last_error

    async def get_events(self, run_id: str) -> list[Event]:
        """Return all events for a run, ordered by seq."""
        cursor = await self.conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_event(dict(r)) for r in rows]

    async def get_event_range(
        self,
        run_id: str,
        from_seq: int,
        to_seq: int | None = None,
    ) -> list[Event]:
        """Return events in [from_seq, to_seq] range (inclusive)."""
        if to_seq is not None:
            cursor = await self.conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq >= ? AND seq <= ? ORDER BY seq ASC",
                (run_id, from_seq, to_seq),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM events WHERE run_id = ? AND seq >= ? ORDER BY seq ASC",
                (run_id, from_seq),
            )
        rows = await cursor.fetchall()
        return [_row_to_event(dict(r)) for r in rows]

    async def get_latest_seq(self, run_id: str) -> int:
        """Return the highest seq for a run, or 0 if no events exist."""
        cursor = await self.conn.execute(
            "SELECT MAX(seq) AS max_seq FROM events WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row["max_seq"] is None:
            return 0
        return row["max_seq"]

    async def event_count(self, run_id: str) -> int:
        """Return the total number of events for a run."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM events WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    # ── Query helpers ──────────────────────────────────────────

    async def find_by_idempotency_key(
        self,
        run_id: str,
        event_type: EventType,
        idempotency_key: str,
    ) -> Event | None:
        cursor = await self.conn.execute(
            "SELECT * FROM events WHERE run_id = ? AND event_type = ? AND idempotency_key = ?",
            (run_id, event_type.value, idempotency_key),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_event(dict(row))


# ── Helpers ────────────────────────────────────────────────────


def _validate_payload(event_type: EventType, payload: dict[str, Any]) -> None:
    model_cls = PAYLOAD_MODEL_MAP.get(event_type)
    if model_cls is None:
        raise ValueError(f"Unknown event type: {event_type}")
    model_cls.model_validate(payload)


def _row_to_event(row: dict[str, Any]) -> Event:
    return Event(
        run_id=row["run_id"],
        seq=row["seq"],
        event_type=EventType(row["event_type"]),
        payload=json.loads(row["payload"]),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )
