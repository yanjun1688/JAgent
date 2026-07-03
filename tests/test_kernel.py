"""Unit tests for Agent Kernel parsing and LLM client (L4)."""

import pytest

from harness.core.agent_kernel import MockAgentKernel, LLMAgentKernel, _parse_results
from harness.core.fold import RunState
from harness.core.llm_client import MockLLMClient
from harness.core.scheduler import ThinkResult
from harness.core.system_prompt import AgentPhase, build_tool_schemas, get_prompt
from harness.models.tools import RetryPolicy, SideEffect, ToolDefinition


def _parse_single(response: str) -> ThinkResult:
    """Test helper: return first ThinkResult from parse results."""
    return _parse_results(response)[0]

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
    prompt = get_prompt(AgentPhase.SERIAL_THINK, intent="Test intent", tool_list="  - **http**: Make HTTP request\n  - **delete**: Delete file (require confirmation)")
    assert "Test intent" in prompt
    assert "**http**" in prompt
    assert "**delete**" in prompt
    assert "require confirmation" in prompt
    assert "Make HTTP request" in prompt
    assert "Delete file" in prompt


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


# ── 4.5 THINK step — response parsing ────────────────────────


def test_parse_stop_signal():
    result = _parse_single("THOUGHT: Task finished successfully.\n<STOP>")
    assert result.thought == "Task finished successfully."
    assert result.tool_name is None


def test_parse_tool_call():
    result = _parse_single(
        'THOUGHT: I need to fetch data\nTOOL: http_request\nARGS: {"url": "https://api.example.com", "method": "GET"}'
    )
    assert result.thought == "I need to fetch data"
    assert result.tool_name == "http_request"
    assert result.tool_input == {"url": "https://api.example.com", "method": "GET"}


def test_parse_tool_without_args():
    result = _parse_single("THOUGHT: Just checking\nTOOL: status_check\nARGS: {}")
    assert result.tool_name == "status_check"
    assert result.tool_input == {}


def test_parse_malformed_args():
    result = _parse_single("THOUGHT: Doing something\nTOOL: test_tool\nARGS: not valid json")
    assert result.tool_name == "test_tool"
    assert result.tool_input == {}


def test_parse_thought_only():
    result = _parse_single("This is just a thought, no structured output.")
    assert result.thought == "This is just a thought, no structured output."
    assert result.tool_name is None


def test_parse_multiline_thought():
    response = """THOUGHT: First line.
Second line of thought.
Third line.
TOOL: my_tool
ARGS: {"key": "value"}"""
    result = _parse_single(response)
    assert "First line" in result.thought
    assert "Second line" in result.thought
    assert result.tool_name == "my_tool"
    assert result.tool_input == {"key": "value"}


def test_parse_tool_takes_priority_over_stop():
    # When both TOOL: and <STOP> appear, the tool call takes priority
    result = _parse_single("THOUGHT: Done.\n<STOP>\nTOOL: my_tool")
    assert result.thought == "Done."
    assert result.tool_name == "my_tool"

    # When TOOL: appears before <STOP>, it should be honored
    result = _parse_single("THOUGHT: Doing work.\nTOOL: work_tool\nARGS: {}\n<STOP>")
    assert result.tool_name == "work_tool"


def test_parse_answer():
    result = _parse_single("ANSWER: 我是你的 AI 助手。")
    assert result.tool_name is None
    assert result.direct_answer == "我是你的 AI 助手。"

def test_parse_answer_with_stop():
    result = _parse_single("ANSWER: Hello!\n<STOP>")
    assert result.tool_name is None
    assert result.direct_answer == "Hello!"

def test_parse_answer_takes_priority():
    # ANSWER: should take priority even if TOOL: appears later
    result = _parse_single("ANSWER: Hello\nTOOL: ignored_tool")
    assert result.tool_name is None
    assert result.direct_answer == "Hello"


# ── 4.6 Parse fault tolerance ────────────────────────────────


def test_parse_empty_response():
    result = _parse_single("")
    assert result.thought == ""
    assert result.tool_name is None


def test_parse_response_with_only_tool():
    result = _parse_single("TOOL: direct_call\nARGS: {}")
    assert result.tool_name == "direct_call"


# ── 4.7 Multi-tool call parsing ────────────────────────────────


def test_parse_multi_tool():
    response = """THOUGHT: Do two things
TOOL: search
ARGS: {"q": "hello", "nested": {"inner": "value"}}
TOOL: echo
ARGS: {"msg": "world"}"""
    results = _parse_results(response)
    assert len(results) == 2
    assert results[0].tool_name == "search"
    assert results[0].tool_input == {"q": "hello", "nested": {"inner": "value"}}
    assert results[1].tool_name == "echo"
    assert results[1].tool_input == {"msg": "world"}


