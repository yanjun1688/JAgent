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
_TOOL_RE = re.compile(r"TOOL:\s*(\S+)")
_ARGS_RE = re.compile(r"ARGS:\s*(\{.+\})", re.DOTALL)


def _parse_response(response: str) -> ThinkResult:
    answer_match = _ANSWER_RE.search(response)
    if answer_match:
        answer = answer_match.group(1).strip()
        thought = f"Answered directly: {answer[:80]}"
        return ThinkResult(thought=thought, tool_name=None, direct_answer=answer)

    thought_match = _THOUGHT_RE.search(response)
    thought = thought_match.group(1).strip() if thought_match else response.strip()

    tool_match = _TOOL_RE.search(response)
    args_match = _ARGS_RE.search(response)

    tool_name = tool_match.group(1) if tool_match else None
    tool_input: dict[str, Any] = {}
    if args_match:
        try:
            tool_input = json.loads(args_match.group(1))
        except json.JSONDecodeError:
            pass

    if tool_name:
        return ThinkResult(thought=thought, tool_name=tool_name, tool_input=tool_input)

    if _STOP_MARKER in response:
        return ThinkResult(thought=thought, tool_name=None)

    return ThinkResult(thought=thought, tool_name=None)


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

    async def think(
        self,
        intent: str,
        tool_defs: list[ToolDefinition],
        state: RunState,
        feedback: str | None = None,
    ) -> ThinkResult:
        self.think_calls.append({"intent": intent, "tool_defs": tool_defs, "state": state, "feedback": feedback})
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
        state: RunState,
        feedback: str | None = None,
    ) -> ThinkResult:
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
        result = _parse_response(response)
        if result.tool_name:
            _logger.info("[PARSE] → tool=%s args=%s thought=%.120s",
                         result.tool_name, result.tool_input, result.thought)
        else:
            _logger.info("[PARSE] → stop (thought: %.120s)", result.thought)
        return result
