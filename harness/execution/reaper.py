"""Startup carrier reaper — reclaim execution-carrier resources leaked by a crash.

Trusted, deterministic, and event-driven (AGENTS.md §2.2): the decision to
reap is made mechanically from the event stream, never from the Agent.

Why this exists
---------------
An orphaned run (server restarted while it was RUNNING/PAUSED) never runs its
scheduler `finally`, so `backend.close()` is never called. For the browser pool
this is handled by subscribing to ``RUN_ORPHANED`` + reaping stale profile
locks (browser_pool.py). Docker sandbox containers have no such path: a
container is started lazily as ``docker run -d --rm ... sleep infinity`` and
``--rm`` only removes it when it *stops* — which ``sleep infinity`` never does.
Without cross-process discovery the container (and its bind mount) leaks
forever after a crash.

Mechanism
---------
Every run-bound Docker container is stamped with identity labels at creation
(docker.py ``_docker_run_args``): ``harness.managed=1``, ``harness.run_id=…``,
``harness.tenant_id=…``. At startup, after ``mark_orphans`` has converged dead
runs to a terminal event, the reaper:

  1. lists running containers carrying ``harness.managed=1``,
  2. folds each container's run event stream to decide whether the run is still
     active (RUNNING/PAUSED) or terminal / unknown,
  3. force-removes containers whose run is NOT active. A managed container whose
     run has no events is also removed: a managed container implies a run once
     existed, so absence of events means a torn-down/unknown run — never a live
     one. Unlabeled (foreign) containers are never touched.

SSH (REMOTE) carriers are not handled here: the RemoteSSHBackend is a lazy
placeholder and leaves no discoverable remote lease. Local (DIRECTORY) carriers
hold no external resource. Both are documented limitations.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from harness.core.fold import RunStatus, fold_events
from harness.core.logger import guard_logger
from harness.storage.event_store import EventStore

_log = guard_logger("carrier.reaper")

_MANAGED_LABEL = "harness.managed"
_RUN_ID_LABEL = "harness.run_id"


def docker_available() -> bool:
    return shutil.which("docker") is not None


def plan_carrier_reaping(
    containers: list[dict[str, Any]],
    active_run_ids: set[str],
) -> list[str]:
    """Pure selection: return the container IDs that must be force-removed.

    ``containers`` is a list of ``{"id": str, "run_id": str | None}`` (run_id
    ``None`` = no/illegible identity label). A container is reaped iff it is
    managed (has a run_id label) AND its run is not in ``active_run_ids``.
    Unlabeled / foreign containers are never returned.
    """
    to_reap: list[str] = []
    for c in containers:
        run_id = c.get("run_id")
        if not run_id:
            # Not ours to manage (foreign container or illegible label).
            continue
        if run_id in active_run_ids:
            continue
        cid = c.get("id")
        if cid:
            to_reap.append(cid)
    return to_reap


async def list_managed_containers() -> list[dict[str, Any]]:
    """List running managed containers as ``[{"id", "run_id"}]``.

    Uses ``docker ps`` with a JSON format so labels are parsed reliably. A
    container without the run-id label yields ``run_id=None`` (treated foreign).
    """
    fmt = "{{json .ID}}|{{json .Label \"" + _RUN_ID_LABEL + "\"}}"
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "ps",
        "--filter",
        f"label={_MANAGED_LABEL}=1",
        "--format",
        fmt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        _log.warning("docker ps failed: %s", err.decode(errors="replace").strip())
        return []
    containers: list[dict[str, Any]] = []
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        id_part, _, label_part = line.partition("|")
        try:
            cid = json.loads(id_part)
        except json.JSONDecodeError:
            continue
        run_id: str | None = None
        try:
            run_id = json.loads(label_part) if label_part and label_part != '""' else None
        except json.JSONDecodeError:
            run_id = None
        if run_id == "":
            run_id = None
        containers.append({"id": cid, "run_id": run_id})
    return containers


async def remove_container(container_id: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "rm",
        "-f",
        container_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        _log.warning("docker rm -f %s failed: %s", container_id, err.decode(errors="replace").strip())


async def reap_orphaned_carriers(store: EventStore) -> int:
    """Force-remove managed Docker containers whose run is no longer active.

    Must run AFTER ``mark_orphans`` so crashed runs have converged to a terminal
    event. Returns the number of containers reaped.
    """
    if not docker_available():
        _log.info("Docker CLI unavailable; skipping carrier reaping")
        return 0

    containers = await list_managed_containers()
    if not containers:
        return 0

    run_ids = {c["run_id"] for c in containers if c.get("run_id")}
    active: set[str] = set()
    for run_id in run_ids:
        events = await store.get_events(run_id)
        if not events:
            # Managed container but its run has no events: unknown/torn-down
            # run → not active → reap (see module docstring).
            continue
        state = fold_events(events)
        if state.status in (RunStatus.RUNNING, RunStatus.PAUSED):
            active.add(run_id)

    to_reap = plan_carrier_reaping(containers, active)
    for cid in to_reap:
        await remove_container(cid)
        _log.info("Reaped orphaned carrier container %s", cid)
    if to_reap:
        _log.info("Carrier reaper removed %d orphaned container(s)", len(to_reap))
    return len(to_reap)
