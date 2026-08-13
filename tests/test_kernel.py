"""Unit tests for Agent Kernel and LLM client (L4).

Structured tool_calls path only — _parse_results / regex已被移除 (B-2/B-3)。
Tests focus on:
  - MockLLMClient returning structured ChatResponse
  - Serial system prompt variants
  - build_tool_schemas
  - LLMAgentKernel stop/answer/text path via ChatResponse
  - D-2: tool_call_id 透传 / 多轮历史协议配对 / json 解析失败可观测性
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from harness.core.agent_kernel import LLMAgentKernel, MockAgentKernel
from harness.core.fold import RunState, ThoughtEntry, ToolResult, ToolResultStatus
from harness.core.llm_client import ChatResponse, MockLLMClient, ToolCall
from harness.core.scheduler import ThinkResult
from harness.core.system_prompt import AgentPhase, build_tool_schemas, get_prompt
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition


# ── 4.1 LLM client abstraction — returns structured ChatResponse ──


@pytest.mark.asyncio
async def test_mock_llm_client_returns_responses():
    client = MockLLMClient(["Hello", "World"])
    r1 = await client.chat([{"role": "user", "content": "Hi"}])
    r2 = await client.chat([{"role": "user", "content": "Again"}])
    assert isinstance(r1, ChatResponse)
    assert r1.content == "Hello"
    assert r2.content == "World"
    assert r1.tool_calls == []
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_mock_llm_client_returns_stop_on_exhaustion():
    client = MockLLMClient(["Only one"])
    r1 = await client.chat([{"role": "user", "content": "A"}])
    assert r1.content == "Only one"
    r2 = await client.chat([{"role": "user", "content": "B"}])
    assert "<STOP>" in r2.content


@pytest.mark.asyncio
async def test_mock_llm_client_accepts_chatresponse():
    resp = ChatResponse(
        content="thinking",
        tool_calls=[ToolCall(id="abc", name="http", arguments={"url": "x"})],
    )
    client = MockLLMClient([resp])
    r = await client.chat([{"role": "user", "content": "q"}])
    assert r is resp
    assert r.tool_calls[0].id == "abc"


# ── 4.4 System Prompt ───────────────────────────────────────


def test_system_prompt_includes_tool_descriptions():
    [
        ToolDefinition(
            name="http",
            description="Make HTTP request",
            idempotency_key_fields=["url"],
            side_effects=[SideEffect.EXTERNAL],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
        ),
        ToolDefinition(
            name="delete",
            description="Delete file",
            idempotency_key_fields=["path"],
            side_effects=[SideEffect.DELETE],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
            requires_confirmation=True,
        ),
    ]
    prompt = get_prompt(
        AgentPhase.SERIAL_THINK_FN,
        intent="Test intent",
        tool_list="  - **http**: Make HTTP request\n  - **delete**: Delete file (require confirmation)",
    )
    assert "Test intent" in prompt
    assert "**http**" in prompt
    assert "function-calling" in prompt


def test_system_prompt_text_variant_still_has_tool_args_directive():
    prompt = get_prompt(AgentPhase.SERIAL_THINK_TEXT, intent="t", tool_list="(no tools)")
    assert "TOOL:" in prompt
    assert "ARGS:" in prompt


def test_system_prompt_no_tools():
    prompt = get_prompt(AgentPhase.SERIAL_THINK, intent="Just think", tool_list="(no tools available)")
    assert "no tools available" in prompt.lower() or "(no tools" in prompt


def test_build_tool_schemas():
    tool_defs = [
        ToolDefinition(
            name="http",
            description="HTTP request",
            idempotency_key_fields=["url"],
            side_effects=[SideEffect.EXTERNAL],
            timeout_ms=5000,
            retry_policy=RetryPolicy(),
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        ),
    ]
    schemas = build_tool_schemas(tool_defs)
    assert len(schemas) == 1
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "http"
    assert "url" in schemas[0]["function"]["parameters"]["properties"]


# ── MockAgentKernel ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_kernel_returns_preprogrammed():
    kernel = MockAgentKernel([ThinkResult(thought="Test", tool_name="http", tool_input={"url": "a"})])
    results = await kernel.think("intent", [], None)
    assert len(results) == 1
    assert results[0].thought == "Test"
    assert results[0].tool_name == "http"
    assert results[0].tool_input == {"url": "a"}
    assert len(kernel.think_calls) == 1


@pytest.mark.asyncio
async def test_mock_kernel_returns_stop_on_exhaustion():
    kernel = MockAgentKernel([ThinkResult(thought="Done", tool_name=None)])
    await kernel.think("intent", [], None)
    results = await kernel.think("intent", [], None)
    assert len(results) == 1
    assert results[0].tool_name is None
    assert "no more" in results[0].thought.lower()


# ── D-2.1 tool_call_id 透传 ──────────────────────────────────


@pytest.mark.asyncio
async def test_llm_kernel_preserves_tool_call_id_from_provider():
    """Mock 注: ChatResponse.tool_calls id 必须原样进入 ThinkResult.tool_call_id."""
    resp = ChatResponse(
        content="",
        tool_calls=[ToolCall(id="call_abc", name="http", arguments={"url": "https://x"})],
    )
    kernel = LLMAgentKernel(MockLLMClient([resp]))
    state = RunState(run_id="r1")
    results = await kernel.think("intent", [], state)
    assert len(results) == 1
    assert results[0].tool_call_id == "call_abc"
    assert results[0].tool_name == "http"
    assert results[0].tool_input == {"url": "https://x"}


@pytest.mark.asyncio
async def test_openai_client_preserves_tool_call_id_on_parse_failure(caplog):
    """OpenAILLMClient 在 json 解析失败时必须 warning + arguments=_parse_error."""
    from harness.core.llm_client import OpenAILLMClient

    fake_response = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {"name": "http", "arguments": "not a json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    client = OpenAILLMClient(api_key="k", model="m", base_url="http://fake")

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return fake_response

        def raise_for_status(self):
            pass

    class _FakeAsync:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

    with patch("harness.core.llm_client.httpx.AsyncClient", _FakeAsync):
        result = await client.chat([{"role": "user", "content": "hi"}])

    assert result.tool_calls[0].id == "call_xyz"
    assert result.tool_calls[0].arguments == {"_parse_error": "not a json"}
    assert any("tool_call arguments json decode failed" in r.getMessage() for r in caplog.records)


# ── D-2.2 多轮历史协议配对 ──────────────────────────────────


@pytest.mark.asyncio
async def test_llm_kernel_history_pairing_two_rounds():
    """构造 2 轮历史，断言 messages 中 assistant.tool_calls + role=tool 配对完整."""
    resp = ChatResponse(content="<STOP>")
    client = MockLLMClient([resp])
    kernel = LLMAgentKernel(client)

    state = RunState(run_id="r")
    state.thought_history = [
        ThoughtEntry(seq=1, thought="round 1"),
        ThoughtEntry(seq=3, thought="round 2"),
    ]
    state.tool_results = [
        ToolResult(
            tool_call_id="tc_1", tool_name="http", status=ToolResultStatus.COMPLETED, output="out1", event_seq=2
        ),
        ToolResult(
            tool_call_id="tc_2", tool_name="file_op", status=ToolResultStatus.COMPLETED, output="out2", event_seq=4
        ),
    ]

    await kernel.think("intent", [], state)
    sent_messages = client.calls[0]["messages"]

    assistant_msgs_with_tool_calls = [m for m in sent_messages if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(assistant_msgs_with_tool_calls) == 2

    tool_msgs = [m for m in sent_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2

    first_assistant_tcs = {tc["id"] for tc in assistant_msgs_with_tool_calls[0]["tool_calls"]}
    second_assistant_tcs = {tc["id"] for tc in assistant_msgs_with_tool_calls[1]["tool_calls"]}
    first_tool_id = tool_msgs[0]["tool_call_id"]
    second_tool_id = tool_msgs[1]["tool_call_id"]
    assert first_tool_id in first_assistant_tcs
    assert second_tool_id in second_assistant_tcs

    idx_first_assistant = sent_messages.index(assistant_msgs_with_tool_calls[0])
    idx_first_tool = sent_messages.index(tool_msgs[0])
    idx_second_assistant = sent_messages.index(assistant_msgs_with_tool_calls[1])
    assert idx_first_assistant < idx_first_tool < idx_second_assistant


# ── D-2.3 json 解析失败可观测性 ──────────────────────────────


@pytest.mark.asyncio
async def test_parse_failure_observable_no_silent_pass():
    """OpenAILLMClient 拿到非 JSON arguments，不抛异常，arguments 含 _parse_error."""
    from harness.core.llm_client import OpenAILLMClient

    fake_response = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "b1", "type": "function", "function": {"name": "f", "arguments": "{bad"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {},
    }
    client = OpenAILLMClient(api_key="k", model="m", base_url="http://fake")

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return fake_response

        def raise_for_status(self):
            pass

    class _FakeAsync:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

    with patch("harness.core.llm_client.httpx.AsyncClient", _FakeAsync):
        resp = await client.chat([{"role": "user", "content": "x"}])

    assert resp.tool_calls[0].arguments == {"_parse_error": "{bad"}
    assert resp.tool_calls[0].name == "f"
    assert resp.tool_calls[0].id == "b1"

    kernel = LLMAgentKernel(
        MockLLMClient([ChatResponse(tool_calls=[ToolCall(id="b1", name="f", arguments={"_parse_error": "{bad"})])])
    )
    state = RunState(run_id="r")
    results = await kernel.think("intent", [], state)
    assert results[0].tool_name == "f"
    assert results[0].tool_input == {"_parse_error": "{bad"}
    assert results[0].tool_call_id == "b1"


# ── LLMAgentKernel _generate_stop_summary ────────────────────


@pytest.mark.asyncio
async def test_llm_kernel_stop_triggers_summary():
    """When content contains <STOP>, _generate_stop_summary produces direct_answer."""
    client = MockLLMClient(
        [
            ChatResponse(content="<STOP>"),
            ChatResponse(content="I have completed the task by fetching the data and saving it."),
        ]
    )
    kernel = LLMAgentKernel(client)
    state = RunState(run_id="test")
    tool_defs: list[ToolDefinition] = []

    results = await kernel.think("fetch data and save", tool_defs, state)
    assert len(results) == 1
    assert results[0].tool_name is None
    assert results[0].direct_answer == "I have completed the task by fetching the data and saving it."
    assert len(client.calls) == 2  # main call + summary call


@pytest.mark.asyncio
async def test_llm_kernel_stop_summary_failure_does_not_break():
    """When summary generation fails, direct_answer stays None but think still succeeds."""

    class _FailingSummaryClient(MockLLMClient):
        async def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=4096):
            self.calls.append({"messages": messages, "tools": tools})
            if self._idx >= len(self.responses):
                raise RuntimeError("simulated LLM failure")
            response = self.responses[self._idx]
            self._idx += 1
            return response

    client = _FailingSummaryClient([ChatResponse(content="<STOP>")])
    kernel = LLMAgentKernel(client)
    state = RunState(run_id="test")
    tool_defs: list[ToolDefinition] = []

    results = await kernel.think("do something", tool_defs, state)
    assert len(results) == 1
    assert results[0].tool_name is None
    assert results[0].direct_answer is None
