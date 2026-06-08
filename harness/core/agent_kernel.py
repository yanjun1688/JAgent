"""Agent kernel implementations (L4) — mock + LLM-backed kernels."""

from __future__ import annotations

import json
import re
from typing import Any

from harness.core.fold import RunState
from harness.core.llm_client import LLMClient
from harness.core.logger import agent_logger
from harness.core.scheduler import AgentKernel, ThinkResult
from harness.core.system_prompt import build_system_prompt, build_tool_schemas
from harness.models.events import EpisodeSummary
from harness.models.tools import ToolDefinition

_logger = agent_logger("kernel")

_STOP_MARKER = "<STOP>"
_ANSWER_RE = re.compile(r"ANSWER:\s*(.+?)(?:\n|$)")
_THOUGHT_RE = re.compile(r"THOUGHT:\s*(.+?)(?:\nTOOL:|\nANSWER:|\n<STOP>|$)", re.DOTALL)
_TOOL_SPLIT_RE = re.compile(r"\nTOOL: ")
_ARGS_GREEDY_RE = re.compile(r"ARGS:\s*(\{.*\})", re.DOTALL)

_EXECUTION_MODE_DEFAULT = "serial"


def _parse_segment(segment: str) -> tuple[str | None, dict[str, Any]]:
    """Parse a single tool segment into (tool_name, tool_input).

    segment looks like: 'tool_name\nARGS: {"key": "val"}\n...'
    Uses greedy {.*} ARGS extraction since each segment is guaranteed
    to contain at most one tool definition.
    """
    seg = segment.strip()
    if not seg:
        return None, {}

    name_end = seg.find("\n")
    tool_name = seg[:name_end].strip() if name_end >= 0 else seg.strip()

    args_match = _ARGS_GREEDY_RE.search(seg)
    if args_match:
        try:
            return tool_name, json.loads(args_match.group(1))
        except json.JSONDecodeError:
            pass
    return tool_name, {}


def _parse_results(response: str) -> list[ThinkResult]:
    answer_match = _ANSWER_RE.search(response)
    if answer_match:
        answer = answer_match.group(1).strip()
        thought = f"Answered directly: {answer[:80]}"
        return [ThinkResult(thought=thought, tool_name=None, direct_answer=answer)]

    thought_match = _THOUGHT_RE.search(response)
    thought = thought_match.group(1).strip() if thought_match else response.strip()

    # Split by \nTOOL: — each segment after the first is one tool definition.
    # This avoids greedy-matching across tool boundaries.
    segments = _TOOL_SPLIT_RE.split(response)
    if len(segments) > 1:
        seen_thought = False
        results = []
        for seg in segments:
            if not seen_thought:
                seen_thought = True
                continue
            tool_name, tool_input = _parse_segment(seg)
            if tool_name:
                results.append(ThinkResult(thought=thought, tool_name=tool_name, tool_input=tool_input))
        if results:
            return results

    # Single-tool fallback: separate TOOL / ARGS scan
    tool_fb = re.search(r"TOOL:\s*(\S+)", response)
    if tool_fb:
        tool_name = tool_fb.group(1)
        tool_input: dict[str, Any] = {}
        args_fb = _ARGS_GREEDY_RE.search(response)
        if args_fb:
            try:
                tool_input = json.loads(args_fb.group(1))
            except json.JSONDecodeError:
                pass
        return [ThinkResult(thought=thought, tool_name=tool_name, tool_input=tool_input)]

    if _STOP_MARKER in response:
        return [ThinkResult(thought=thought, tool_name=None)]

    return [ThinkResult(thought=thought, tool_name=None)]


# Deprecated — kept for test compat, use _parse_results
def _parse_response(response: str) -> ThinkResult:
    return _parse_results(response)[0]