def test_parse_multi_tool_nested_json():
    """Nested JSON objects must not be truncated by non-greedy matching."""
    response = """THOUGHT: Multi step
TOOL: http_request
ARGS: {"url": "https://api.example.com", "headers": {"Authorization": "Bearer tok"}}
TOOL: file_op
ARGS: {"operation": "write", "path": "/tmp/x"}"""
    results = _parse_results(response)
    assert len(results) == 2
    assert results[0].tool_name == "http_request"
    assert results[0].tool_input["url"] == "https://api.example.com"
    assert results[0].tool_input["headers"]["Authorization"] == "Bearer tok"
    assert results[1].tool_name == "file_op"
    assert results[1].tool_input["operation"] == "write"


def test_parse_multi_tool_malformed_args():
    """One tool with malformed ARGS should not break the other."""
    response = """THOUGHT: test
TOOL: good
ARGS: {"ok": true}
TOOL: bad
ARGS: not json"""
    results = _parse_results(response)
    assert len(results) == 2
    assert results[0].tool_name == "good"
    assert results[0].tool_input == {"ok": True}
    assert results[1].tool_name == "bad"
    assert results[1].tool_input == {}


def test_parse_multi_tool_stop_ignored():
    """TOOL blocks before <STOP> should be honored."""
    response = """THOUGHT: finishing
TOOL: cleanup
ARGS: {"action": "flush"}
<STOP>"""
    results = _parse_results(response)
    assert len(results) == 1
    assert results[0].tool_name == "cleanup"
    assert results[0].tool_input == {"action": "flush"}


# ── 4.8 Edge cases from unified parse path ───────────────────


def test_parse_tool_at_start_of_response():
    """Response starts with TOOL: — no preceding newline."""
    result = _parse_single("TOOL: browser\nARGS: {\"url\": \"https://x.com\"}")
    assert result.tool_name == "browser"
    assert result.tool_input == {"url": "https://x.com"}


def test_parse_tool_no_space_after_colon():
    """TOOL:toolname (no space) through the unified split path."""
    result = _parse_single("THOUGHT: test\nTOOL:echo\nARGS: {\"msg\": \"hi\"}")
    assert result.tool_name == "echo"
    assert result.tool_input == {"msg": "hi"}


def test_parse_multi_tool_starting_with_tool():
    """Multi-tool response where first line is TOOL: (no THOUGHT)."""
    response = """TOOL: step1
ARGS: {"a": 1}
TOOL: step2
ARGS: {"b": 2}"""
    results = _parse_results(response)
    assert len(results) == 2
    assert results[0].tool_name == "step1"
    assert results[0].tool_input == {"a": 1}
    assert results[1].tool_name == "step2"
    assert results[1].tool_input == {"b": 2}


def test_parse_greedy_regex_boundary_protected_by_split():
    """Multiple JSON-like bodies — split prevents greedy match across tools."""
    response = """THOUGHT: fetch data
TOOL: http
ARGS: {"url": "https://api.example.com", "payload": {"nested": true}}
TOOL: log
ARGS: {"message": "done"}"""
    results = _parse_results(response)
    assert len(results) == 2
    assert results[0].tool_name == "http"
    assert results[0].tool_input["url"] == "https://api.example.com"
    assert results[0].tool_input["payload"] == {"nested": True}
    assert results[1].tool_name == "log"
    # The second args should NOT contain the first args' JSON
    assert results[1].tool_input == {"message": "done"}


def test_parse_single_tool_unified_path():
    """Single tool with leading newline before TOOL: — unified path, no fallback."""
    result = _parse_single("THOUGHT: one thing\nTOOL: fetch\nARGS: {\"url\": \"https://a\"}\ntrailing text")
    assert result.tool_name == "fetch"
    assert result.tool_input == {"url": "https://a"}


def test_parse_response_no_longer_importable():
    """_parse_response must not be importable from agent_kernel (dead code removed)."""
    with pytest.raises(ImportError):
        from harness.core.agent_kernel import _parse_response  # noqa: F401


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


# ── LLMAgentKernel _generate_stop_summary ────────────────────


@pytest.mark.asyncio
async def test_llm_kernel_stop_triggers_summary():
    """When <STOP> is detected, _generate_stop_summary produces direct_answer."""
    client = MockLLMClient([
        "<STOP>",
        "I have completed the task by fetching the data and saving it.",
    ])
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

    client = _FailingSummaryClient(["<STOP>"])  # only one response; summary call raises
    kernel = LLMAgentKernel(client)
    state = RunState(run_id="test")
    tool_defs: list[ToolDefinition] = []

    results = await kernel.think("do something", tool_defs, state)
    assert len(results) == 1
    assert results[0].tool_name is None
    assert results[0].direct_answer is None  # summary failed, no direct_answer set
