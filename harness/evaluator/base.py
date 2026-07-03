from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.models.events import EventType, QualityCheckCompletedPayload, QualityIssuePayload


@dataclass
class QualityReport:
    check_id: str
    target: str
    evaluator_type: str
    verdict: str
    score: float | None = None
    issues: list[QualityIssuePayload] = field(default_factory=list)
    summary: str | None = None
    duration_ms: int = 0

    def to_payload(self) -> QualityCheckCompletedPayload:
        return QualityCheckCompletedPayload(
            check_id=self.check_id,
            target=self.target,
            evaluator_type=self.evaluator_type,
            verdict=self.verdict,
            score=self.score,
            issues=self.issues,
            summary=self.summary,
            duration_ms=self.duration_ms,
        )


class QualityCheck(ABC):
    check_id: str = ""
    trigger_events: list[EventType] = []

    def should_run(self, state: RunState) -> bool:
        return True

    @abstractmethod
    async def evaluate(self, state: RunState) -> QualityReport: ...


class RuleQualityCheck(QualityCheck):
    evaluator_type = "rule"

    @abstractmethod
    async def evaluate(self, state: RunState) -> QualityReport: ...


class LLMQualityCheck(QualityCheck):
    evaluator_type = "llm"

    def __init__(
        self,
        llm_client: LLMClient,
        sample_rate: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.sample_rate = sample_rate
        self._rng = rng or random.Random()

    def should_run(self, state: RunState) -> bool:
        return self._rng.random() < self.sample_rate

    @abstractmethod
    async def evaluate(self, state: RunState) -> QualityReport: ...
