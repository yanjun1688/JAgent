"""T-R8b-3 regression: local-repair parse-failure feedback + no-spin semantics.

The defect: local repair retried the SAME prompt on a parse failure (3x no-op)
and had no test pinning its terminal/abort semantics. This pins the fix:

- parse failure → the concrete error is fed back (local_repair_retry_prompt)
- a legitimate ``{"tool_name": ""}`` answer terminates WITHOUT retrying
- the first LLM transport exception aborts local repair (return None)
- CancelledError always propagates (BaseException, never swallowed)

``parse_local_repair_proposal`` now returns ``(proposal, error)``: an empty error
marks a terminal answer, a non-empty error marks a retryable format failure.
"""

from __future__ import annotations

import asyncio

import pytest

from harness.core.llm_client import MockLLMClient
from harness.core.planner import Planner, parse_local_repair_proposal
from harness.models.plan import DagStep
from harness.models.tools import RetryPolicy, ToolDefinition
from harness.tools.registry import ToolRegistry


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r._register(
        ToolDefinition(
            name="http_request",
            description="Read-only HTTP GET",
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            output_schema={"type": "object"},
            idempotency_key_fields=[],
            side_effects=[],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        ),
        lambda x: {"ok": True},
    )
    return r


class _BoomLLM:
    """LLM double whose every chat call raises the given exception."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        raise self.exc


def _step() -> DagStep:
    return DagStep(id="s1", tool="broken", input={})


class TestParseLocalRepairProposal:
    def test_valid_proposal_returns_no_error(self):
        proposal, err = parse_local_repair_proposal('{"tool_name": "http_request", "input": {"url": "https://x"}}')
        assert err == ""
        assert proposal is not None
        assert proposal.tool_name == "http_request"
        assert proposal.input == {"url": "https://x"}

    def test_parameters_alias_accepted(self):
        proposal, err = parse_local_repair_proposal('{"tool_name": "http_request", "parameters": {"url": "https://x"}}')
        assert err == ""
        assert proposal is not None and proposal.input == {"url": "https://x"}

    def test_empty_tool_name_is_terminal_not_retryable(self):
        proposal, err = parse_local_repair_proposal('{"tool_name": ""}')
        assert proposal is None
        assert err == ""  # model decision — caller escalates without retrying

    def test_missing_input_is_retryable_error(self):
        proposal, err = parse_local_repair_proposal('{"tool_name": "http_request"}')
        assert proposal is None
        assert "input" in err

    def test_non_dict_input_is_retryable_error(self):
        proposal, err = parse_local_repair_proposal('{"tool_name": "http_request", "input": "nope"}')
        assert proposal is None
        assert "input" in err

    def test_no_json_is_retryable_error(self):
        proposal, err = parse_local_repair_proposal("totally not json")
        assert proposal is None
        assert err  # non-empty → retryable

    def test_non_object_root_is_retryable_error(self):
        proposal, err = parse_local_repair_proposal('["not", "an", "object"]')
        assert proposal is None
        assert err


class TestProposeLocalRepairRetry:
    async def test_parse_failure_feeds_error_back_then_succeeds(self):
        llm = MockLLMClient(
            [
                '{"tool_name": "http_request"}',  # missing input → retry
                '{"tool_name": "http_request", "input": {"url": "https://ok"}}',
            ]
        )
        planner = Planner(llm_client=llm, registry=_registry())
        proposal = await planner.propose_local_repair(_step(), "boom", allowed_tools=["http_request"])
        assert proposal is not None
        assert proposal.tool_name == "http_request"
        assert len(llm.calls) == 2
        retry_msg = llm.calls[1]["messages"][-1]
        assert retry_msg["role"] == "user"
        assert "input" in retry_msg["content"]  # concrete error fed back, not same-prompt spin

    async def test_empty_answer_does_not_retry(self):
        llm = MockLLMClient(['{"tool_name": ""}'])
        planner = Planner(llm_client=llm, registry=_registry())
        assert await planner.propose_local_repair(_step(), "boom", allowed_tools=["http_request"]) is None
        assert len(llm.calls) == 1

    async def test_transport_exception_aborts_without_retry(self):
        llm = _BoomLLM(RuntimeError("upstream 500"))
        planner = Planner(llm_client=llm, registry=_registry())
        assert await planner.propose_local_repair(_step(), "boom", allowed_tools=["http_request"]) is None
        assert llm.calls == 1

    async def test_cancellation_propagates(self):
        llm = _BoomLLM(asyncio.CancelledError())
        planner = Planner(llm_client=llm, registry=_registry())
        with pytest.raises(asyncio.CancelledError):
            await planner.propose_local_repair(_step(), "boom", allowed_tools=["http_request"])
        assert llm.calls == 1

    async def test_all_attempts_retryable_then_give_up(self):
        llm = MockLLMClient(['{"tool_name": "http_request"}'] * 3)
        planner = Planner(llm_client=llm, registry=_registry(), max_plan_retries=2)
        assert await planner.propose_local_repair(_step(), "boom", allowed_tools=["http_request"]) is None
        assert len(llm.calls) == 3  # max_plan_retries + 1
