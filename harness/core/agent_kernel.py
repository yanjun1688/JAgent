"""Agent kernel implementations (L4) — LLM-backed kernel.

Consumes ChatResponse (structured tool_calls) directly from LLMClient — no
regex round-trip, tool_call_id preserved end-to-end.  Multi-turn history is
rebuilt per OpenAI convention (assistant.tool_calls + role=tool pairs).
"""

from __future__ import annotations

from typing import Any

from harness.core.fold import RunState, ToolResult
from harness.core.llm_client import ChatResponse, LLMClient
from harness.core.logger import agent_logger
from harness.core.scheduler import AgentKernel, ThinkResult
from harness.core.system_prompt import AgentPhase, build_tool_schemas, get_prompt
from harness.models.events import Episode
from harness.models.tools import ToolDefinition

_logger = agent_logger("kernel")

_STOP_MARKER = "<STOP>"


def _is_stop_signal(text: str) -> bool:
    return _STOP_MARKER in text or "ANSWER:" in text


def _extract_answer(text: str) -> str | None:
    idx = text.find("ANSWER:")
    if idx < 0:
        return None
    after = text[idx + len("ANSWER:") :].strip()
    if not after:
        return None
    end = after.find("\n")
    return after.split("\n", 1)[0].strip() if end < 0 else after[:end].strip()


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
    """Real LLM-backed kernel using OpenAI-compatible APIs.

    Uses function-calling tools API. Returns structured ChatResponse.tool_calls
    preserved with id → ThinkResult.tool_call_id. Falls back to text ANSWER/
    <STOP> parsing only when the model emits plain content (no tool_calls).
    """

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def _generate_stop_summary(self, messages: list[dict[str, Any]], response_text: str) -> str | None:
        summary_messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant. Summarize the completed task for the user in plain text. "
                    "Do not use any special format prefixes like ANSWER: or THOUGHT:. Just write naturally."
                ),
            },
            *messages[1:],
            {"role": "assistant", "content": response_text},
            {
                "role": "user",
                "content": (
                    "The task is now complete. Based on everything done above, provide a brief final response "
                    "to the user summarizing what was accomplished. Be concise and helpful. Do not use any tools."
                ),
            },
        ]
        try:
            resp = await self.client.chat(summary_messages, max_tokens=512)
            summary = resp.content.removeprefix("ANSWER:").removeprefix("THOUGHT:").strip()
            _logger.info("[summary] Generated user-facing answer: %s", summary)
            return summary
        except Exception as exc:
            _logger.warning("[summary] Failed to generate: %s", exc)
            return None

    def _build_history_messages(self, state: RunState) -> list[dict[str, Any]]:
        """Rebuild multi-turn history per OpenAI tool_calls protocol.

        Walking a seq-ordered timeline of (thought | result) entries:
          - a `thought` opens a new assistant message and flushes the previous
            assistant plus its 1:1 paired `role=tool` result messages
          - a `result` attaches a tool_call to the current assistant message
            (creating an empty-content assistant if none is open — happens
            when the preceding thought was outside the window)
        On flush, each assistant.tool_calls id must have a matching role=tool
        message appended immediately after, per OpenAI strict pairing rules.
        """
        window = max(state.keep_recent_count, 2) if state.summary else 5
        window = max(window, 2)
        thoughts = state.thought_history[-window:]
        results = state.tool_results[-window:]
        timeline: list[tuple[int, str, Any]] = []
        for t in thoughts:
            timeline.append((t.seq, "thought", t))
        for tr in results:
            timeline.append((tr.event_seq, "result", tr))
        timeline.sort(key=lambda x: x[0])

        messages: list[dict[str, Any]] = []
        current_assistant: dict[str, Any] | None = None
        pending_tool_call_ids: list[str] = []

        def _flush() -> None:
            nonlocal current_assistant, pending_tool_call_ids
            if current_assistant is None:
                return
            messages.append(current_assistant)
            for tcid in pending_tool_call_ids:
                tr_by_id = next((r for r in results if r.tool_call_id == tcid), None)
                if tr_by_id is not None:
                    messages.append(self._tool_result_message(tr_by_id))
            current_assistant = None
            pending_tool_call_ids = []

        for _, kind, item in timeline:
            if kind == "thought":
                _flush()
                current_assistant = {"role": "assistant", "content": item.thought}
            else:
                tool_result: ToolResult = item
                if current_assistant is None:
                    current_assistant = {"role": "assistant", "content": ""}
                tool_calls_list = current_assistant.setdefault("tool_calls", [])
                tool_calls_list.append(
                    {
                        "id": tool_result.tool_call_id,
                        "type": "function",
                        "function": {"name": tool_result.tool_name, "arguments": "{}"},
                    }
                )
                pending_tool_call_ids.append(tool_result.tool_call_id)
        _flush()
        return messages

    @staticmethod
    def _tool_result_message(tr: ToolResult) -> dict[str, Any]:
        status_str = tr.status.value if hasattr(tr.status, "value") else str(tr.status)
        body = tr.output if tr.output is not None else (tr.error or "")
        content = f"{status_str}: {body}"
        return {"role": "tool", "tool_call_id": tr.tool_call_id, "content": content}

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> list[ThinkResult]:
        schemas = build_tool_schemas(tool_defs) if tool_defs else None
        use_fn = schemas is not None
        phase = AgentPhase.SERIAL_THINK_FN if use_fn else AgentPhase.SERIAL_THINK_TEXT
        tool_list = (
            "\n".join(
                f"- **{td.name}**: {td.description}"
                + (" (dangerous — requires confirmation)" if td.requires_confirmation else "")
                for td in tool_defs
            )
            or "(no tools available)"
        )
        system_prompt = get_prompt(phase, intent=intent, tool_list=tool_list)
        _logger.info(
            "[think] phase=%s len=%d chars intent=%s tools=%d",
            phase.value,
            len(system_prompt),
            intent[:80],
            len(tool_defs),
        )

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        if feedback:
            messages.append({"role": "system", "content": f"## Monitoring Feedback\n{feedback}"})

        if state.summary:
            if isinstance(state.summary, Episode):
                parts = []
                if state.summary.title:
                    parts.append(f"Title: {state.summary.title}")
                if state.summary.summary:
                    parts.append(f"Summary: {state.summary.summary}")
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

        messages.extend(self._build_history_messages(state))

        _logger.info(
            "[think] %s: %d messages, %d chars total",
            phase.value,
            len(messages),
            sum(len(m.get("content", "")) for m in messages),
        )

        resp = await self.client.chat(messages, tools=schemas) if schemas else await self.client.chat(messages)
        return await self._consume_response(resp, messages)

    async def _consume_response(self, resp: ChatResponse, messages: list[dict[str, Any]]) -> list[ThinkResult]:
        results: list[ThinkResult] = []
        if resp.tool_calls:
            thought = resp.content or ""
            for tc in resp.tool_calls:
                results.append(
                    ThinkResult(
                        thought=thought,
                        tool_name=tc.name,
                        tool_input=tc.arguments,
                        tool_call_id=tc.id,
                    )
                )
            tc_names = [r.tool_name for r in results if r.tool_name]
            if tc_names:
                _logger.info("[PARSE] → %d tool(s): %s", len(tc_names), ", ".join(tc_names))
            return results

        content = resp.content or ""
        if not content.strip():
            _logger.warning("[PARSE] Empty response (no content and no tool_calls) finish=%s", resp.finish_reason)

        answer = _extract_answer(content)
        if answer is not None:
            thought = f"Answered directly: {answer[:80]}"
            return [ThinkResult(thought=thought, tool_name=None, direct_answer=answer)]

        if _STOP_MARKER in content or "ANSWER:" in content:
            summary = await self._generate_stop_summary(messages, content)
            if summary:
                thought = content
                if "\n<STOP>" in thought:
                    thought = thought.split("\n<STOP>")[0]
                if _STOP_MARKER in thought:
                    thought = thought.split(_STOP_MARKER)[0]
                if thought.startswith("THOUGHT:"):
                    thought = thought[len("THOUGHT:") :].strip()
                return [ThinkResult(thought=thought[:200], tool_name=None, direct_answer=summary)]
            if _STOP_MARKER in content:
                stop_idx = content.find(_STOP_MARKER)
                thought = content[:stop_idx].strip()
                if thought.startswith("THOUGHT:"):
                    thought = thought[len("THOUGHT:") :].strip()
                return [ThinkResult(thought=thought, tool_name=None)]
            return [ThinkResult(thought=content.strip(), tool_name=None)]

        _logger.info("[PARSE] → plain thought (no tool, no stop, no answer)")
        return [ThinkResult(thought=content.strip(), tool_name=None)]
