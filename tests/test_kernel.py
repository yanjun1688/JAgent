"""Unit tests for Agent Kernel parsing and LLM client (L4)."""

import pytest

from harness.core.agent_kernel import MockAgentKernel, _parse_response
from harness.core.llm_client import MockLLMClient
from harness.core.scheduler import ThinkResult
from harness.core.system_prompt import build_system_prompt, build_tool_schemas
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition

# ── 4.1 LLM client abstraction ──────────────────────────────


@pytest.mark.asyncio
async def test_mock_llm_client_returns_responses():
    client = MockLLMClient(["Hello", "World"])
    assert await client.chat([{"role": "user", "content": "Hi"}]) == "Hello"
    assert await client.chat([{"role": "user", "content": "Again"}]) == "World"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_mock_llm_client_returns_stop_on_exhaustion():
    client = MockLLMClient(["Only one"])
    assert await client.chat([{"role": "user", "content": "A"}]) == "Only one"
    response = await client.chat([{"role": "user", "content": "B"}])
    assert "<STOP>" in response


# ── 4.2 Context window — covered in LLMAgentKernel ──────────
# (No standalone unit tests; integration tested via scheduler)


# ── 4.4 System Prompt ───────────────────────────────────────


def test_system_prompt_includes_tool_descriptions():
    tool_defs = [
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
    prompt = build_system_prompt("Test intent", tool_defs)
    assert "Test intent" in prompt
    assert "**http**" in prompt
    assert "**delete**" in prompt
    assert "dangerous" in prompt
    assert "Make HTTP request" in prompt
    assert "Delete file" in prompt


def test_system_prompt_no_tools():
    prompt = build_system_prompt("Just think", [])
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


# ── 4.5 THINK step — response parsing ────────────────────────


def test_parse_stop_signal():
    result = _parse_response("THOUGHT: Task finished successfully.\n<STOP>")
    assert result.thought == "Task finished successfully."
    assert result.tool_name is None


def test_parse_tool_call():
    result = _parse_response(
        'THOUGHT: I need to fetch data\nTOOL: http_request\nARGS: {"url": "https://api.example.com", "method": "GET"}'
    )
    assert result.thought == "I need to fetch data"
    assert result.tool_name == "http_request"
    assert result.tool_input == {"url": "https://api.example.com", "method": "GET"}


def test_parse_tool_without_args():
    result = _parse_response("THOUGHT: Just checking\nTOOL: status_check\nARGS: {}")
    assert result.tool_name == "status_check"
    assert result.tool_input == {}


def test_parse_malformed_args():
    result = _parse_response("THOUGHT: Doing something\nTOOL: test_tool\nARGS: not valid json")
    assert result.tool_name == "test_tool"
    assert result.tool_input == {}


def test_parse_thought_only():
    result = _parse_response("This is just a thought, no structured output.")
    assert result.thought == "This is just a thought, no structured output."
    assert result.tool_name is None


def test_parse_multiline_thought():
    response = """THOUGHT: First line.
Second line of thought.
Third line.
TOOL: my_tool
ARGS: {"key": "value"}"""
    result = _parse_response(response)
    assert "First line" in result.thought
    assert "Second line" in result.thought
    assert result.tool_name == "my_tool"
    assert result.tool_input == {"key": "value"}


def test_parse_stop_with_tool_ignored():
    result = _parse_response("THOUGHT: Done.\n<STOP>\nTOOL: ignored_tool")
    assert result.thought == "Done."
    assert result.tool_name is None


# ── 4.6 Parse fault tolerance ────────────────────────────────


def test_parse_empty_response():
    result = _parse_response("")
    assert result.thought == ""
    assert result.tool_name is None


def test_parse_response_with_only_tool():
    result = _parse_response("TOOL: direct_call\nARGS: {}")
    assert result.tool_name == "direct_call"


# ── MockAgentKernel ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_mock_kernel_returns_preprogrammed():
    kernel = MockAgentKernel([ThinkResult(thought="Test", tool_name="http", tool_input={"url": "a"})])
    result = await kernel.think("intent", [], None)
    assert result.thought == "Test"
    assert result.tool_name == "http"
    assert result.tool_input == {"url": "a"}
    assert len(kernel.think_calls) == 1


@pytest.mark.asyncio
async def test_mock_kernel_returns_stop_on_exhaustion():
    kernel = MockAgentKernel([ThinkResult(thought="Done", tool_name=None)])
    await kernel.think("intent", [], None)
    result = await kernel.think("intent", [], None)
    assert result.tool_name is None
    assert "no more" in result.thought.lower()
