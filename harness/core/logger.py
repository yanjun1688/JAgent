"""Logging categories for Harness v2.1.

Three namespaces with independently configurable levels:

  role      namespace           content
  ─────────────────────────────────────────────────────────
  AGENT     harness.agent.*     agent lifecycle: think, act, observe,
                                feedback, LLM calls, tool execution
  GUARD     harness.guard.*     system protection: guardrails, idempotency,
                                context compression, breaker, event store
  MONITOR   harness.monitor.*   real-time monitoring: event observation,
                                anomaly detection, feedback injection

Utilities:
  log_duration()  — context manager that logs ENTER/EXIT with wall-clock ms
  fmtkv()         — format key=value pairs for structured log lines
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any


def guard_logger(name: str) -> logging.Logger:
    """Get a logger under the harness.guard namespace (system protection)."""
    return logging.getLogger(f"harness.guard.{name}")


def agent_logger(name: str) -> logging.Logger:
    """Get a logger under the harness.agent namespace (agent lifecycle)."""
    return logging.getLogger(f"harness.agent.{name}")


def monitor_logger(name: str) -> logging.Logger:
    """Get a logger under the harness.monitor namespace (monitoring)."""
    return logging.getLogger(f"harness.monitor.{name}")


def fmtkv(**fields: Any) -> str:
    """Format key=value pairs for structured log lines.

    Values are str()-ified. None is rendered as "null", booleans as lowercase.
    """
    parts: list[str] = []
    for k, v in fields.items():
        if v is None:
            v = "null"
        elif isinstance(v, bool):
            v = "true" if v else "false"
        parts.append(f"{k}={v}")
    return " ".join(parts)


@contextmanager
def log_duration(
    logger: logging.Logger,
    operation: str,
    level: int = logging.INFO,
    **fields: Any,
):
    """Context manager that logs ENTER on entry and EXIT + duration_ms on exit.

    Usage:
        with log_duration(_log, "get_events", run=run_id):
            rows = await self.conn.execute(...)

    Produces:
        [ENTER] get_events run=abc123
        [EXIT]  get_events run=abc123 duration_ms=1.2
    """
    logger.log(level, "[ENTER] %s %s", operation, fmtkv(**fields))
    _start = time.monotonic()
    try:
        yield
    finally:
        _ms = (time.monotonic() - _start) * 1000
        logger.log(level, "[EXIT]  %s %s duration_ms=%.1f", operation, fmtkv(**fields), _ms)
