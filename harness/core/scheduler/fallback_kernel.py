"""_FallbackKernel — wraps LLMClient as AgentKernel for serial fallback path."""

from __future__ import annotations

from typing import Any

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.scheduler.base import ThinkResult, AgentKernel
from harness.core.system_prompt import AgentPhase, get_prompt
from harness.models.events import EpisodeSummary
from harness.models.tools import ToolDefinition


class _FallbackKernel(AgentKernel):
    """Wraps LLMClient as AgentKernel for fallback from serial AgentLoopScheduler.

    Uses the old TOOL:/ARGS:/ANSWER:/<STOP> format for LLM interaction.
    """

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> list[ThinkResult]:
        from harness.core.agent_kernel import _parse_results

        tool_list = "\n".join(
            f"- **{td.name}**: {td.description}"
            + (" (dangerous — requires confirmation)" if td.requires_confirmation else "")
            for td in tool_defs
        ) or "(no tools available)"
        system_prompt = get_prompt(AgentPhase.SERIAL_THINK, intent=intent, tool_list=tool_list)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        if feedback:
            messages.append({"role": "system", "content": f"## Monitoring Feedback\n{feedback}"})

        if state.summary:
            if isinstance(state.summary, EpisodeSummary):
                parts = []
                if state.summary.key_decisions:
                    parts.append(f"Key decisions: {', '.join(state.summary.key_decisions)}")
                if state.summary.tools_used:
                    parts.append(f"Tools used: {', '.join(state.summary.tools_used)}")
                if state.summary.key_findings:
                    parts.append(f"Key findings: {', '.join(state.summary.key_findings)}")
                if state.summary.errors_encountered:
                    parts.append(f"Errors: {', '.join(state.summary.errors_encountered)}")
                if state.summary.current_plan:
                    parts.append(f"Current plan: {state.summary.current_plan}")
                summary_text = "\n".join(parts)
            else:
                summary_text = state.summary
            messages.append({"role": "system", "content": f"Previous context summary:\n{summary_text}"})

        window = max(getattr(state, "keep_recent_count", 0), 5)
        timeline: list[tuple[str, Any]] = []
        for t in state.thought_history[-window:]:
            timeline.append(("thought", t))
        for tr in state.tool_results[-window:]:
            timeline.append(("result", tr))
        timeline.sort(key=lambda x: x[1].seq if x[0] == "thought" else x[1].event_seq)

        for kind, item in timeline:
            if kind == "thought":
                choice = f" ({item.tool_choice})" if item.tool_choice else ""
                messages.append({"role": "assistant", "content": f"THOUGHT{choice}: {item.thought}"})
            else:
                content = f"Tool '{item.tool_name}' result ({item.status}): {item.output or item.error}"
                messages.append({"role": "user", "content": content})

        response = await self.client.chat(messages)
        results = _parse_results(response)

        if len(results) == 1 and results[0].tool_name is None and not results[0].direct_answer and ("<STOP>" in response or "ANSWER:" in response):
            result = results[0]
            summary_messages = [
                {"role": "system", "content": "You are a helpful assistant. Summarize the completed task for the user in plain text."},
                *messages[1:],
                {"role": "assistant", "content": response},
                {"role": "user", "content": "The task is now complete. Provide a brief final response summarizing what was accomplished."},
            ]
            try:
                summary = await self.client.chat(summary_messages, max_tokens=512)
                summary = summary.removeprefix("ANSWER:").removeprefix("THOUGHT:").strip()
                results[0] = ThinkResult(thought=result.thought, direct_answer=summary)
            except Exception:
                pass

        return results
