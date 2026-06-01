"""Agent kernel implementations (L4) — mock + LLM-backed kernels."""

from __future__ import annotations

import json
import re
from typing import Any

from harness.core.llm_client import LLMClient
from harness.core.scheduler import AgentKernel, ThinkResult
from harness.core.system_prompt import build_system_prompt, build_tool_schemas
from harness.models.tools import ToolDefinition

_STOP_MARKER = "<STOP>"
_THOUGHT_RE = re.compile(r"THOUGHT:\s*(.+?)(?:\nTOOL:|\n<STOP>|$)", re.DOTALL)
_TOOL_RE = re.compile(r"TOOL:\s*(\S+)")
_ARGS_RE = re.compile(r"ARGS:\s*(\{.+\})", re.DOTALL)


def _parse_response(response: str) -> ThinkResult:
    thought_match = _THOUGHT_RE.search(response)
    thought = thought_match.group(1).strip() if thought_match else response.strip()

    if _STOP_MARKER in response:
        return ThinkResult(thought=thought, tool_name=None)

    tool_match = _TOOL_RE.search(response)
    args_match = _ARGS_RE.search(response)

    tool_name = tool_match.group(1) if tool_match else None
    tool_input: dict[str, Any] = {}
    if args_match:
        try:
            tool_input = json.loads(args_match.group(1))
        except json.JSONDecodeError:
            pass

    return ThinkResult(thought=thought, tool_name=tool_name, tool_input=tool_input)


class MockAgentKernel(AgentKernel):
    """Deterministic kernel for testing — returns pre-programmed ThinkResults."""

    def __init__(self, responses: list[ThinkResult]) -> None:
        self.responses = responses
        self._idx = 0
        self.think_calls: list[dict[str, Any]] = []

    async def think(self, intent: str, tool_defs: list[ToolDefinition], state) -> ThinkResult:
        self.think_calls.append({"intent": intent, "tool_defs": tool_defs, "state": state})
        if self._idx >= len(self.responses):
            return ThinkResult(thought="Task complete (no more pre-programmed responses)")
        result = self.responses[self._idx]
        self._idx += 1
        return result


class LLMAgentKernel(AgentKernel):
    """Real LLM-backed kernel using OpenAI/DeepSeek API."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state,
    ) -> ThinkResult:
        system_prompt = build_system_prompt(intent, tool_defs)
        schemas = build_tool_schemas(tool_defs)

        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        for thought in state.thought_history[-5:]:
            messages.append({"role": "assistant", "content": f"THOUGHT: {thought.thought}"})

        for tr in state.tool_results[-5:]:
            content = f"Tool '{tr.tool_name}' result ({tr.status}): {tr.output or tr.error}"
            messages.append({"role": "user", "content": content})

        response = await self.client.chat(messages, tools=schemas)
        return _parse_response(response)
