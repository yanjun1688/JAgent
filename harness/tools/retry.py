"""Retry policy execution — exponential backoff with jitter."""

import asyncio
import random

from harness.models.tools import RetryPolicy


class RetryRunner:
    @staticmethod
    def should_retry(attempt: int, error: str, policy: RetryPolicy) -> bool:
        if attempt > policy.max_retries:
            return False
        if policy.retryable_errors and not any(candidate in error for candidate in policy.retryable_errors):
            return False
        return True

    @staticmethod
    def _backoff_ms(attempt: int, policy: RetryPolicy) -> int:
        base = policy.backoff_base_ms * (2 ** (attempt - 1))
        jitter_ratio = 0.25
        jitter = int(base * jitter_ratio * (random.random() * 2 - 1))
        return max(0, base + jitter)

    @staticmethod
    async def execute_with_retry(fn, *args, policy: RetryPolicy, **kwargs):
        last_error: Exception | None = None
        for attempt in range(1, policy.max_retries + 2):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                error_str = str(exc)
                if not RetryRunner.should_retry(attempt, error_str, policy):
                    raise
            backoff_ms = RetryRunner._backoff_ms(attempt, policy)
            await asyncio.sleep(backoff_ms / 1000.0)
        raise last_error  # type: ignore[misc]
