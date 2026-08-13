"""Tests for V3.0 Phase 1 TokenCounter module."""

from __future__ import annotations

import os
from unittest.mock import patch


from harness.core.token_counter import (
    HeuristicTokenCounter,
    ProviderTokenCounter,
    TiktokenTokenCounter,
    create_token_counter,
)


class TestTiktokenTokenCounter:
    async def test_count_empty_string(self):
        tc = TiktokenTokenCounter()
        assert await tc.count("") == 0

    async def test_count_english_text(self):
        tc = TiktokenTokenCounter()
        count = await tc.count("Hello world, this is a test.")
        assert count > 0
        assert count < 20

    async def test_count_chinese_text(self):
        tc = TiktokenTokenCounter()
        count = await tc.count("你好世界，这是一个测试。")
        assert count > 0

    async def test_count_mixed_text(self):
        tc = TiktokenTokenCounter()
        count = await tc.count("Hello 世界, this is 测试.")
        assert count > 0

    async def test_count_messages(self):
        tc = TiktokenTokenCounter()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        total = await tc.count_messages(messages)
        assert total > 0

    async def test_custom_encoding(self):
        tc = TiktokenTokenCounter(model="gpt2")
        count = await tc.count("Hello world")
        assert count > 0


class TestHeuristicTokenCounter:
    async def test_count_empty_string(self):
        tc = HeuristicTokenCounter()
        assert await tc.count("") == 1

    async def test_count_english_text(self):
        tc = HeuristicTokenCounter()
        count = await tc.count("Hello world")
        assert count == max(1, int(11 * 0.25))

    async def test_count_chinese_text(self):
        tc = HeuristicTokenCounter()
        text = "你好世界"
        count = await tc.count(text)
        assert count == max(1, int(len(text) * 0.25))

    async def test_count_messages(self):
        tc = HeuristicTokenCounter()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        total = await tc.count_messages(messages)
        assert total > 0


class TestProviderTokenCounter:
    async def test_successful_api_call(self):
        fallback = HeuristicTokenCounter()
        tc = ProviderTokenCounter(
            api_url="https://api.example.com/tokenize",
            api_key="test-key",
            model="test-model",
            fallback=fallback,
        )

        class FakeResp:
            def json(self):
                return {"tokens": 42}

            def raise_for_status(self):
                pass

        class FakeClient:
            async def post(self, url, headers=None, json=None):
                return FakeResp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            count = await tc.count("test text")
        assert count == 42

    async def test_api_failure_falls_back(self):
        fallback = HeuristicTokenCounter()
        tc = ProviderTokenCounter(
            api_url="https://api.example.com/tokenize",
            api_key="test-key",
            model="test-model",
            fallback=fallback,
        )

        with patch("httpx.AsyncClient", side_effect=Exception("Connection failed")):
            count = await tc.count("Hello world")
        assert count == max(1, int(len("Hello world") * 0.25))

    async def test_api_failure_no_fallback(self):
        tc = ProviderTokenCounter(
            api_url="https://api.example.com/tokenize",
            api_key="test-key",
            model="test-model",
            fallback=None,
        )

        with patch("httpx.AsyncClient", side_effect=Exception("Connection failed")):
            count = await tc.count("Hello world")
        assert count == max(1, int(len("Hello world") * 0.25))


class TestCreateTokenCounter:
    def test_default_strategy_is_auto(self):
        with patch.dict(os.environ, {}, clear=True):
            tc = create_token_counter()
        assert isinstance(tc, TiktokenTokenCounter)

    def test_explicit_tiktoken_strategy(self):
        tc = create_token_counter(strategy="tiktoken")
        assert isinstance(tc, TiktokenTokenCounter)

    def test_explicit_heuristic_strategy(self):
        tc = create_token_counter(strategy="heuristic")
        assert isinstance(tc, HeuristicTokenCounter)

    def test_provider_strategy_with_config(self):
        tc = create_token_counter(
            strategy="provider",
            api_url="https://api.example.com/tokenize",
            api_key="test-key",
            model="test-model",
        )
        assert isinstance(tc, ProviderTokenCounter)

    def test_provider_strategy_without_config_falls_back(self):
        with patch.dict(os.environ, {}, clear=True):
            tc = create_token_counter(strategy="provider")
        assert isinstance(tc, TiktokenTokenCounter)

    def test_auto_with_api_config_uses_provider(self):
        tc = create_token_counter(
            strategy="auto",
            api_url="https://api.example.com/tokenize",
            api_key="test-key",
        )
        assert isinstance(tc, ProviderTokenCounter)

    def test_auto_without_api_config_uses_tiktoken(self):
        with patch.dict(os.environ, {}, clear=True):
            tc = create_token_counter(strategy="auto")
        assert isinstance(tc, TiktokenTokenCounter)

    def test_env_var_strategy_heuristic(self):
        with patch.dict(os.environ, {"TOKEN_COUNTER_STRATEGY": "heuristic"}):
            tc = create_token_counter()
        assert isinstance(tc, HeuristicTokenCounter)

    def test_env_var_strategy_tiktoken(self):
        with patch.dict(os.environ, {"TOKEN_COUNTER_STRATEGY": "tiktoken"}):
            tc = create_token_counter()
        assert isinstance(tc, TiktokenTokenCounter)

    def test_env_var_api_config(self):
        env = {
            "TOKEN_COUNTER_API_URL": "https://api.example.com/tokenize",
            "TOKEN_COUNTER_API_KEY": "test-key",
            "TOKEN_COUNTER_MODEL": "test-model",
        }
        with patch.dict(os.environ, env):
            tc = create_token_counter()
        assert isinstance(tc, ProviderTokenCounter)

    def test_explicit_params_override_env(self):
        env = {
            "TOKEN_COUNTER_STRATEGY": "provider",
            "TOKEN_COUNTER_API_URL": "https://env.example.com",
            "TOKEN_COUNTER_API_KEY": "env-key",
        }
        with patch.dict(os.environ, env):
            tc = create_token_counter(strategy="heuristic")
        assert isinstance(tc, HeuristicTokenCounter)


class TestTokenCounterAccuracy:
    async def test_tiktoken_vs_heuristic_english(self):
        """Tiktoken should give reasonable counts for English text."""
        tc_tiktoken = TiktokenTokenCounter()
        tc_heuristic = HeuristicTokenCounter()
        text = "The quick brown fox jumps over the lazy dog." * 5
        tiktoken_count = await tc_tiktoken.count(text)
        heuristic_count = await tc_heuristic.count(text)
        ratio = tiktoken_count / heuristic_count if heuristic_count > 0 else 0
        assert 0.3 < ratio < 3.0

    async def test_tiktoken_vs_heuristic_chinese(self):
        """Tiktoken should give reasonable counts for Chinese text."""
        tc_tiktoken = TiktokenTokenCounter()
        tc_heuristic = HeuristicTokenCounter()
        text = "这是一个中文测试文本，用于验证token计数器的准确性。" * 3
        tiktoken_count = await tc_tiktoken.count(text)
        heuristic_count = await tc_heuristic.count(text)
        assert tiktoken_count > 0
        assert heuristic_count > 0
