"""Typed results for DAG step execution.

Replaces the opaque dict[str, Any] contract with explicit types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    COMPLETED = "completed"
    SOFT_ERROR = "soft_error"
    FAILED = "failed"
    CONFIRMATION_NEEDED = "confirmation_needed"
    EXECUTOR_ERROR = "executor_error"


@dataclass
class StepResult:
    """Result of executing a single DAG step.

    Replaces the previous dict-based contract used by _execute_step_only
    and retry_step.  Callers should check .status rather than raw.get("status").
    """

    step_id: str
    status: StepStatus
    output: Any = None
    summary: str = ""
    error: str | None = None
    retryable: bool = False
    confirmation_id: str | None = None

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-compatible access for backward compatibility with upstream_outputs."""
        if key == "output":
            return self.output
        if key == "status":
            return self.status.value
        if key == "summary":
            return self.summary
        if key == "error":
            return self.error
        if key == "retryable":
            return self.retryable
        if key == "confirmation_id":
            return self.confirmation_id
        return default

    @property
    def is_completed(self) -> bool:
        return self.status == StepStatus.COMPLETED

    @property
    def is_done(self) -> bool:
        return self.status in (StepStatus.COMPLETED, StepStatus.SOFT_ERROR)

    @property
    def is_failed(self) -> bool:
        return self.status in (StepStatus.FAILED, StepStatus.EXECUTOR_ERROR)

    @property
    def needs_confirmation(self) -> bool:
        return self.status == StepStatus.CONFIRMATION_NEEDED

    @property
    def has_soft_error(self) -> bool:
        return self.status == StepStatus.SOFT_ERROR
