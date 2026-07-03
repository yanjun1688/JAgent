from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from harness.core.planner import Planner, PlanGuardrail
from harness.core.fold import RunState
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import RetryPolicy, ToolDefinition
from harness.storage.event_store import EventStore
from harness.tools.registry import ToolRegistry


class _MockLLM:
    """Minimal LLM double that records what messages it received."""
    def __init__(self):
        self.last_messages = None

    async def chat(self, messages, **kwargs):
        self.last_messages = messages
        return "Mock answer"


@pytest.fixture
def store():
    return EventStore(":memory:")


@pytest.fixture
def registry():
    r = ToolRegistry()
    r.register(
        ToolDefinition(
            name="echo", description="Echo",
            input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
            output_schema={"type": "object"},
            idempotency_key_fields=[],
            side_effects=[], timeout_ms=5000, retry_policy=RetryPolicy(),
        ),
        lambda x: {"ok": True},
    )
    return r


class TestPlannerParsePlan:
    def test_parse_plan_parameters_fallback(self):
        """_parse_plan should accept 'parameters' as alias for 'input'."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "parameters": {"msg": "hello"}, "depends_on": []}]}'
        plan, err = Planner._parse_plan(response)
        assert plan is not None, err
        assert len(plan.steps) == 1
        assert plan.steps[0].input == {"msg": "hello"}

    def test_parse_plan_input_takes_priority(self):
        """When both 'input' and 'parameters' exist, 'input' wins."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "input": {"msg": "from_input"}, "parameters": {"msg": "from_params"}}]}'
        plan, err = Planner._parse_plan(response)
        assert plan is not None, err
        assert plan.steps[0].input == {"msg": "from_input"}

    def test_parse_plan_missing_input_returns_err(self):
        """When neither 'input' nor 'parameters' exists, return error."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "depends_on": []}]}'
        plan, err = Planner._parse_plan(response)
        assert plan is None
        assert "required property" in err

    def test_parse_plan_non_dict_input_returns_err(self):
        """When input is not a dict (e.g. string), return error."""
        response = '{"steps": [{"id": "s1", "tool": "echo", "input": "not_a_dict"}]}'
        plan, err = Planner._parse_plan(response)
        assert plan is None
        assert "not of type" in err

    def test_parse_plan_parameters_non_dict_returns_err(self):
        response = '{"steps": [{"id": "s1", "tool": "echo", "parameters": "bad"}]}'
        plan, err = Planner._parse_plan(response)
        assert plan is None
        assert "not of type 'object'" in err or "required property" in err

    def test_parse_plan_empty_steps(self):
        response = '{"steps": []}'
        plan, err = Planner._parse_plan(response)
        assert plan is not None, err
        assert len(plan.steps) == 0

    def test_parse_plan_malformed_json(self):
        plan, err = Planner._parse_plan("not json at all")
        assert plan is None
        assert err  # should be a non-empty error

    def test_parse_plan_code_fences(self):
        response = '```\n{"steps": [{"id": "s1", "tool": "echo", "input": {"msg": "hi"}}]}\n```'
        plan, err = Planner._parse_plan(response)
        assert plan is not None, err
        assert len(plan.steps) == 1


class TestPlanGuardrail:
    def test_validate_valid_plan(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[DagStep(id="s1", tool="echo", input={"msg": "hi"})])
        errors = guardrail.validate(plan)
        assert errors == []

    def test_validate_unknown_tool(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[DagStep(id="s1", tool="ghost_tool", input={})])
        errors = guardrail.validate(plan)
        assert any("unknown tool" in e for e in errors)

    def test_validate_duplicate_step_id(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}),
            DagStep(id="s1", tool="echo", input={}),
        ])
        errors = guardrail.validate(plan)
        assert any("Duplicate" in e for e in errors)

    def test_validate_cyclic_dependency(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}, depends_on=["s2"]),
            DagStep(id="s2", tool="echo", input={}, depends_on=["s1"]),
        ])
        errors = guardrail.validate(plan)
        assert any("Cycle" in e for e in errors)

    def test_validate_depends_on_unknown(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}, depends_on=["ghost"]),
        ])
        errors = guardrail.validate(plan)
        assert any("depends on unknown" in e for e in errors)

    def test_validate_empty_plan_passes(self, registry):
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[])
        errors = guardrail.validate(plan)
        assert errors == []

    def test_validate_max_parallel_is_warning_now(self, registry):
        """_check_max_parallel should no longer produce hard errors."""
        guardrail = PlanGuardrail(registry)
        plan = DagPlan(intent="test", steps=[
            DagStep(id="s1", tool="echo", input={}),
            DagStep(id="s2", tool="echo", input={}),
            DagStep(id="s3", tool="echo", input={}),
            DagStep(id="s4", tool="echo", input={}),  # 4 echoes in same layer, max_parallel=3
        ])
        errors = guardrail.validate(plan)
        # Should pass without errors (semaphore handles enforcement)
        assert errors == []


class TestPlanPromptBug1:
    """Tests for Bug 1: Planner prompt should NOT plan tool calls for conversation actions."""

    def test_prompt_contains_no_conversation_tool_rule(self, registry):
        """The PLAN prompt must instruct the LLM that answering is handled by the Answer phase."""
        from harness.core.system_prompt import AgentPhase, get_prompt

        prompt = get_prompt(AgentPhase.PLAN, step_schema="", tool_descriptions="", intent="test")
        assert "conversational responses are handled" in prompt.lower() or \
               "answer phase" in prompt.lower(), \
               "Prompt should tell Planner NOT to plan conversation/answer tool calls"

    async def test_planner_does_not_plan_conversation_tool(self, registry):
        """Planner receives a composite intent (tools + conversation) and should NOT output a fake 'answer' step."""
        from harness.core.llm_client import MockLLMClient

        registry.register(
            ToolDefinition(
                name="file_op", description="File operations",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
                retry_policy=RetryPolicy(),
            ),
            lambda x: {"success": True},
        )
        registry.register(
            ToolDefinition(
                name="browser", description="Browser control",
                input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
                retry_policy=RetryPolicy(),
            ),
            lambda x: {"success": True},
        )

        plan_json = (
            '{"intent": "Write 1+1=2 to file and visit Baidu",'
            '"steps": ['
            '{"id": "s1", "tool": "file_op", "input": {"path": "answer.txt"}},'
            '{"id": "s2", "tool": "browser", "input": {"url": "https://baidu.com"}, "depends_on": ["s1"]}'
            ']}'
        )
        llm = MockLLMClient(responses=[plan_json])
        planner = Planner(llm_client=llm, registry=registry)
        intent = "告诉我1+1等于几，然后写进文件，再访问百度"
        plan = await planner.plan(intent)
        assert plan is not None
        invalid_tools = {"answer", "respond", "say", "tell", "reply", "conversation", "chat"}
        for step in plan.steps:
            assert step.tool not in invalid_tools, (
                f"Bug 1: Planner should NOT plan a non-tool step like '{step.tool}'. "
                f"Conversation/answer must be left to the Answer phase."
            )


class TestPlanPromptBug2:
    """Tests for Bug 2: Planner should NOT compute/derive values itself."""

    def test_prompt_forbids_computed_values(self):
        """The PLAN prompt must explicitly forbid the Planner from computing values."""
        from harness.core.system_prompt import AgentPhase, get_prompt

        prompt = get_prompt(AgentPhase.PLAN, step_schema="", tool_descriptions="", intent="test")
        assert "never compute" in prompt.lower() or \
               "do not hardcode" in prompt.lower(), \
               "Prompt should forbid Planner from computing/hardcoding values"

    async def test_plan_accepts_data_flow_references(self, registry):
        """Verify _parse_plan correctly handles step inputs with $step_id.field references.

        This ensures the infrastructure supports data flow syntax, which is a
        prerequisite for the Planner to follow the "never compute" rule (Bug 2)
        and use $ references instead of hardcoded values (Bug 3).
        """
        from harness.core.llm_client import MockLLMClient

        registry.register(
            ToolDefinition(
                name="file_op", description="File ops",
                input_schema={"type": "object", "properties": {
                    "operation": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                }},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
                retry_policy=RetryPolicy(),
            ),
            lambda x: {"success": True},
        )
        registry.register(
            ToolDefinition(
                name="browser", description="Browser",
                input_schema={"type": "object", "properties": {
                    "action": {"type": "string"}, "url": {"type": "string"},
                }},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
                retry_policy=RetryPolicy(),
            ),
            lambda x: {"success": True},
        )

        plan_json = (
            '{"intent": "Calculate 1+1 and write result to file",'
            '"steps": ['
            '{"id": "s1", "tool": "browser", "input": {"action": "navigate", "url": "https://baidu.com"}},'
            '{"id": "s2", "tool": "file_op", "input": {"operation": "write", "path": "result.txt", "content": "$s1.answer"}, "depends_on": ["s1"]}'
            ']}'
        )
        llm = MockLLMClient(responses=[plan_json])
        planner = Planner(llm_client=llm, registry=registry)
        plan = await planner.plan("1+1等于几？计算后写文件并访问百度")
        assert plan is not None, "Plan should be valid even with $data_flow references in input"
        s2 = [s for s in plan.steps if s.id == "s2"]
        assert len(s2) == 1
        assert "$s1" in str(s2[0].input.get("content", "")), (
            f"Bug 2/3: Data flow reference should be preserved in step input, "
            f"not replaced with a hardcoded computed value. Got: {s2[0].input}"
        )


class TestPlanPromptBug3:
    """Tests for Bug 3: Data Flow should be properly demonstrated in the PLAN prompt."""

    def test_example2_uses_data_flow_reference(self):
        """Example 2 must NOT hardcode 'done' — it should use $s1.result instead."""
        from harness.core.system_prompt import AgentPhase, get_prompt

        prompt = get_prompt(AgentPhase.PLAN, step_schema="", tool_descriptions="", intent="test")
        assert '"content": "done"' not in prompt, (
            "Bug 3: Example 2 should NOT hardcode 'done' — use $s1.result instead"
        )
        assert "$s1.result" in prompt, (
            "Bug 3: Example 2 should demonstrate $s1.result data flow reference"
        )

    def test_data_flow_section_has_syntax_examples(self):
        """The Data Flow section must include syntax examples like $s1.result, $s1.body."""
        from harness.core.system_prompt import AgentPhase, get_prompt

        prompt = get_prompt(AgentPhase.PLAN, step_schema="", tool_descriptions="", intent="test")
        assert "Syntax:" in prompt or "$s1.result" in prompt, (
            "Bug 3: Data Flow section should explain $step_id.field syntax with examples"
        )
        assert "never hardcode" in prompt.lower(), (
            "Bug 3: Data Flow section should forbid hardcoding upstream values"
        )

    def test_example3_exists_with_data_flow(self):
        """Example 3 must demonstrate $step_id.field usage between dependent steps."""
        from harness.core.system_prompt import AgentPhase, get_prompt

        prompt = get_prompt(AgentPhase.PLAN, step_schema="", tool_descriptions="", intent="test")
        assert "Example 3" in prompt, (
            "Bug 3: Should have an Example 3 demonstrating data flow with $ references"
        )
        assert "$s1.body" in prompt, (
            "Bug 3: Example 3 should use $s1.body to show data flow between steps"
        )


class TestPlannerGenerateAnswerFeedback:
    """Tests that generate_answer() includes state.feedbacks in the LLM message."""

    async def test_generate_answer_includes_feedback(self):
        """FEATURE: generate_answer() includes state.feedbacks in the LLM message.

        The LLM receives feedback entries alongside tool results,
        enabling self-healing based on monitoring signals.
        """
        llm = _MockLLM()
        planner = Planner(llm_client=llm, registry=None, store=None)
        state = RunState(
            run_id="r1",
            intent="test",
            feedbacks=[
                _Feedback(seq=1, feedback_text="Too slow, please retry"),
                _Feedback(seq=2, feedback_text="Use the fast tool instead"),
            ],
        )
        result = await planner.generate_answer("test intent", state, feedback=None)
        assert llm.last_messages is not None
        user_msg = next(m for m in llm.last_messages if m["role"] == "user")
        content = user_msg["content"]
        assert "Too slow" in content
        assert "Use the fast tool" in content


class _Feedback:
    """Minimal FeedbackInjectedPayload stand-in."""
    def __init__(self, seq, feedback_text):
        self.seq = seq
        self.feedback_text = feedback_text


class _ToolCall:
    """Minimal ToolCalledPayload stand-in."""
    def __init__(self, tool_call_id, tool_name, input):
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.input = input


class TestAnswerContextBug5:
    """Bug 5: Answer context must include step number, tool name, input, output, status."""

    async def test_answer_context_has_step_info(self):
        """Each tool result in answer context shows step#, tool, input, output, status."""
        from harness.core.fold import ToolResult, ToolResultStatus

        llm = _MockLLM()
        planner = Planner(llm_client=llm, registry=None, store=None)

        state = RunState(
            run_id="r1",
            intent="write file and browse",
            tool_calls=[
                _ToolCall("tc1", "file_op", {"action": "write", "path": "/tmp/test.txt", "content": "hello world"}),
                _ToolCall("tc2", "browser", {"action": "navigate", "url": "https://example.com"}),
            ],
            tool_results=[
                ToolResult("tc1", "file_op", ToolResultStatus.COMPLETED, output={"success": True, "path": "/tmp/test.txt", "size": 11}, duration_ms=150),
                ToolResult("tc2", "browser", ToolResultStatus.SOFT_ERROR, output=None, error="Connection timeout", duration_ms=5000),
            ],
        )
        result = await planner.generate_answer("write file and browse", state, feedback=None)
        assert llm.last_messages is not None
        user_msg = next(m for m in llm.last_messages if m["role"] == "user")
        content = user_msg["content"]

        assert "Step 1" in content, f"Missing step number:\n{content[:500]}"
        assert "file_op" in content, f"Missing tool name:\n{content[:500]}"
        assert "completed" in content, f"Missing status:\n{content[:500]}"
        assert "/tmp/test.txt" in content, f"Missing input details:\n{content[:500]}"
        assert "hello world" in content, f"Missing input content:\n{content[:500]}"

        assert "Step 2" in content, f"Missing step number for step 2:\n{content[:500]}"
        assert "browser" in content, f"Missing tool name for step 2:\n{content[:500]}"
        assert "soft_error" in content, f"Missing status for step 2:\n{content[:500]}"
        assert "Connection timeout" in content, f"Missing error message:\n{content[:500]}"
        assert "https://example.com" in content, f"Missing input URL:\n{content[:500]}"

    async def test_answer_context_shows_duration(self):
        """Duration field should be included when non-zero."""
        from harness.core.fold import ToolResult, ToolResultStatus

        llm = _MockLLM()
        planner = Planner(llm_client=llm, registry=None, store=None)

        state = RunState(
            run_id="r1",
            intent="simple",
            tool_calls=[
                _ToolCall("tc1", "http", {"url": "https://api.example.com"}),
            ],
            tool_results=[
                ToolResult("tc1", "http", ToolResultStatus.COMPLETED, output={"status": 200}, duration_ms=320),
            ],
        )
        result = await planner.generate_answer("simple", state, feedback=None)
        assert llm.last_messages is not None
        user_msg = next(m for m in llm.last_messages if m["role"] == "user")
        content = user_msg["content"]
        assert "320ms" in content, f"Missing duration:\n{content[:500]}"

    async def test_answer_no_tool_results_produces_clean_output(self):
        """When there are no tool results, output should be clean (no broken sections)."""
        llm = _MockLLM()
        planner = Planner(llm_client=llm, registry=None, store=None)
        state = RunState(run_id="r1", intent="simple chat")
        result = await planner.generate_answer("simple chat", state, feedback=None)
        assert llm.last_messages is not None
        user_msg = next(m for m in llm.last_messages if m["role"] == "user")
        content = user_msg["content"]
        assert "simple chat" in content
        assert "Tool execution results" not in content, "Should not have tool section when no results"


