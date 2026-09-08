from __future__ import annotations

import time

from harness.core.fold import RunStatus, fold_events
from harness.core.logger import guard_logger
from harness.models.events import EventType, RunFailedPayload, RunOrphanedPayload
from harness.storage.event_store import EventStore

_log = guard_logger("lifecycle")


async def mark_orphans(store: EventStore) -> int:
    """Scan all runs and terminate runs orphaned by a server restart.

    Called once at server startup. A run that was RUNNING/PAUSED when the
    previous process died has no scheduler driving it anymore — its in-memory
    scheduler state (tasks, pause/confirm events, deadlines) is gone. Such a
    run is handled in two appended events:

      1. ``RUN_ORPHANED`` — diagnostic flag (fold sets ``state.orphaned``),
         kept for attribution / UI badge / resource reapers (e.g. browser
         pool lease release subscribes to it).
      2. ``RUN_FAILED`` — terminal state. Direction A: orphans ARE terminal.
         We deliberately do NOT auto-resume crashed runs. The operator
         re-submits the task; event sourcing + idempotency keys + the F-4
         evidence rebuild mean completed steps are not re-executed / their
         side effects not replayed. See REPLAY_INSPECTOR_v1.0.md for the
         recorded "resume after crash" future direction.

    Idempotent: runs already FAILED/COMPLETED or already orphaned-then-failed
    are skipped, so a second startup scan writes nothing. Returns the number
    of runs newly terminated.

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
            # Terminal (COMPLETED/FAILED, including a run terminated by an
            # earlier scan) — nothing to do. This status guard is what makes
            # the two appends below idempotent across repeated startup scans.
            continue

        # 1) Diagnostic flag (idempotent across restarts).
        await store.append_event(
            run_id,
            EventType.RUN_ORPHANED,
            RunOrphanedPayload(
                reason="server_restart",
                detected_at=now,
            ).model_dump(),
            idempotency_key=f"orphan_detect_{run_id}",
        )

        # 2) Terminal convergence (idempotent): the run is dead with the
        # process that drove it; do not leave a fake-RUNNING zombie behind.
        event_count = len(events) + 2  # + RUN_ORPHANED + this RUN_FAILED
        await store.append_event(
            run_id,
            EventType.RUN_FAILED,
            RunFailedPayload(
                final_error=(
                    "Run was terminated because the server process restarted while it was active "
                    "(orphaned run). Automatic crash recovery is not performed; please re-submit "
                    "the task — completed steps are not repeated (event sourcing + idempotency)."
                ),
                event_count=event_count,
                result_summary="任务因服务重启而中断（孤儿 run），请重新发起任务。",
                user_facing_message="任务因服务重启而中断，请重新发起该任务；已完成的步骤不会重复执行。",
            ).model_dump(),
            idempotency_key=f"orphan_fail_{run_id}",
        )
        marked += 1
        _log.info("Terminated orphan run %s as FAILED (was status=%s)", run_id, state.status.value)

    if marked:
        _log.info("Orphan detection complete: %d run(s) terminated as FAILED", marked)
    else:
        _log.info("Orphan detection complete: no orphans found")

    return marked
