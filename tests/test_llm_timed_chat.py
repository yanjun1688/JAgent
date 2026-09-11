"""T-R8b-4: timed_chat — single-source LLM timing/error log helper.

Pins three properties shared by planner.plan/revise and classify:
- success → one INFO ``[llm] phase=... run=... duration_ms=... chars=...``
- failure → one ERROR line with exception type, then re-raise
- CancelledError propagates untouched and is NOT downgraded to an error log
"""

import asyncio

import pytest

from harness.core.llm_client import ChatResponse, MockLLMClient, timed_chat
from harness.core.logger import agent_logger

_log = agent_logger("test_timed_chat")


@pytest.mark.asyncio
async def test_timed_chat_success_logs_phase_run_chars(caplog):
    llm = MockLLMClient([ChatResponse(content="x" * 10)])
    with caplog.at_level("INFO", logger="harness.agent.test_timed_chat"):
        resp = await timed_chat(llm, _log, "plan", [{"role": "user", "content": "q"}], run_id="run-1")
    assert resp.content == "x" * 10
    line = [r.message for r in caplog.records if r.message.startswith("[llm] phase=plan")]
    assert len(line) == 1
    assert "run=run-1" in line[0]
    assert "chars=10" in line[0]
    assert llm.calls[0]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_timed_chat_failure_logs_error_and_reraises(caplog):
    class _BoomLLM:
        async def chat(self, *a, **k):
            raise RuntimeError("upstream 500")

    with caplog.at_level("ERROR", logger="harness.agent.test_timed_chat"):
        with pytest.raises(RuntimeError, match="upstream 500"):
            await timed_chat(_BoomLLM(), _log, "classify", [], run_id="run-2")
    err = [r for r in caplog.records if "phase=classify" in r.message]
    assert len(err) == 1
    assert "RuntimeError" in err[0].message


@pytest.mark.asyncio
async def test_timed_chat_cancellation_propagates_without_error_log(caplog):
    class _CancelLLM:
        async def chat(self, *a, **k):
            raise asyncio.CancelledError()

    with caplog.at_level("ERROR", logger="harness.agent.test_timed_chat"):
        with pytest.raises(asyncio.CancelledError):
            await timed_chat(_CancelLLM(), _log, "serial_think", [], run_id="run-3")
    assert not [r for r in caplog.records if "phase=serial_think" in r.message]