class TestToolFilteringBug8:
    """Bug 8: Filter tool descriptions by intent relevance to reduce prompt size."""

    def test_keywords_extracted_from_tool_def(self):
        """_extract_tool_keywords should produce words from name, description, params."""
        from harness.models.tools import SideEffect, SuccessIndicator, ToolDefinition

        td = ToolDefinition(
            name="http_request",
            description="Send HTTP requests to remote servers",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target URL to fetch"},
                    "method": {"type": "string"},
                },
                "required": ["url"],
            },
            output_schema={},
            idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
        )
        kw = Planner._extract_tool_keywords(td)
        assert "http_request" in kw, "Should include tool name"
        assert "http" in kw, "Should include description words"
        assert "requests" in kw, "Should include description words"
        assert "url" in kw, "Should include parameter names"
        assert "target" in kw, "Should include parameter description words"

    def test_tools_filtered_by_intent_relevance(self):
        """_filter_tools_by_intent should keep matching tools and file_op."""
        from harness.models.tools import SideEffect, SuccessIndicator, ToolDefinition

        tools = [
            ToolDefinition(name="file_op", description="Read/write files", input_schema={}, output_schema={},
                           idempotency_key_fields=[], side_effects=[], timeout_ms=5000),
            ToolDefinition(name="browser", description="Browse web pages", input_schema={}, output_schema={},
                           idempotency_key_fields=[], side_effects=[], timeout_ms=5000),
            ToolDefinition(name="http_request", description="Send HTTP requests", input_schema={}, output_schema={},
                           idempotency_key_fields=[], side_effects=[], timeout_ms=5000),
        ]
        filtered = Planner._filter_tools_by_intent("navigate to a web page and capture screenshot", tools)
        filtered_names = {td.name for td in filtered}
        assert "browser" in filtered_names, "browser should match 'web page' intent"
        assert "file_op" in filtered_names, "file_op should always be included"
        assert "http_request" not in filtered_names, "http_request should not match browser intent"

    def test_filtered_empty_returns_all(self):
        """When no tools match, all tools should be returned (safety fallback)."""
        from harness.models.tools import SideEffect, SuccessIndicator, ToolDefinition

        tools = [
            ToolDefinition(name="browser", description="Browse web pages", input_schema={}, output_schema={},
                           idempotency_key_fields=[], side_effects=[], timeout_ms=5000),
        ]
        filtered = Planner._filter_tools_by_intent("calculate 2+2", tools)
        assert len(filtered) == 1, "Unmatched intent should return all tools as fallback"

    def test_build_tool_descriptions_filters_with_intent(self):
        """_build_tool_descriptions(intent=...) should filter, _build_tool_descriptions() shows all."""
        from harness.models.tools import SideEffect, SuccessIndicator, ToolDefinition

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="echo", description="Echo",
                input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
            ),
            lambda x: {"ok": True},
        )
        registry.register(
            ToolDefinition(
                name="browser", description="Browse web pages and take screenshots",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[SideEffect.EXTERNAL], timeout_ms=5000,
            ),
            lambda x: {"ok": True},
        )
        registry.register(
            ToolDefinition(
                name="mcp_call", description="Call MCP server tools",
                input_schema={"type": "object", "properties": {}},
                output_schema={"type": "object"},
                idempotency_key_fields=[], side_effects=[], timeout_ms=5000,
            ),
            lambda x: {"ok": True},
        )
        planner = Planner(llm_client=_MockLLM(), registry=registry, store=None)

        all_desc = planner._build_tool_descriptions()
        assert "browser" in all_desc, "Without intent, all tools should be included"
        assert "echo" in all_desc, "Without intent, all tools should be included"

        filtered = planner._build_tool_descriptions(intent="search the web and take screenshots")
        assert "browser" in filtered, "Browser should match web/screenshot intent"
        assert "echo" not in filtered, "echo should be filtered out for non-echo intent"
        assert "mcp_call" not in filtered, "mcp_call should be filtered out for browser intent"
