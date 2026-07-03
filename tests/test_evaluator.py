"""Tests for Quality Evaluator (V0.8)."""

from __future__ import annotations

import json
import pytest

from harness import (
    Event,
    EventStore,
    EventType,
    MockLLMClient,
    QualityCheckCompletedPayload,
    QualityIssuePayload,
    RunState,
    ToolResultType,
    fold_events,
)
from harness.evaluator import (
    AnswerAccuracyCheck,
    EvaluatorRunner,
    QualityReport,
    RuleQualityCheck,
    StepCompletenessCheck,
)


def _event(
    run_id: str,
    seq: int,
    event_type: EventType,
    payload: dict,
) -> Event:
    return Event(
        run_id=run_id, seq=seq, event_type=event_type,
        payload=payload, idempotency_key=None, created_at=0.0,
    )


async def _write(store: EventStore, run_id: str, event_type: EventType, payload: dict) -> Event:
    return await store.append_event(run_id, event_type, payload)


async def _setup_run(
    store: EventStore,
    run_id: str = "run1",
    intent: str = "test intent",
    with_answer: bool = False,
    result_type: ToolResultType = ToolResultType.SUCCESS,
) -> None:
    await _write(store, run_id, EventType.RUN_STARTED, {
        "intent": intent, "context_snapshot": {},
    })
    await _write(store, run_id, EventType.TOOL_CALLED, {
        "tool_call_id": "tc1", "tool_name": "http_request",
        "input": {"url": "http://example.com"},
    })
    await _write(store, run_id, EventType.TOOL_COMPLETED, {
        "tool_call_id": "tc1", "tool_name": "http_request",
        "output": {"status": "ok", "data": 42},
        "duration_ms": 100,
        "result_type": result_type.value,
    })
    if with_answer:
        await _write(store, run_id, EventType.RUN_COMPLETED, {
            "result_summary": "The request returned status ok with data value 42.",
        })


# ── Payload Models ─────────────────────────────────────────────────


class TestQualityCheckCompletedPayload:
    def test_minimal(self):
        p = QualityCheckCompletedPayload(
            check_id="step_completeness", target="step_results",
            evaluator_type="rule", verdict="pass",
        )
        assert p.check_id == "step_completeness"
        assert p.verdict == "pass"
        assert p.score is None
        assert p.issues == []

    def test_full(self):
        issues = [
            QualityIssuePayload(type="hallucination", severity="error", detail="Made up number"),
        ]
        p = QualityCheckCompletedPayload(
            check_id="answer_accuracy", target="answer", evaluator_type="llm",
            verdict="warn", score=0.6, issues=issues,
            summary="Some issues found", duration_ms=1500,
        )
        assert len(p.issues) == 1
        assert p.issues[0].type == "hallucination"
        assert p.score == 0.6

    def test_roundtrip(self):
        p = QualityCheckCompletedPayload(
            check_id="answer_accuracy", target="answer", evaluator_type="llm",
            verdict="pass", score=1.0,
            issues=[QualityIssuePayload(type="info", severity="info", detail="all good")],
            summary="Perfect", duration_ms=200,
        )
        restored = QualityCheckCompletedPayload(**p.model_dump())
        assert restored.check_id == p.check_id
        assert restored.score == 1.0


class TestQualityIssuePayload:
    def test_minimal(self):
        i = QualityIssuePayload(type="hallucination", severity="error", detail="bad")
        assert i.source is None

    def test_with_source(self):
        i = QualityIssuePayload(type="inconsistency", severity="warning", detail="mismatch", source="tc1")
        assert i.source == "tc1"


# ── Event Model ───────────────────────────────────────────────────


class TestEventType:
    def test_quality_check_completed_registered(self):
        assert EventType.QUALITY_CHECK_COMPLETED.value == "QualityCheckCompleted"

    def test_in_payload_map(self):
        from harness.models.events import PAYLOAD_MODEL_MAP
        assert EventType.QUALITY_CHECK_COMPLETED in PAYLOAD_MODEL_MAP


# ── Fold ──────────────────────────────────────────────────────────


