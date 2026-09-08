"""v3.4 resilience live-smoke helper — local flaky HTTP server (stdlib only).

Endpoints (call GET /reset to re-arm counters before each run):
  GET /flaky  -> first 2 calls return 503, then 200   (F-5 semantic retry)
  GET /cold   -> first call returns 404,  then 200    (F-6 step-local repair)
  GET /ping   -> always 200 (reachability probe)
  GET /reset  -> reset /flaky and /cold counters

The 503/404 status lines surface as the http tool's semantic fallback error
("status_code=503 ..." / "status_code=404 ..."), which is exactly what the
F-5 transient classifier and the F-6 non-transient candidate funnel key on.

Usage:  python scripts/smoke_resilience_server.py [port]     (default 8737)
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

flaky_calls = 0
cold_calls = 0
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8737


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        global flaky_calls, cold_calls
        if self.path == "/reset":
            flaky_calls = 0
            cold_calls = 0
            self._json(200, {"ok": True, "reset": True})
            return
        if self.path == "/ping":
            self._json(200, {"ok": True})
            return
        if self.path == "/flaky":
            flaky_calls += 1
            if flaky_calls <= 2:
                # 503 body is irrelevant: the tool's fallback error carries
                # "status_code=503", which the transient classifier matches.
                self._json(503, {"error": "upstream hiccup"})
                return
            self._json(200, {"ok": True, "attempt": flaky_calls})
            return
        if self.path == "/cold":
            cold_calls += 1
            if cold_calls == 1:
                # 404 => NON-transient (no 5xx / temporar / unavailable tokens),
                # so the Tool Layer must NOT auto-retry; it escalates to F-6.
                self._json(404, {"error": "cold start: entry not found yet"})
                return
            self._json(200, {"ok": True, "attempt": cold_calls})
            return
        self._json(404, {"error": "unknown path"})

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: object) -> None:  # keep console quiet
        pass


if __name__ == "__main__":
    print(f"[smoke-resilience] listening on 127.0.0.1:{PORT}  (GET /flaky /cold /ping /reset)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
