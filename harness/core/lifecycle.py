from __future__ import annotations

import time

from harness.core.fold import RunStatus, fold_events
from harness.core.logger import guard_logger
from harness.models.events import EventType, RunOrphanedPayload
from harness.storage.event_store import EventStore

_log = guard_logger("lifecycle")


async def mark_orphans(store: EventStore) -> int:
    """Scan all runs and mark RUNNING/PAUSED runs as orphaned.

    Called once at server startup to detect runs that lost their scheduler
    due to a server restart. Idempotent: runs already marked orphaned are
    skipped. COMPLETED/FAILED runs are never touched.

    Returns the number of runs newly marked as orphaned.

    Performance: O(N * E) where N = total runs, E = avg events per run.
    Each run requires a full event fetch + fold to determine its current
    status and orphaned flag. For databases with tens of thousands of runs,
    consider replacing with a SQL-level filter on terminal event types to
    avoid loading non-terminal runs into memory.
    """
    run_ids = await store.list_all_run_ids()
    if not run_ids:
        return 0

    marked = 0
    now = time.time()

    for run_id in run_ids:
        events = await store.get_events(run_id)
        if not events:
            continue

        state = fold_events(events)

        if state.status not in (RunStatus.RUNNING, RunStatus.PAUSED):
            continue

        if state.orphaned:
            continue

        await store.append_event(
            run_id,
            EventType.RUN_ORPHANED,
            RunOrphanedPayload(
                reason="server_restart",
                detected_at=now,
            ).model_dump(),
            idempotency_key=f"orphan_detect_{run_id}",
        )
        marked += 1
        _log.info("Marked run %s as orphaned (status=%s)", run_id, state.status.value)

    if marked:
        _log.info("Orphan detection complete: %d run(s) marked", marked)
    else:
        _log.info("Orphan detection complete: no orphans found")

    return marked
