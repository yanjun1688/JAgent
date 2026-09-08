"""Retry policy execution — exponential backoff with jitter.

Retries cover both *raised exceptions* (legacy behaviour) and, when the caller
supplies a ``semantic_retry_check``, *returned results* that are judged to be
retryable semantic failures (v3.4 F-5: infra-transient ``success:false`` /
5xx responses). Both failure kinds share one ``retry_policy`` budget.
"""

import asyncio
import random
from typing import Any, Callable

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
    async def execute_with_retry(
        fn,
        *args,
        policy: RetryPolicy,
        semantic_retry_check: Callable[[Any], str | None] | None = None,
        **kwargs,
    ):
        """Run ``fn`` under a shared retry budget (exceptions + optional semantics).

        Exceptions are retried per the policy (legacy behaviour). When
        ``semantic_retry_check`` is provided, a *returned* result for which the
        check yields a non-None reason is treated as a retryable semantic failure
        sharing the same budget: it is re-invoked until the budget is exhausted,
        at which point the **last result is returned** (the caller decides how to
        finalize it — e.g. an UNSUCCESSFUL tool completion — rather than raising).
        A ``None`` reason finalizes immediately (semantic success or a
        non-retryable semantic failure).
        """
        last_error: Exception | None = None
        retry_count = 0
        for attempt in range(1, policy.max_retries + 2):
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if not RetryRunner.should_retry(attempt, str(exc), policy):
                    raise
                retry_count += 1
            else:
                if semantic_retry_check is None:
                    return result, retry_count
                reason = semantic_retry_check(result)
                if reason is None or attempt > policy.max_retries:
                    return result, retry_count
                retry_count += 1
            backoff_ms = RetryRunner._backoff_ms(attempt, policy)
            await asyncio.sleep(backoff_ms / 1000.0)
        raise last_error  # type: ignore[misc]
