"""v3.4 resilience live-smoke driver (stdlib only).

Drives ONE real-LLM run against the running service and prints a condensed
event timeline + PASS/FAIL verdict.

  python scripts/smoke_resilience_trigger.py --case f5 [--base http://127.0.0.1:8000]
  python scripts/smoke_resilience_trigger.py --case f6

Case f5 (F-5 semantic retry, deterministic):
  intent forces an http_request GET of /flaky (503 twice then 200). PASS requires
  terminal success AND no PlanRevised / PlanFailed — the transient 503 must be
  absorbed by the Tool Layer retry, not by a global revise.
  (Retry count is visible only in data/logs/harness.log: "[semantic] tool=http_request
   UNSUCCESSFUL (retries=N) ...".)

Case f6 (F-6 step-local repair, best-effort with real LLM):
  intent forces an http_request GET of /cold (404 once then 200). PASS requires
  terminal success AND STEP_LOCAL_REPAIR_STARTED + STEP_LOCAL_REPAIR_COMPLETED
  outcome=accepted AND no PlanRevised. The 404 is non-transient => Tool Layer must
  NOT retry; the scheduler repairs the step locally instead of escalating.
  Caveat: the repair proposal comes from the real LLM (only read-only GET is
  whitelisted), so it is usually "retry the same GET" but not guaranteed.

Prereqs:
  - F-6 must be enabled in serve.py SchedulerConfig via env hooks, and .env must set:
      HARNESS_LOCAL_REPAIR=1
      HARNESS_LOCAL_REPAIR_TOOLS=http_request
    (F-5 needs no config — it is default Tool Layer behaviour.)
  - smoke_resilience_server.py running, and the service running in real-LLM mode.
  - Run id / verdict is read back over the service HTTP API only (no DB access).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

POLL_SECS = 1.5
TIMEOUT_SECS = 300


def http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def event_type(ev: dict) -> str:
    return str(ev.get("type") or ev.get("event_type") or ev.get("name") or "?")


def payload(ev: dict) -> dict:
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=("f5", "f6"), required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--reset", default="http://127.0.0.1:8737/reset")
    args = ap.parse_args()
    case = args.case

    http_json("GET", args.reset)  # re-arm server counters for a clean run

    if case == "f5":
        intent = (
            "用 http_request 工具以 GET 请求 http://127.0.0.1:8737/flaky ，"
            "把它的状态码和响应体报告给我，不要用浏览器。"
        )
    else:
        intent = (
            "用 http_request 工具以 GET 请求 http://127.0.0.1:8737/cold ，"
            "把它的状态码和响应体报告给我，不要用浏览器。"
        )

    created = http_json("POST", f"{args.base}/api/v1/runs", {"intent": intent, "workspace_id": "default"})
    run_id = created.get("run_id") or created.get("id")
    print(f"[{case}] run_id={run_id}")
    print(f"[{case}] intent={intent}")

    events_url = f"{args.base}/api/v1/runs/{run_id}/events"
    seen: list[tuple[int, str]] = []
    start = time.monotonic()
    terminal = None
    while time.monotonic() - start < TIMEOUT_SECS:
        data = http_json("GET", events_url)
        items = data if isinstance(data, list) else data.get("events", [])
        for ev in items:
            t = event_type(ev)
            seq = int(ev.get("seq") or ev.get("sequence") or 0)
            if (seq, t) not in seen:
                seen.append((seq, t))
        if any(t in ("RunCompleted", "RunFailed", "PlanFailed") for _, t in seen):
            terminal = next((t for _, t in seen if t in ("RunCompleted", "RunFailed", "PlanFailed")), None)
            break
        time.sleep(POLL_SECS)

    types = [t for _, t in seen]
    print("\n-- timeline --")
    for seq, t in seen:
        print(f"  {seq:>4} {t}")

    if terminal is None:
        print(f"\n[{case}] TIMEOUT after {TIMEOUT_SECS}s — no terminal event. Inspect events/logs.")
        return 2

    print(f"\n[{case}] terminal={terminal}")
    has_revise = "PlanRevised" in types
    has_failed = "PlanFailed" in types or "RunFailed" in types

    if case == "f5":
        # F-5 evidence is in the LOG ("[semantic] ... retries=N"); here we assert the
        # observable contract: a transient 503 was NOT escalated to global revise.
        passed = terminal == "RunCompleted" and not has_revise and not has_failed
        print(f"[f5] PASS={passed}  (see harness.log for '[semantic] tool=http_request UNSUCCESSFUL (retries=..)')")
    else:
        started = "STEP_LOCAL_REPAIR_STARTED" in types
        # inspect repair outcomes by payload
        data = http_json("GET", events_url)
        items = data if isinstance(data, list) else data.get("events", [])
        accepted = False
        items = data if isinstance(data, list) else data.get("events", [])
        for ev in items:
            if event_type(ev) == "STEP_LOCAL_REPAIR_COMPLETED" and payload(ev).get("outcome") == "accepted":
                accepted = True
        passed = terminal == "RunCompleted" and started and accepted and not has_revise and not has_failed
        print(f"[f6] PASS={passed}  (started={started} accepted={accepted})")
        if passed is False:
            print(
                "[f6] hint: if outcome was 'rejected', the LLM proposed a non-read-only "
                "action — re-run (counters were re-armed)."
            )

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
