"""F-3 (v3.4): token_limit 配置外置 — 移除 serve.py 硬编码 3000 的配置地雷。

回归 run e05087b6：``serve.py`` 曾硬编码 ``token_limit=3000``（类默认 128000），
导致压缩阈值被压到 2100、紧急 2700，频繁误触发 episode/紧急压缩。
"""

from __future__ import annotations

from harness.core.context_manager import DEFAULT_CONTEXT_TOKEN_LIMIT, resolve_context_token_limit


class TestTokenLimitConfig:
    def test_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("HARNESS_CONTEXT_TOKEN_LIMIT", raising=False)
        assert resolve_context_token_limit(None) == DEFAULT_CONTEXT_TOKEN_LIMIT
        # The old hardcoded landmine was 3000; the safe default must be far larger.
        assert DEFAULT_CONTEXT_TOKEN_LIMIT > 50_000

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTEXT_TOKEN_LIMIT", "64000")
        assert resolve_context_token_limit(None) == 64_000

    def test_explicit_value_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTEXT_TOKEN_LIMIT", "64000")
        assert resolve_context_token_limit(200_000) == 200_000

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTEXT_TOKEN_LIMIT", "not-a-number")
        assert resolve_context_token_limit(None) == DEFAULT_CONTEXT_TOKEN_LIMIT

    def test_non_positive_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("HARNESS_CONTEXT_TOKEN_LIMIT", "0")
        assert resolve_context_token_limit(None) == DEFAULT_CONTEXT_TOKEN_LIMIT
