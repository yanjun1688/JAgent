"""Token counter abstraction — pluggable token counting with auto-fallback.

Strategies (priority order):
  1. ProviderTokenCounter — remote tokenize API (requires .env config)
  2. TiktokenTokenCounter — local tiktoken (default, no network)
  3. HeuristicTokenCounter — char × 0.25 (final fallback)

Factory: create_token_counter() reads TOKEN_COUNTER_STRATEGY env var.
Runtime fallback: Provider catches API failures and delegates transparently.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

_logger = logging.getLogger("harness.token_counter")


class TokenCounter(ABC):
    """Abstract token counter with auto-fallback chain."""

    @abstractmethod
    async def count(self, text: str) -> int: ...

    async def count_messages(self, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            total += await self.count(str(m.get("content", "")))
        return total


class TiktokenTokenCounter(TokenCounter):
    """Local tiktoken implementation. Default for Phase 1."""

    def __init__(self, model: str = "cl100k_base"):
        import tiktoken

        self.encoding = tiktoken.get_encoding(model)

    async def count(self, text: str) -> int:
        return len(self.encoding.encode(text or ""))


class HeuristicTokenCounter(TokenCounter):
    """Fallback when tiktoken is unavailable: char_count * 0.25."""

    async def count(self, text: str) -> int:
        return max(1, int(len(text or "") * 0.25))


class ProviderTokenCounter(TokenCounter):
    """Remote tokenize API implementation.

    Configured via .env:
      TOKEN_COUNTER_API_URL=https://api.example.com/v1/tokenize
      TOKEN_COUNTER_API_KEY=sk-xxx
      TOKEN_COUNTER_MODEL=qwen3.7-max

    Default request/response format (OpenAI-compatible):
      POST {url}
      Authorization: Bearer {key}
      Body: {"model": "...", "input": "..."}
      Response: {"tokens": 123}

    On first API failure, sets _degraded=True and permanently falls back to
    local counter for the process lifetime. This avoids repeated HTTP failures
    on every count() / count_messages() call.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str = "cl100k_base",
        fallback: TokenCounter | None = None,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self._fallback = fallback
        self._degraded = False

    async def count(self, text: str) -> int:
        if self._degraded:
            return await self._fallback_count(text)
        try:
            return await self._call_api(text)
        except Exception as exc:
            _logger.warning("Provider tokenize failed (%s), permanently degrading to fallback", exc)
            self._degraded = True
            return await self._fallback_count(text)

    async def _fallback_count(self, text: str) -> int:
        if self._fallback:
            return await self._fallback.count(text)
        return max(1, int(len(text or "") * 0.25))

    async def _call_api(self, text: str) -> int:
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {"model": self.model, "input": text or ""}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(self.api_url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        return int(data["tokens"])


def create_token_counter(
    strategy: str | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> TokenCounter:
    """Create token counter with automatic fallback.

    Reads env vars if params not provided:
      TOKEN_COUNTER_STRATEGY (auto|provider|tiktoken|heuristic)
      TOKEN_COUNTER_API_URL
      TOKEN_COUNTER_API_KEY
      TOKEN_COUNTER_MODEL (default: cl100k_base)
    """
    _strategy = strategy or os.environ.get("TOKEN_COUNTER_STRATEGY", "auto")
    _api_url = api_url or os.environ.get("TOKEN_COUNTER_API_URL", "")
    _api_key = api_key or os.environ.get("TOKEN_COUNTER_API_KEY", "")
    _model = model or os.environ.get("TOKEN_COUNTER_MODEL", "cl100k_base")

    if _strategy == "heuristic":
        _logger.info("Token counter: heuristic (char×0.25)")
        return HeuristicTokenCounter()

    if _strategy == "tiktoken":
        return _create_tiktoken(_model)

    if _strategy == "provider":
        if _api_url and _api_key:
            fallback = _create_tiktoken(_model)
            _logger.info("Token counter: provider (%s)", _api_url)
            return ProviderTokenCounter(_api_url, _api_key, _model, fallback=fallback)
        _logger.warning("provider strategy requested but API_URL/API_KEY missing, falling back")
        return _create_tiktoken(_model)

    # auto
    if _api_url and _api_key:
        fallback = _create_tiktoken(_model)
        _logger.info("Token counter: auto → provider (%s)", _api_url)
        return ProviderTokenCounter(_api_url, _api_key, _model, fallback=fallback)

    return _create_tiktoken(_model)


def _create_tiktoken(model: str) -> TokenCounter:
    try:
        tc = TiktokenTokenCounter(model)
        _logger.info("Token counter: tiktoken (%s)", model)
        return tc
    except Exception as exc:
        _logger.warning("tiktoken unavailable (%s), falling back to heuristic", exc)
        return HeuristicTokenCounter()
