"""T-R8b-5 regression: prompt-registry residency convergence.

Pins the three convergences done in T-R8b-5:
1. contract-extraction prompts live in the phase registry (AgentPhase.EXTRACT /
   EXTRACT_RETRY) and still accept their runtime variables.
2. the dead ``AgentPhase.SERIAL_THINK`` registration is gone (only the FN/TEXT
   consumers remain), so no phase is registered without a consumer.
3. ``build_plan_prompt`` injects conversation-context / feedback through explicit
   template placeholders, not a literal ``"## User Intent\\n"`` .replace anchor
   (a template rename can no longer silently drop the injection).
"""

from __future__ import annotations

from harness import MockLLMClient
from harness.core.contract_extractor import ContractExtractor
from harness.core.planner.prompts import build_plan_prompt
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.tools.registry import ToolRegistry


class TestDeadRegistrationRemoved:
    def test_serial_think_phase_removed(self):
        names = {p.name for p in AgentPhase}
        assert "SERIAL_THINK" not in names
        # The real consumers remain registered.
        assert "SERIAL_THINK_FN" in names
        assert "SERIAL_THINK_TEXT" in names

    def test_serial_think_no_longer_resolvable(self):
        assert not hasattr(AgentPhase, "SERIAL_THINK")


class TestExtractPromptsRegistered:
    def test_extract_prompt_takes_runtime_vars(self):
        prompt = get_prompt(AgentPhase.EXTRACT, intent="read /tmp/a.txt", tool_names="file_op, http_request")
        assert "read /tmp/a.txt" in prompt
        assert "file_op, http_request" in prompt
        assert "required_operations" in prompt

    def test_extract_retry_prompt_takes_error_and_tools(self):
        prompt = get_prompt(AgentPhase.EXTRACT_RETRY, error="tool 'x' unknown", tool_names="file_op")
        assert "tool 'x' unknown" in prompt
        assert "file_op" in prompt

    async def test_contract_extractor_uses_registry_prompt(self):
        llm = MockLLMClient(['{"required_operations": []}'])
        ex = ContractExtractor(llm, ToolRegistry(), max_retries=1)
        await ex.extract("just chat")
        system_msg = llm.calls[0]["messages"][0]["content"]
        assert "Extract the REQUIRED hard-delivery operations" in system_msg


class TestPlanPromptStructuredInjection:
    def test_context_and_feedback_injected_before_user_intent(self):
        prompt = build_plan_prompt(
            ToolRegistry(),
            "do the thing",
            feedback="use http_request instead",
            conversation_context="earlier turn",
        )
        assert "## Conversation Context (reference only)" in prompt
        assert "earlier turn" in prompt
        assert "## System Monitoring Feedback" in prompt
        assert "use http_request instead" in prompt

        i_ctx = prompt.index("## Conversation Context (reference only)")
        i_fb = prompt.index("## System Monitoring Feedback")
        i_intent = prompt.index("## User Intent")
        assert i_ctx < i_fb < i_intent

    def test_no_context_or_feedback_adds_no_sections(self):
        prompt = build_plan_prompt(ToolRegistry(), "chat only")
        assert "## Conversation Context" not in prompt
        assert "## System Monitoring Feedback" not in prompt
        assert "## User Intent" in prompt


class TestSummarizePromptContract:
    @staticmethod
    def _prompt():
        return get_prompt(AgentPhase.SUMMARIZE)

    def test_role_and_replacement_semantics(self):
        prompt = self._prompt()
        assert "episodic memory writer" in prompt
        assert "REPLACE" in prompt

    def test_fidelity_clauses(self):
        prompt = self._prompt()
        assert "Fidelity rules" in prompt
        assert "verbatim" in prompt
        assert "Do not infer" in prompt
        assert "uncertain or unverified" in prompt

    def test_output_contract_keys(self):
        prompt = self._prompt()
        for key in (
            '"title"',
            '"summary"',
            '"key_decisions"',
            '"tools_used"',
            '"key_findings"',
            '"errors_encountered"',
            '"current_plan"',
        ):
            assert key in prompt

    def test_format_clause(self):
        assert "ONLY valid JSON" in self._prompt()

    def test_old_phrasing_gone(self):
        assert "You are a context compression system" not in self._prompt()

    def test_resolvable_without_variables(self):
        assert get_prompt(AgentPhase.SUMMARIZE)