class TestFoldQualityChecks:
    def test_accumulates(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.QUALITY_CHECK_COMPLETED, {
                "check_id": "step_completeness", "target": "step_results",
                "evaluator_type": "rule", "verdict": "pass",
            }),
            _event("r1", 3, EventType.QUALITY_CHECK_COMPLETED, {
                "check_id": "answer_accuracy", "target": "answer",
                "evaluator_type": "llm", "verdict": "warn", "score": 0.7,
            }),
        ]
        state = fold_events(events)
        assert len(state.quality_checks) == 2
        assert state.quality_checks[0].check_id == "step_completeness"
        assert state.quality_checks[1].score == 0.7

    def test_default_empty(self):
        events = [
            _event("r1", 1, EventType.RUN_STARTED, {"intent": "x", "context_snapshot": {}}),
            _event("r1", 2, EventType.RUN_COMPLETED, {"result_summary": "done"}),
        ]
        state = fold_events(events)
        assert state.quality_checks == []


# ── QualityReport ─────────────────────────────────────────────────


class TestQualityReport:
    def test_to_payload_converts_correctly(self):
        report = QualityReport(
            check_id="test", target="answer", evaluator_type="rule",
            verdict="pass", duration_ms=10,
        )
        payload = report.to_payload()
        assert isinstance(payload, QualityCheckCompletedPayload)
        assert payload.check_id == "test"


# ── StepCompletenessCheck ─────────────────────────────────────────


class TestStepCompletenessCheck:
    def test_check_id_and_triggers(self):
        check = StepCompletenessCheck()
        assert check.check_id == "step_completeness"
        assert check.evaluator_type == "rule"
        assert EventType.RUN_COMPLETED in check.trigger_events

    @pytest.mark.asyncio
    async def test_all_matched_passes(self, store: EventStore):
        await _setup_run(store)
        events = await store.get_events("run1")
        report = await StepCompletenessCheck().evaluate(fold_events(events))
        assert report.verdict == "pass"
        assert report.issues == []

    @pytest.mark.asyncio
    async def test_unmatched_tool_call_fails(self, store: EventStore):
        await _write(store, "run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        await _write(store, "run1", EventType.TOOL_CALLED, {
            "tool_call_id": "tc_missing", "tool_name": "http_request",
            "input": {"url": "http://example.com"},
        })
        events = await store.get_events("run1")
        report = await StepCompletenessCheck().evaluate(fold_events(events))
        assert report.verdict == "fail"
        assert report.issues[0].type == "missing_data"
        assert report.issues[0].source == "tc_missing"

    @pytest.mark.asyncio
    async def test_null_output_warns(self, store: EventStore):
        await _write(store, "run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        await _write(store, "run1", EventType.TOOL_CALLED, {
            "tool_call_id": "tc1", "tool_name": "file_op",
            "input": {"path": "/tmp", "op": "read"},
        })
        await _write(store, "run1", EventType.TOOL_COMPLETED, {
            "tool_call_id": "tc1", "tool_name": "file_op",
            "output": None, "duration_ms": 50,
            "result_type": ToolResultType.SUCCESS.value,
        })
        events = await store.get_events("run1")
        report = await StepCompletenessCheck().evaluate(fold_events(events))
        assert report.verdict == "warn"
        assert report.issues[0].severity == "warning"

    @pytest.mark.asyncio
    async def test_mixed_error_wins(self, store: EventStore):
        await _write(store, "run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        await _write(store, "run1", EventType.TOOL_CALLED, {
            "tool_call_id": "tc_ok", "tool_name": "echo", "input": {"msg": "x"},
        })
        await _write(store, "run1", EventType.TOOL_COMPLETED, {
            "tool_call_id": "tc_ok", "tool_name": "echo",
            "output": {"ok": True}, "duration_ms": 10,
            "result_type": ToolResultType.SUCCESS.value,
        })
        await _write(store, "run1", EventType.TOOL_CALLED, {
            "tool_call_id": "tc_missing", "tool_name": "http_request",
            "input": {"url": "http://fail.com"},
        })
        events = await store.get_events("run1")
        report = await StepCompletenessCheck().evaluate(fold_events(events))
        assert report.verdict == "fail"
        assert len(report.issues) == 1


# ── AnswerAccuracyCheck ──────────────────────────────────────────


