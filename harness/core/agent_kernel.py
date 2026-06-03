"""Agent kernel implementations (L4) — mock + LLM-backed kernels."""

from __future__ import annotations

import json
import re
from typing import Any

from harness.core.fold import RunState
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
    """Deterministic kernel for testing — returns pre-programmed ThinkResults.

    状态说明（避免重入 Bug）：
      self._idx 记录已消费的响应数。首次 think() 返回 responses[0]，第二次返回
      responses[1]……以此类推。当 _idx 超过响应列表长度时，返回 tool_name=None 的
      终止信号。

      正因为有 _idx 这个内部状态， 同一个 MockAgentKernel 实例不能跨多个 run 共享。
      如果 kernel_factory 返回固定实例，第二个 run 调用 think() 时 _idx 已经越界，
      会直接返回 tool_name=None → 只产生 3 个事件（RunStarted + AgentThought +
      RunCompleted）就结束，工具逻辑完全不执行。

      修复：kernel_factory 必须是工厂函数（每次调都 new 一个实例），而非单例引用。
      参见 harness/api/serve.py 中 kernel_factory 的实现模式。
    """

    def __init__(self, responses: list[ThinkResult]) -> None:
        self.responses = responses
        self._idx = 0
        self.think_calls: list[dict[str, Any]] = []

    async def think(self, intent: str, tool_defs: list[ToolDefinition], state: RunState) -> ThinkResult:
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
