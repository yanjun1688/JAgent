from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from harness.core.logger import guard_logger
from harness.models.events import PAYLOAD_MODEL_MAP, Event, EventType

_log_write = guard_logger("store.write")
_log_query = guard_logger("store.query")
_log_idem = guard_logger("store.idem")

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

-- Append-Only enforcement: reject UPDATE and DELETE on events table
CREATE TRIGGER IF NOT EXISTS trg_prevent_update_events
    BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'Append-Only: UPDATE is forbidden on events table');
END;

CREATE TRIGGER IF NOT EXISTS trg_prevent_delete_events
    BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'Append-Only: DELETE is forbidden on events table');
END;
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

    seq allocation:
    - Uses per-run_id asyncio.Lock to ensure atomic MAX(seq)+1 under concurrent
      asyncio tasks on the same connection. Locks are cleaned up after each
      append_event to prevent memory accumulation.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._post_append: list[Callable[[Event], Awaitable[None]]] = []
        self._seq_locks: dict[str, asyncio.Lock] = {}
        self._append_count: int = 0

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

    def on_append(self, callback: Callable[[Event], Awaitable[None]]) -> None:
        self._post_append.append(callback)

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
        _max_retries: int = 3,
    ) -> Event:
        """Append a new event to the store."""
        _t0 = time.monotonic()
        _validate_payload(event_type, payload)

        if idempotency_key is not None:
            existing = await self.find_by_idempotency_key(run_id, event_type, idempotency_key)
            if existing is not None:
                _log_idem.info("Idempotency cache hit: %s @ seq=%d (run=%s)",
                               event_type.value, existing.seq, run_id)
                return existing

        created_at = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False)

        sql = (
            "INSERT INTO events (run_id, seq, event_type, payload, idempotency_key, created_at) "
            "SELECT ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?), "
            "?, ?, ?, ?"
        )

        lock = self._seq_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            for attempt in range(_max_retries + 1):
                try:
                    await self.conn.execute(
                        sql,
                        (run_id, run_id, event_type.value, payload_json, idempotency_key, created_at),
                    )
                    await self.conn.commit()
                    break
                except sqlite3.IntegrityError as exc:
                    error_str = str(exc)
                    if "UNIQUE constraint" in error_str and "events.idempotency_key" in error_str:
                        existing = await self.find_by_idempotency_key(run_id, event_type, idempotency_key)
                        if existing is not None:
                            return existing
                        raise DuplicateIdempotencyKeyError(
                            f"Duplicate idempotency key '{idempotency_key}' for run '{run_id}'"
                        ) from exc
                    if attempt < _max_retries:
                        _log_write.warning("Seq conflict on attempt %d/%d for run=%s, retrying...",
                                           attempt + 1, _max_retries + 1, run_id)
                        continue
                    raise SequenceConflictError(
                        f"PK conflict on (run_id='{run_id}', seq): {exc}"
                    ) from exc

        self._append_count += 1
        if self._append_count % 50 == 0:
            stale = [rid for rid, lk in self._seq_locks.items() if not lk.locked()]
            for rid in stale:
                self._seq_locks.pop(rid, None)

        next_seq = await self.get_latest_seq(run_id)
        event = Event(
            run_id=run_id,
            seq=next_seq,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        _ms = (time.monotonic() - _t0) * 1000
        _log_write.info("Written event @ seq=%d: %s (run=%s, %dms)",
                     next_seq, event_type.value, run_id, _ms)
        for cb in self._post_append:
            try:
                await cb(event)
            except Exception as exc:
                _log_write.error("on_append callback failed: %s", exc)
        return event

    async def get_events(self, run_id: str) -> list[Event]:
        """Return all events for a run, ordered by seq."""
        _start = time.monotonic()
        cursor = await self.conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        )
        rows = await cursor.fetchall()
        _ms = (time.monotonic() - _start) * 1000
        _log_query.debug("get_events(run=%s): %d rows, %dms", run_id, len(rows), _ms)
        return [_row_to_event(dict(r)) for r in rows]

    async def get_event_range(
        self,
        run_id: str,
        from_seq: int,
        to_seq: int | None = None,
    ) -> list[Event]:
        """Return events in [from_seq, to_seq] range (inclusive)."""
        _t0 = time.monotonic()
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
        _ms = (time.monotonic() - _t0) * 1000
        _log_query.debug("get_event_range(run=%s, from=%s): %d rows, %dms",
                         run_id, from_seq, len(rows), _ms)
        return [_row_to_event(dict(r)) for r in rows]

    async def get_latest_seq(self, run_id: str) -> int:
        """Return the highest seq for a run, or 0 if no events exist."""
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            "SELECT MAX(seq) AS max_seq FROM events WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        _ms = (time.monotonic() - _t0) * 1000
        result = row["max_seq"] if row is not None and row["max_seq"] is not None else 0
        _log_query.debug("get_latest_seq(run=%s): %d, %dms", run_id, result, _ms)
        return result

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
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            "SELECT * FROM events WHERE run_id = ? AND event_type = ? AND idempotency_key = ?",
            (run_id, event_type.value, idempotency_key),
        )
        row = await cursor.fetchone()
        _ms = (time.monotonic() - _t0) * 1000
        if row is None:
            _log_idem.debug("IK lookup miss: run=%s type=%s ik=%.16s (%dms)",
                            run_id, event_type.value, idempotency_key, _ms)
            return None
        _log_idem.debug("IK lookup hit: run=%s type=%s ik=%.16s seq=%d (%dms)",
                        run_id, event_type.value, idempotency_key, row["seq"], _ms)
        return _row_to_event(dict(row))

    async def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            """
            SELECT run_id, MIN(seq) AS seq, MIN(created_at) AS created_at,
                   MAX(created_at) AS updated_at,
                   COUNT(*) AS event_count
            FROM events
            GROUP BY run_id
            ORDER BY MAX(created_at) DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = await cursor.fetchall()
        _ms = (time.monotonic() - _t0) * 1000
        result = [dict(r) for r in rows]
        _log_query.debug("list_runs(limit=%d, offset=%d): %d rows, %dms",
                         limit, offset, len(result), _ms)
        return result

    async def total_run_count(self) -> int:
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            "SELECT COUNT(DISTINCT run_id) AS cnt FROM events"
        )
        row = await cursor.fetchone()
        _ms = (time.monotonic() - _t0) * 1000
        result = row["cnt"] if row else 0
        _log_query.debug("total_run_count: %d, %dms", result, _ms)
        return result

    async def get_events_for_runs(self, run_ids: list[str]) -> list[Event]:
        if not run_ids:
            return []
        _t0 = time.monotonic()
        placeholders = ",".join("?" * len(run_ids))
        cursor = await self.conn.execute(
            f"SELECT * FROM events WHERE run_id IN ({placeholders}) ORDER BY run_id, seq ASC",
            run_ids,
        )
        rows = await cursor.fetchall()
        _ms = (time.monotonic() - _t0) * 1000
        result = [_row_to_event(dict(r)) for r in rows]
        _log_query.debug("get_events_for_runs(%d runs): %d rows, %dms",
                         len(run_ids), len(result), _ms)
        return result

    async def find_confirmation_by_id(self, run_id: str, confirmation_id: str) -> Event | None:
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            "SELECT * FROM events WHERE run_id = ? AND event_type = ?",
            (run_id, EventType.CONFIRMATION_RECEIVED.value),
        )
        rows = await cursor.fetchall()
        _ms = (time.monotonic() - _t0) * 1000
        for row in rows:
            event = _row_to_event(dict(row))
            payload = json.loads(row["payload"])
            if payload.get("confirmation_id") == confirmation_id:
                _log_query.debug("find_confirmation(run=%s, id=%s): hit, %dms",
                                 run_id, confirmation_id, _ms)
                return event
        _log_query.debug("find_confirmation(run=%s, id=%s): miss, %dms",
                         run_id, confirmation_id, _ms)
        return None


    # ── Read-only query helpers (for analysis/reporting) ─────────

    async def execute_query(self, sql: str, params: list | tuple | None = None) -> list[dict]:
        """Execute a read-only SELECT and return rows as dicts.

        This is the sanctioned escape hatch for analysis queries that
        are not covered by the public API.  Only SELECT is permitted —
        the caller owns the query, EventStore provides the connection.
        """
        cursor = await self.conn.execute(sql, params or [])
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def execute_query_one(self, sql: str, params: list | tuple | None = None) -> dict | None:
        """Like execute_query but returns a single row (or None)."""
        rows = await self.execute_query(sql, params)
        return rows[0] if rows else None


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
