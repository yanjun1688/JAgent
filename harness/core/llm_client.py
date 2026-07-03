"""LLM client abstraction (L4) — supports OpenAI-compatible backends.

Returns a structured ChatResponse (Pydantic v2) — tool_calls survive intact
from provider to Kernel, never flattened to text.  JSON parse failures on
tool_call.arguments are observable via _logger.warning and surface as
``{"_parse_error": raw}`` instead of silent pass.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from pydantic import BaseModel, Field

from harness.core.logger import agent_logger

_logger = agent_logger("llm")

_STOP_MARKER = "<STOP>"


class ToolCall(BaseModel):
    """A single provider-issued tool call — id is mandatory, never dropped."""

    id: str
    name: str
    arguments: dict[str, Any]


class ChatResponse(BaseModel):
    """Structured LLM response — tool_calls are first-class, not flattened."""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = ""
    raw: dict[str, Any] | None = None


class LLMClient(ABC):
    """Abstract LLM client — implemented for each provider in L5.

    Returns a structured ChatResponse. Structured tool_calls survive intact
    from provider to Kernel; the caller consumes ChatResponse.tool_calls
    directly without regex round-trips.
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResponse: ...


class MockLLMClient(LLMClient):
    """Deterministic mock for testing — returns pre-programmed responses.

    Accepts either string responses (auto-wrapped as ChatResponse.content)
    or ChatResponse objects for tool_call injection.
    """

    def __init__(self, responses: list[str | ChatResponse]) -> None:
        self.responses: list[ChatResponse] = [
            r if isinstance(r, ChatResponse) else ChatResponse(content=r)
            for r in responses
        ]
        self.calls: list[dict[str, Any]] = []
        self._idx = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self._idx >= len(self.responses):
            return ChatResponse(content=f"{_STOP_MARKER} Task complete.")
        response = self.responses[self._idx]
        self._idx += 1
        return response


class OpenAILLMClient(LLMClient):
    """Real LLM client for OpenAI-compatible APIs (Bailian, DeepSeek, OpenAI, etc.)."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        _logger.info("[LLM] Sending %d messages (%.0f chars) to %s",
                     len(messages), sum(len(str(m)) for m in messages), self.model)
        for i, m in enumerate(messages):
            _logger.info("[LLM]   msg[%d] role=%s\n%s", i, m.get("role", "?"), m.get("content", ""))

        _t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                _logger.error("[LLM] API responded %d: %s", resp.status_code, resp.text)
                resp.raise_for_status()
            data = resp.json()
        _ms = (time.monotonic() - _t0) * 1000

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        _logger.info("[LLM] Response in %dms (prompt=%d, completion=%d, total=%d)",
                     _ms, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens)

        choice = data["choices"][0]
        msg = choice.get("message", {})
        content = msg.get("content", "") or ""
        finish_reason = choice.get("finish_reason", "") or ""

        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id", "")
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(parsed, dict):
                    parsed = {"_parse_error": raw_args, "_value": parsed}
            except json.JSONDecodeError:
                _logger.warning(
                    "[LLM] tool_call arguments json decode failed: id=%s name=%s raw=%.200s",
                    tc_id, name, raw_args,
                )
                parsed = {"_parse_error": raw_args}
            tool_calls.append(ToolCall(id=tc_id, name=name, arguments=parsed))

        if tool_calls:
            tc_names = [tc.name for tc in tool_calls]
            _logger.info("[LLM] → tool_calls: %s [reason=%s]", ", ".join(tc_names), finish_reason)
        else:
            _logger.info("[LLM] → text (%d chars) [reason=%s]: %s",
                         len(content), finish_reason, content)

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=choice,
        )