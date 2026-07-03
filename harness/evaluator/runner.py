from __future__ import annotations

import asyncio
import time

from harness.core.fold import fold_events
from harness.core.logger import fmtkv, monitor_logger
from harness.evaluator.base import QualityCheck
from harness.models.events import Event, EventType
from harness.storage.event_store import EventStore

_log = monitor_logger("evaluator")


class EvaluatorRunner:
    def __init__(self, checks: list[QualityCheck], store: EventStore) -> None:
        self.checks = checks
        self.store = store

    def attach(self) -> None:
        self.store.on_append(self._on_event)
        _log.info("EvaluatorRunner attached with %d checks", len(self.checks))

    async def _on_event(self, event: Event) -> None:
        try:
            matching = [c for c in self.checks if event.event_type in c.trigger_events]
            if not matching:
                return
            _log.debug("Triggering checks for %s: %s",
                       event.event_type.value, [c.check_id for c in matching])
            asyncio.create_task(self._evaluate_checks(event.run_id, matching))
        except Exception:
            _log.exception("EvaluatorRunner._on_event crashed")

    async def _evaluate_checks(self, run_id: str, checks: list[QualityCheck]) -> None:
        _t0 = time.monotonic()
        try:
            events = await self.store.get_events(run_id)
            state = fold_events(events)

            for check in checks:
                try:
                    if not check.should_run(state):
                        continue

                    report = await check.evaluate(state)
                    _log.info("Check %s completed %s", check.check_id,
                              fmtkv(verdict=report.verdict, duration_ms=report.duration_ms))

                    await self.store.append_event(
                        run_id,
                        EventType.QUALITY_CHECK_COMPLETED,
                        report.to_payload().model_dump(),
                        idempotency_key=f"qc_{run_id}_{check.check_id}",
                    )
                except Exception:
                    _log.exception("Check %s failed for run %s", check.check_id, run_id)
        except Exception:
            _log.exception("_evaluate_checks failed for run %s", run_id)
        finally:
            _ms = int((time.monotonic() - _t0) * 1000)
            _log.debug("_evaluate_checks finished for run %s in %dms", run_id, _ms)
