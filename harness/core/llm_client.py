"""LLM client abstraction (L4) — supports OpenAI and DeepSeek backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


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
