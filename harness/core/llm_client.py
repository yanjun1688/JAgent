"""LLM client abstraction (L4) — supports OpenAI and DeepSeek backends."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from harness.core.logger import agent_logger

_logger = agent_logger("llm")

_STOP_MARKER = "<STOP>"


class LLMClient(ABC):
    """Abstract LLM client — implemented for each provider in L5.

    Returns a response string. Structured output parsing (JSON tool call
    extraction) is handled by the caller.
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str: ...


class MockLLMClient(LLMClient):
    """Deterministic mock for testing — returns pre-programmed responses."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self._idx = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        self.calls.append({"messages": messages, "tools": tools})
        if self._idx >= len(self.responses):
            return "<STOP> Task complete."
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
    ) -> str:
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

        _t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                _logger.error("[LLM] API responded %d: %.200s", resp.status_code, resp.text)
                resp.raise_for_status()
            data = resp.json()
        _ms = (time.monotonic() - _t0) * 1000

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        _logger.info("[LLM] Response in %dms (prompt=%d, completion=%d, total=%d)",
                     _ms, prompt_tokens, completion_tokens, prompt_tokens + completion_tokens)

        choice = data["choices"][0]
        msg = choice["message"]
        content = msg.get("content", "") or ""

        finish_reason = choice.get("finish_reason", "")

        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                parsed = json.loads(raw_args)
                args_str = json.dumps(parsed, ensure_ascii=False)
            except json.JSONDecodeError:
                args_str = raw_args

            if content:
                _logger.info("[LLM] → tool_call: %s(%s) [reason=%s] thought: %.200s",
                             fn.get("name", "?"), args_str[:300], finish_reason, content)
                return f"THOUGHT: {content}\nTOOL: {fn.get('name', '')}\nARGS: {args_str}"
            _logger.info("[LLM] → tool_call: %s(%s) [reason=%s]",
                         fn.get("name", "?"), args_str[:300], finish_reason)
            return f"THOUGHT: Calling tool {fn.get('name', '')}\nTOOL: {fn.get('name', '')}\nARGS: {args_str}"

        _logger.info("[LLM] → text (%d chars) [reason=%s]: %.300s",
                     len(content), finish_reason, content)
        return content
