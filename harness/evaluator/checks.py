from __future__ import annotations

import json
import time

from harness.core.json_parser import extract_json
from harness.core.llm_client import LLMClient
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.evaluator.base import LLMQualityCheck, QualityReport, RuleQualityCheck
from harness.core.fold import RunState, ToolResultStatus
from harness.models.events import EventType, QualityIssuePayload


STEP_COMPLETENESS_ID = "step_completeness"
ANSWER_ACCURACY_ID = "answer_accuracy"


class StepCompletenessCheck(RuleQualityCheck):
    check_id = STEP_COMPLETENESS_ID
    trigger_events = [EventType.RUN_COMPLETED]

    async def evaluate(self, state: RunState) -> QualityReport:
        _start = time.monotonic()
        issues: list[QualityIssuePayload] = []

        tool_call_ids = {tc.tool_call_id for tc in state.tool_calls}
        result_call_ids = {tr.tool_call_id for tr in state.tool_results}

        for call_id in sorted(tool_call_ids - result_call_ids):
            tc = next((t for t in state.tool_calls if t.tool_call_id == call_id), None)
            detail = (
                f"Tool call '{call_id}' ({tc.tool_name if tc else 'unknown'}) "
                f"has no corresponding result event"
            )
            issues.append(QualityIssuePayload(
                type="missing_data", severity="error", detail=detail, source=call_id,
            ))

        for tr in state.tool_results:
            if tr.status == ToolResultStatus.COMPLETED and tr.output is None:
                issues.append(QualityIssuePayload(
                    type="missing_data",
                    severity="warning",
                    detail=f"Tool '{tr.tool_name}' ({tr.tool_call_id}) completed but output is null",
                    source=tr.tool_call_id,
                ))

        if issues:
            verdict = "fail" if any(i.severity == "error" for i in issues) else "warn"
        else:
            verdict = "pass"

        _ms = int((time.monotonic() - _start) * 1000)
        return QualityReport(
            check_id=self.check_id,
            target="step_results",
            evaluator_type=self.evaluator_type,
            verdict=verdict,
            issues=issues,
            summary=None if not issues else (
                f"{len(state.tool_calls)} tool calls, {len(state.tool_results)} results, "
                f"{len(issues)} issues"
            ),
            duration_ms=_ms,
        )


class AnswerAccuracyCheck(LLMQualityCheck):
    check_id = ANSWER_ACCURACY_ID
    trigger_events = [EventType.RUN_COMPLETED]

    def __init__(self, llm_client: LLMClient, sample_rate: float = 1.0) -> None:
        super().__init__(llm_client=llm_client, sample_rate=sample_rate)

    def should_run(self, state: RunState) -> bool:
        if not super().should_run(state):
            return False
        return bool(state.summary and state.tool_results)

    async def evaluate(self, state: RunState) -> QualityReport:
        _start = time.monotonic()

        if state.tool_results and all(
            tr.status == ToolResultStatus.COMPLETED for tr in state.tool_results
        ):
            _ms = int((time.monotonic() - _start) * 1000)
            return QualityReport(
                check_id=self.check_id,
                target="answer",
                evaluator_type=self.evaluator_type,
                verdict="pass",
                score=1.0,
                issues=[],
                summary="All tools completed successfully — LLM evaluation skipped",
                duration_ms=_ms,
            )

        tool_results_lines = [
            f"  [{tr.status.value}] {tr.tool_name} ({tr.tool_call_id}): "
            f"{_truncate(json.dumps(tr.output, ensure_ascii=False, default=str), 500)}"
            for tr in state.tool_results
        ]
        tool_results_summary = "\n".join(tool_results_lines) if tool_results_lines else "(no tool results)"
        answer = str(state.summary) if state.summary else "(no answer)"

        prompt = get_prompt(
            AgentPhase.EVALUATE_ANSWER_ACCURACY,
            intent=state.intent or "(no user intent recorded)",
            tool_results_summary=tool_results_summary,
            answer=answer,
        )

        try:
            raw = await self.llm_client.chat([
                {"role": "user", "content": prompt},
            ])
        except Exception as exc:
            _ms = int((time.monotonic() - _start) * 1000)
            return QualityReport(
                check_id=self.check_id,
                target="answer",
                evaluator_type=self.evaluator_type,
                verdict="warn",
                issues=[QualityIssuePayload(
                    type="check_failed", severity="warning",
                    detail=f"LLM call failed: {exc}",
                )],
                summary="LLM evaluation unavailable",
                duration_ms=_ms,
            )

        parsed = extract_json(raw)
        _ms = int((time.monotonic() - _start) * 1000)

        if parsed is None:
            return QualityReport(
                check_id=self.check_id,
                target="answer",
                evaluator_type=self.evaluator_type,
                verdict="warn",
                issues=[QualityIssuePayload(
                    type="check_failed", severity="warning",
                    detail=f"Failed to parse LLM response: {_truncate(raw, 200)}",
                )],
                summary="Could not parse evaluation result",
                duration_ms=_ms,
            )

        return QualityReport(
            check_id=self.check_id,
            target="answer",
            evaluator_type=self.evaluator_type,
            verdict=parsed.get("verdict", "warn"),
            score=parsed.get("score"),
            issues=[
                QualityIssuePayload(
                    type=i.get("type", "unknown"),
                    severity=i.get("severity", "info"),
                    detail=i.get("detail", ""),
                    source=i.get("source"),
                )
                for i in parsed.get("issues", [])
            ],
            summary=parsed.get("summary"),
            duration_ms=_ms,
        )


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + "..."
