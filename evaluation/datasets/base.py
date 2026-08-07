"""Eval dataset models and YAML loader (Phase 4).

Design per JAgent-docs/Dev/LANGFUSE_INTEGRATION_PLAN.md §8.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml


@dataclass
class EvalCase:
    """Single evaluation case — expected behaviour for one Agent run.

    All ``expected_*`` fields are optional; scorers only check the fields that
    are present on a case, so a case can target a subset of scoring dimensions.
    """

    id: str
    scenario: str
    intent: str
    scheduler_mode: str = "serial"
    expected_tools: list[str] | None = None
    expected_max_steps: int | None = None
    expected_status: str | None = None
    expected_guardrail_hit: bool = False
    expected_guardrail_type: str | None = None
    expected_tool_status: str | None = None
    expected_requires_confirmation: bool = False
    expected_confirmation_status: str | None = None
    expected_parallel_layers: int | None = None
    expected_output_contains: list[str] | None = None
    expected_output_not_contains: list[str] | None = None
    expected_recovery: bool | None = None
    expected_hallucination_free: bool | None = None
    expected_tool_sequence: list[str] | None = None
    max_runtime_s: float = 300.0
    mock_actions: list[dict] | None = None
    """Deterministic tool-call script for trusted-component scenarios.

    When present, the eval engine drives the run with a MockAgentKernel that
    issues exactly these tool calls (with optional ``repeat`` count), so
    Guardrail / confirmation flows are exercised deterministically without
    depending on an LLM's willingness to attempt dangerous operations.
    """

    mock_plan: dict | None = None
    """Pre-built DAG plan (``{"intent": ..., "steps": [...]}``) for planning mode.

    When present, the eval engine drives the run with a MockLLMClient that
    returns this plan verbatim, so DAG topology execution (layers, parallelism,
    event order) is verified deterministically. Values may contain ``@project@``
    placeholders expanded to the repo root at build time.
    """


class DatasetLoader:
    """Load eval datasets from YAML and filter them."""

    @staticmethod
    def load(path: str) -> list[EvalCase]:
        """Load all cases from a YAML file.

        Expected YAML shape (a top-level ``datasets:`` key is optional):
            - id: "single_step_001"
              scenario: "单步工具调用"
              intent: "..."
              scheduler_mode: "serial"
              expected_tools: ["file_read"]
        """
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if not raw:
            return []
        if isinstance(raw, dict) and isinstance(raw.get("datasets"), list):
            rows = raw["datasets"]
        elif isinstance(raw, list):
            rows = raw
        else:
            raise ValueError(f"Unsupported dataset YAML shape in {path}")

        cases: list[EvalCase] = []
        for row in rows:
            if not isinstance(row, dict) or "id" not in row or "intent" not in row:
                raise ValueError(f"Invalid eval case in {path}: {row!r}")
            unknown = set(row) - {f for f in EvalCase.__dataclass_fields__}
            if unknown:
                raise ValueError(f"Unknown field(s) {sorted(unknown)} in case '{row['id']}'")
            cases.append(EvalCase(**row))
        return cases

    @staticmethod
    def filter_by_scenario(cases: list[EvalCase], scenario: str) -> list[EvalCase]:
        return [c for c in cases if c.scenario == scenario]

    @staticmethod
    def filter_by_case_id(cases: list[EvalCase], case_id: str) -> list[EvalCase]:
        return [c for c in cases if c.id == case_id]