class MockAgentKernel(AgentKernel):
    """Deterministic kernel for testing — returns pre-programmed ThinkResults.

    State note (reentrancy guard):
      self._idx tracks consumed responses. First think() returns responses[0],
      second returns responses[1], etc. When _idx exceeds list length, returns
      tool_name=None (stop signal).

      Because of _idx internal state, a single MockAgentKernel instance cannot
      be shared across multiple runs. If kernel_factory returns a fixed instance,
      the second run's think() will find _idx exhausted and emit only 3 events
      (RunStarted + AgentThought + RunCompleted).

      Fix: kernel_factory must be a factory function (creates a new instance
      each call), not a singleton reference.
    """

    def __init__(self, responses: list[ThinkResult]) -> None:
        self.responses = responses
        self._idx = 0
        self.think_calls: list[dict[str, Any]] = []

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> list[ThinkResult]:
        self.think_calls.append({"intent": intent, "tool_defs": tool_defs, "state": state, "feedback": feedback})
        if self._idx >= len(self.responses):
            return [ThinkResult(thought="Task complete (no more pre-programmed responses)")]
        result = self.responses[self._idx]
        self._idx += 1
        return [result]


class LLMAgentKernel(AgentKernel):
    """Real LLM-backed kernel using OpenAI/DeepSeek API."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> list[ThinkResult]:
        system_prompt = build_system_prompt(intent, tool_defs)
        schemas = build_tool_schemas(tool_defs)

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        if feedback:
            messages.append({"role": "system", "content": f"## Monitoring Feedback\n{feedback}"})

        # When a context summary is available (from ContextManager compression),
        # use it plus recent items instead of the full 5-item window.
        # The keep_recent_count comes from the compression mode:
        #   normal → 2, emergency → 3 (keep 3 recent rounds untouched)
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
            window = max(state.keep_recent_count, 2)
        else:
            window = 5

        timeline: list[tuple[str, Any]] = []
        for t in state.thought_history[-window:]:
            if hasattr(t, "seq"):
                timeline.append(("thought", t))
        for tr in state.tool_results[-window:]:
            if hasattr(tr, "event_seq"):
                timeline.append(("result", tr))
        timeline.sort(key=lambda x: x[1].seq if x[0] == "thought" else x[1].event_seq)

        for kind, item in timeline:
            if kind == "thought":
                choice = f" ({item.tool_choice})" if item.tool_choice else ""
                messages.append({"role": "assistant", "content": f"THOUGHT{choice}: {item.thought}"})
            else:
                content = f"Tool '{item.tool_name}' result ({item.status}): {item.output or item.error}"
                messages.append({"role": "user", "content": content})

        response = await self.client.chat(messages, tools=schemas)
        results = _parse_results(response)

        tool_names = [r.tool_name for r in results if r.tool_name]
        if tool_names:
            _logger.info("[PARSE] → %d tool(s): %s", len(tool_names), ", ".join(tool_names))

        # Stop detected with single result and no direct answer — generate summary
        # Only when explicit stop/answer markers exist (not on format anomalies)
        if len(results) == 1 and results[0].tool_name is None and not results[0].direct_answer and ("<STOP>" in response or "ANSWER:" in response):
            result = results[0]
            summary_messages = [
                {"role": "system", "content": "You are a helpful assistant. Summarize the completed task for the user in plain text. Do not use any special format prefixes like ANSWER: or THOUGHT:. Just write naturally."},
                *messages[1:],
                {"role": "assistant", "content": response},
                {"role": "user", "content": "The task is now complete. Based on everything done above, provide a brief final response to the user summarizing what was accomplished. Be concise and helpful. Do not use any tools."},
            ]
            try:
                summary = await self.client.chat(summary_messages, max_tokens=512)
                summary = summary.removeprefix("ANSWER:").removeprefix("THOUGHT:").strip()
                _logger.info("[summary] Generated user-facing answer: %.200s", summary)
                results[0] = ThinkResult(thought=result.thought, direct_answer=summary)
            except Exception as exc:
                _logger.warning("[summary] Failed to generate: %s", exc)

        _logger.info("[PARSE] → %d result(s)", len(results))
        return results