class TestAnswerAccuracyCheck:
    def test_check_id_and_triggers(self):
        check = AnswerAccuracyCheck(llm_client=MockLLMClient(responses=[]))
        assert check.check_id == "answer_accuracy"
        assert check.evaluator_type == "llm"
        assert EventType.RUN_COMPLETED in check.trigger_events

    @pytest.mark.asyncio
    async def test_pass_verdict(self, store: EventStore):
        client = MockLLMClient(responses=[
            json.dumps({"verdict": "pass", "score": 1.0, "summary": "All good", "issues": []}),
        ])
        await _setup_run(store, with_answer=True)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "pass"
        assert report.score == 1.0
        assert report.issues == []

    @pytest.mark.asyncio
    async def test_short_circuits_when_all_tools_completed(self, store: EventStore):
        """When all tools complete successfully, skip LLM call and return pass."""
        client = MockLLMClient(responses=[])  # no responses needed, LLM won't be called
        await _setup_run(store, with_answer=True)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "pass"
        assert report.score == 1.0
        assert report.issues == []
        assert report.summary == "All tools completed successfully — LLM evaluation skipped"

    @pytest.mark.asyncio
    async def test_warn_with_issues(self, store: EventStore):
        client = MockLLMClient(responses=[
            json.dumps({
                "verdict": "warn", "score": 0.5,
                "summary": "Minor issues",
                "issues": [{"type": "inconsistency", "severity": "warning",
                            "detail": "Answer says 100 but result was 42"}],
            }),
        ])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "warn"
        assert len(report.issues) == 1
        assert report.issues[0].type == "inconsistency"

    @pytest.mark.asyncio
    async def test_skips_when_no_answer_or_no_tool_results(self, store: EventStore):
        check = AnswerAccuracyCheck(llm_client=MockLLMClient(responses=[]))
        await _write(store, "run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        await _write(store, "run1", EventType.RUN_COMPLETED, {"result_summary": ""})
        events = await store.get_events("run1")
        assert not check.should_run(fold_events(events))

    @pytest.mark.asyncio
    async def test_llm_failure_graceful(self, store: EventStore):
        class FailingClient:
            async def chat(self, messages, **kwargs):
                raise RuntimeError("API down")

        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        check = AnswerAccuracyCheck(llm_client=FailingClient(), sample_rate=1.0)
        report = await check.evaluate(fold_events(events))
        assert report.verdict == "warn"
        assert report.issues[0].type == "check_failed"
        assert "API down" in report.issues[0].detail

    @pytest.mark.asyncio
    async def test_parse_markdown_json(self, store: EventStore):
        client = MockLLMClient(responses=[
            '```json\n{"verdict": "fail", "score": 0.0, "summary": "Bad", "issues": []}\n```',
        ])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "fail"

    @pytest.mark.asyncio
    async def test_parse_unparseable(self, store: EventStore):
        client = MockLLMClient(responses=["not json"])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "warn"
        assert report.issues[0].type == "check_failed"

    @pytest.mark.asyncio
    async def test_parse_json_with_text_prefix(self, store: EventStore):
        client = MockLLMClient(responses=[
            'Here is my evaluation:\n\n{"verdict": "pass", "score": 1.0, "summary": "OK", "issues": []}',
        ])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "pass"
        assert report.score == 1.0

    @pytest.mark.asyncio
    async def test_parse_json_with_text_suffix(self, store: EventStore):
        client = MockLLMClient(responses=[
            '{"verdict": "fail", "score": 0.0, "summary": "Bad", "issues": []}\n\nHope this helps.',
        ])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "fail"

    @pytest.mark.asyncio
    async def test_parse_json_with_prefix_and_suffix(self, store: EventStore):
        client = MockLLMClient(responses=[
            'Analysis:\n{"verdict": "warn", "score": 0.5, "summary": "issues", "issues": []}\n\nDone.',
        ])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "warn"
        assert report.score == 0.5

    @pytest.mark.asyncio
    async def test_parse_json_nested_braces(self, store: EventStore):
        client = MockLLMClient(responses=[
            '{"verdict": "pass", "score": 1.0, "issues": [{"type": "info", "severity": "info", "detail": "ok"}], "summary": "Good"}',
        ])
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)
        events = await store.get_events("run1")
        report = await AnswerAccuracyCheck(llm_client=client).evaluate(fold_events(events))
        assert report.verdict == "pass"
        assert len(report.issues) == 1


# ── EvaluatorRunner — tests _evaluate_checks directly ────────────


