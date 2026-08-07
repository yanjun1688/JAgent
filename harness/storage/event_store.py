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
    conversation_id TEXT,
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

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0
);
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
        # run_id -> conversation_id cache, populated from RunStarted payloads.
        # Used by append_event to fill conversation_id column for subsequent
        # events on the same run whose payload lacks this field.
        self._run_to_conv: dict[str, str] = {}

    async def _migrate_add_conversation_id_column(self) -> None:
        """Add conversation_id column to events table for legacy persistent DBs.

        DDL only — does not modify existing rows (Append-Only invariant preserved).
        The conversation_id index is created here (rather than in _SCHEMA_SQL)
        so that legacy DBs, whose CREATE TABLE IF NOT EXISTS is a no-op and
        whose table lacks the column, can ALTER TABLE before the index references it.
        For fresh DBs the column is already in the CREATE TABLE; this still runs
        to ensure the index exists.
        """
        cursor = await self.conn.execute("PRAGMA table_info(events)")
        rows = await cursor.fetchall()
        cols = {r[1] for r in rows}
        if "conversation_id" not in cols:
            _log_write.info("Migrating events table: adding conversation_id column")
            await self.conn.execute("ALTER TABLE events ADD COLUMN conversation_id TEXT")
            await self.conn.commit()
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_conversation ON events(conversation_id)"
        )
        await self.conn.commit()

    async def _prepopulate_run_to_conv_cache(self) -> None:
        """On startup, rebuild _run_to_conv from existing RunStarted payloads.

        Required for persistent DBs so that subsequent events on already-existing
        runs continue to receive the conversation_id column.
        """
        cursor = await self.conn.execute(
            "SELECT run_id, payload FROM events WHERE event_type = ?",
            (EventType.RUN_STARTED.value,),
        )
        rows = await cursor.fetchall()
        for r in rows:
            try:
                p = json.loads(r["payload"])
            except (json.JSONDecodeError, KeyError):
                continue
            cid = p.get("conversation_id")
            if cid:
                self._run_to_conv[r["run_id"]] = cid

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
        # Migration: existing persistent DBs predate `conversation_id` column.
        # DDL is permitted (Append-Only trigger only forbids UPDATE/DELETE on rows).
        await self._migrate_add_conversation_id_column()
        await self._prepopulate_run_to_conv_cache()

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

        # Auto-resolve conversation_id: prefer payload's own field (covers
        # RunStarted, ConversationStarted/Message/Ended); otherwise fall back
        # to the run-level cache so subsequent run events keep the column filled.
        conversation_id = payload.get("conversation_id")
        if conversation_id is None:
            conversation_id = self._run_to_conv.get(run_id)
        if conversation_id is not None and run_id not in self._run_to_conv:
            # Cache new mapping for future events on this run.
            self._run_to_conv[run_id] = conversation_id

        sql = (
            "INSERT INTO events (run_id, seq, event_type, payload, idempotency_key, created_at, conversation_id) "
            "SELECT ?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?), "
            "?, ?, ?, ?, ?"
        )

        lock = self._seq_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            for attempt in range(_max_retries + 1):
                try:
                    await self.conn.execute(
                        sql,
                        (run_id, run_id, event_type.value, payload_json,
                         idempotency_key, created_at, conversation_id),
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
        # Known Issue: count-based eviction above may miss locks held across the
        # 50-write checkpoint. Needs time-based TTL. See TODO_v2.1.md §Known Technical Debt.

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
        return [e for e in (_row_to_event(dict(r)) for r in rows) if e is not None]

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
        return [e for e in (_row_to_event(dict(r)) for r in rows) if e is not None]

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
            WHERE conversation_id IS NULL OR run_id != conversation_id
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
            """
            SELECT COUNT(DISTINCT run_id) AS cnt
            FROM events
            WHERE conversation_id IS NULL OR run_id != conversation_id
            """
        )
        row = await cursor.fetchone()
        _ms = (time.monotonic() - _t0) * 1000
        result = row["cnt"] if row else 0
        _log_query.debug("total_run_count: %d, %dms", result, _ms)
        return result

    async def list_all_run_ids(self) -> list[str]:
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            """
            SELECT DISTINCT run_id FROM events
            WHERE conversation_id IS NULL OR run_id != conversation_id
            """
        )
        rows = await cursor.fetchall()
        _ms = (time.monotonic() - _t0) * 1000
        result = [r["run_id"] for r in rows]
        _log_query.debug("list_all_run_ids: %d rows, %dms", len(result), _ms)
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
        result = [e for e in (_row_to_event(dict(r)) for r in rows) if e is not None]
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
            if event is None:
                continue
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

    # ── Conversation queries ──────────────────────────────────

    async def upsert_conversation(
        self,
        conversation_id: str,
        title: str,
        user_id: str = "default",
    ) -> None:
        now = time.time()
        await self.conn.execute(
            """INSERT INTO conversations (conversation_id, user_id, title, status, created_at, updated_at, message_count)
               VALUES (?, ?, ?, 'active', ?, ?, 0)
               ON CONFLICT(conversation_id) DO UPDATE SET
               title = excluded.title, updated_at = excluded.updated_at""",
            (conversation_id, user_id, title, now, now),
        )
        await self.conn.commit()

    async def list_conversations(
        self,
        limit: int = 50,
        offset: int = 0,
        user_id: str | None = None,
    ) -> list[dict]:
        if user_id:
            cursor = await self.conn.execute(
                """SELECT * FROM conversations WHERE status = 'active' AND user_id = ?
                   ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (user_id, limit, offset),
            )
        else:
            cursor = await self.conn.execute(
                """SELECT * FROM conversations WHERE status = 'active'
                   ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def total_conversation_count(self, user_id: str | None = None) -> int:
        if user_id:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM conversations WHERE status = 'active' AND user_id = ?",
                (user_id,),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM conversations WHERE status = 'active'",
            )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def get_conversation(self, conversation_id: str) -> dict | None:
        cursor = await self.conn.execute(
            "SELECT * FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_conversation(self, conversation_id: str) -> None:
        now = time.time()
        await self.conn.execute(
            "UPDATE conversations SET status = 'archived', updated_at = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
        await self.conn.commit()

    async def update_conversation(
        self,
        conversation_id: str,
        title: str | None = None,
        status: str | None = None,
    ) -> bool:
        now = time.time()
        updates = ["updated_at = ?"]
        params: list = [now]
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        params.append(conversation_id)
        sql = f"UPDATE conversations SET {', '.join(updates)} WHERE conversation_id = ?"
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.rowcount > 0

    async def increment_message_count(self, conversation_id: str) -> None:
        now = time.time()
        await self.conn.execute(
            "UPDATE conversations SET message_count = message_count + 1, updated_at = ? WHERE conversation_id = ?",
            (now, conversation_id),
        )
        await self.conn.commit()

    async def get_events_for_conversation(self, conversation_id: str) -> list[Event]:
        """Return all events associated with a conversation, as a coherent timeline.

        An event belongs to the conversation when EITHER:
          - its `conversation_id` column equals this conversation (new data
            written after the P0-04 migration: RunStarted carries the field
            in payload, subsequent run events inherit it via the run-level
            cache, conversation-level events carry it directly), OR
          - its `run_id` equals the conversation_id (legacy conversation-level
            events written before the column existed, where the column is NULL).

        Both arms use DISTINCT to avoid double-counting rows that satisfy
        both conditions (conversation-level events where run_id == conversation_id
        and the column is also filled).

        Ordering is by created_at then seq to give a real-world interleaving
        of events across multiple runs in the same conversation.
        """
        _t0 = time.monotonic()
        cursor = await self.conn.execute(
            """
            SELECT run_id, seq, event_type, payload, idempotency_key, created_at
            FROM events
            WHERE conversation_id = ? OR run_id = ?
            ORDER BY created_at ASC, seq ASC
            """,
            (conversation_id, conversation_id),
        )
        rows = await cursor.fetchall()
        _ms = (time.monotonic() - _t0) * 1000
        result = [e for e in (_row_to_event(dict(r)) for r in rows) if e is not None]
        _log_query.debug("get_events_for_conversation(conv=%s): %d rows, %dms",
                         conversation_id, len(result), _ms)
        return result

    def evict_run_to_conv(self, run_id: str) -> None:
        """Drop a run's cached conversation_id mapping.

        Safe to call after the run has reached a terminal state — at that
        point no new events will be appended for this run_id, so the cache
        entry is no longer needed for column auto-fill. Bounded growth is
        achieved by Scheduler terminal hooks calling this method.
        """
        self._run_to_conv.pop(run_id, None)


# ── Helpers ────────────────────────────────────────────────────


def _validate_payload(event_type: EventType, payload: dict[str, Any]) -> None:
    model_cls = PAYLOAD_MODEL_MAP.get(event_type)
    if model_cls is None:
        raise ValueError(f"Unknown event type: {event_type}")
    model_cls.model_validate(payload)


def _row_to_event(row: dict[str, Any]) -> Event | None:
    """Materialize a DB row into an Event.

    Returns None for rows whose `event_type` no longer exists in the
    EventType enum (e.g. legacy `QualityCheckCompleted` from V0.8 removed
    in f63e474). Append-Only invariant forbids DELETE so we tolerate these
    historical rows on read paths instead of crashing the whole query.
    """
    raw = row["event_type"]
    try:
        et = EventType(raw)
    except ValueError:
        _log_query.warning(
            "Skipping row with unknown event_type=%r (run=%s seq=%s) — "
            "likely a legacy event from a removed enum member",
            raw, row.get("run_id"), row.get("seq"),
        )
        return None
    return Event(
        run_id=row["run_id"],
        seq=row["seq"],
        event_type=et,
        payload=json.loads(row["payload"]),
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
    )