class TestEvaluatorRunner:
    @pytest.mark.asyncio
    async def test_evaluates_step_completeness(self, store: EventStore):
        runner = EvaluatorRunner(checks=[StepCompletenessCheck()], store=store)
        await _setup_run(store)

        await runner._evaluate_checks("run1", [StepCompletenessCheck()])

        events = await store.get_events("run1")
        qc = [e for e in events if e.event_type == EventType.QUALITY_CHECK_COMPLETED]
        assert len(qc) == 1
        assert qc[0].payload["check_id"] == "step_completeness"
        assert qc[0].payload["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_evaluates_answer_accuracy(self, store: EventStore):
        client = MockLLMClient(responses=[
            json.dumps({"verdict": "fail", "score": 0.0, "summary": "Bad", "issues": []}),
        ])
        runner = EvaluatorRunner(
            checks=[AnswerAccuracyCheck(llm_client=client)], store=store,
        )
        await _setup_run(store, with_answer=True, result_type=ToolResultType.SOFT_ERROR)

        await runner._evaluate_checks("run1", [AnswerAccuracyCheck(llm_client=client)])

        events = await store.get_events("run1")
        qc = [e for e in events if e.event_type == EventType.QUALITY_CHECK_COMPLETED]
        assert len(qc) == 1
        assert qc[0].payload["check_id"] == "answer_accuracy"
        assert qc[0].payload["verdict"] == "fail"

    @pytest.mark.asyncio
    async def test_both_checks_on_full_run(self, store: EventStore):
        client = MockLLMClient(responses=[
            json.dumps({"verdict": "pass", "score": 1.0, "summary": "OK", "issues": []}),
        ])
        runner = EvaluatorRunner(
            checks=[StepCompletenessCheck(), AnswerAccuracyCheck(llm_client=client)],
            store=store,
        )
        await _setup_run(store, with_answer=True)

        await runner._evaluate_checks(
            "run1",
            [StepCompletenessCheck(), AnswerAccuracyCheck(llm_client=client)],
        )

        events = await store.get_events("run1")
        qc_events = [e for e in events if e.event_type == EventType.QUALITY_CHECK_COMPLETED]
        check_ids = {e.payload["check_id"] for e in qc_events}
        assert "step_completeness" in check_ids
        assert "answer_accuracy" in check_ids

    @pytest.mark.asyncio
    async def test_isolated_check_failure_does_not_block_others(self, store: EventStore):
        class CrashCheck(RuleQualityCheck):
            check_id = "crash"
            trigger_events = [EventType.RUN_COMPLETED]

            async def evaluate(self, state: RunState) -> QualityReport:
                raise RuntimeError("boom")

        runner = EvaluatorRunner(
            checks=[CrashCheck(), StepCompletenessCheck()],
            store=store,
        )
        await _setup_run(store, with_answer=True)

        await runner._evaluate_checks("run1", [CrashCheck(), StepCompletenessCheck()])

        events = await store.get_events("run1")
        qc_events = [e for e in events if e.event_type == EventType.QUALITY_CHECK_COMPLETED]
        check_ids = {e.payload["check_id"] for e in qc_events}
        assert "step_completeness" in check_ids
        assert "crash" not in check_ids

    @pytest.mark.asyncio
    async def test_skip_check_when_should_run_false(self, store: EventStore):
        client = MockLLMClient(responses=[])
        runner = EvaluatorRunner(
            checks=[AnswerAccuracyCheck(llm_client=client)], store=store,
        )
        await _write(store, "run1", EventType.RUN_STARTED, {
            "intent": "test", "context_snapshot": {},
        })
        await _write(store, "run1", EventType.RUN_COMPLETED, {
            "result_summary": "",
        })

        await runner._evaluate_checks("run1", [AnswerAccuracyCheck(llm_client=client)])

        events = await store.get_events("run1")
        qc = [e for e in events if e.event_type == EventType.QUALITY_CHECK_COMPLETED]
        assert len(qc) == 0

    @pytest.mark.asyncio
    async def test_writes_folded_state_back_to_quality_checks(self, store: EventStore):
        runner = EvaluatorRunner(checks=[StepCompletenessCheck()], store=store)
        await _setup_run(store, with_answer=True)

        await runner._evaluate_checks("run1", [StepCompletenessCheck()])

        events = await store.get_events("run1")
        state = fold_events(events)
        assert len(state.quality_checks) >= 1
        assert any(qc.check_id == "step_completeness" for qc in state.quality_checks)
