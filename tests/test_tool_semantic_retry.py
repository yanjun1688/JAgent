"""F-5 (v3.4): Tool Layer 语义失败自动重试 — RetryRunner + ToolExecutor 集成。

DESIGN §1 根因①：``RetryRunner`` 只重试异常/超时，不重试语义失败
（``success:false`` / ``status_code>=400`` 的基础设施瞬态），失败直接升级到全局 revise。

DESIGN §10.3（保守界定）：仅当 ① 语义失败文案被 `classify_failure_tier` 判为
``tool_retry``（瞬态/基础设施 5xx）**且** ② 该工具+操作在受信只读白名单内
（自动重跑不会重复副作用）时，才在 Tool Layer 内按 ``retry_policy`` 预算自动重试
同输入；其余 UNSUCCESSFUL 维持现状直接返回（升级到 F-6 局部修复）。业务性不成功
（404/not found 等）绝不盲目重试。

受信边界：本自动重试完全内聚于 Tool Layer（受信组件），不引入 LLM，不写新事件，
与异常重试行为静默对齐（重试计数由 ``retry_attempts`` / trace 承载）。
"""

from __future__ import annotations

from typing import Any

import pytest

from harness.core.recovery import classify_failure_tier, is_read_only_action
from harness.models.events import EventType, ToolCompletedPayload, ToolResultType
from harness.models.tools import RetryPolicy, SuccessIndicator, ToolDefinition
from harness.tools.executor import ExecutionStatus, ToolExecutor
from harness.tools.retry import RetryRunner
from harness.storage.event_store import EventStore


def _http_get_def(*, max_retries: int = 2, retryable_errors: list[str] | None = None) -> ToolDefinition:
    """A GET-only read-only tool whose output carries a status_code success gate.

    Mirrors the real ``http_request`` shape (``status_code < 400`` indicator) but
    with a fast, bounded retry policy so tests never sleep.
    """
    return ToolDefinition(
        name="http_request",
        description="GET-only http tool (test)",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
            },
        },
        side_effects=[],
        timeout_ms=5000,
        retry_policy=RetryPolicy(
            max_retries=max_retries,
            backoff_base_ms=0,
            retryable_errors=list(retryable_errors or []),
        ),
        success_indicator=SuccessIndicator(field="status_code", op="lt", value=400),
    )


# ── 纯函数：瞬态文案分类（F-5）──


class TestSemanticTransientClassification:
    def test_5xx_fallback_message_is_tool_retry(self):
        # 真实 http 工具 5xx 输出无 error 键 → 语义错误文案是 fallback
        # "status_code=503 (op=lt, value=400)"，这属于基础设施瞬态，可工具级重试。
        assert classify_failure_tier("status_code=503 (op=lt, value=400)", retryable=False) == "tool_retry"

    def test_4xx_business_message_escalates(self):
        # 404/not found 是业务性不成功，不应工具级自动重试。
        assert classify_failure_tier("status_code=404 (op=lt, value=400)", retryable=False) == "step_repair"

    def test_429_without_keyword_escalates(self):
        assert classify_failure_tier("status_code=429 (op=lt, value=400)", retryable=False) == "step_repair"

    def test_transient_text_still_tool_retry(self):
        assert classify_failure_tier("temporary upstream 503", retryable=False) == "tool_retry"

    def test_browser_env_error_still_step_repair(self):
        assert classify_failure_tier("Browser unavailable on this event loop", retryable=False) == "step_repair"


# ── 纯函数：只读白名单（自动重跑安全门）──


class TestReadOnlyActionPublic:
    def test_http_get_read_only(self):
        assert is_read_only_action("http_request", {"url": "https://x", "method": "GET"}) is True

    def test_http_default_get_read_only(self):
        assert is_read_only_action("http_request", {"url": "https://x"}) is True

    def test_http_post_is_mutating(self):
        assert is_read_only_action("http_request", {"url": "https://x", "method": "POST"}) is False

    def test_file_read_read_only(self):
        assert is_read_only_action("file_op", {"operation": "read", "path": "/f"}) is True

    def test_file_write_is_mutating(self):
        assert is_read_only_action("file_op", {"operation": "write", "path": "/f", "content": "x"}) is False

    def test_fetch_output_whole_tool_read_only(self):
        assert is_read_only_action("fetch_output", {"ref": "abc"}) is True

    def test_unknown_tool_fails_closed(self):
        assert is_read_only_action("mcp_call", {}) is False


# ── RetryRunner：语义重试共享预算 ──


class TestRetryRunnerSemanticBudget:
    def _always_transient(self):
        async def fn(_input: dict[str, Any]) -> dict[str, Any]:
            return {"status_code": 503, "error": "temporary upstream 503"}

        return fn

    def _check(self):
        # A predicate mirroring the executor's gate: transient only.
        def check(out: dict[str, Any]) -> str | None:
            err = str(out.get("error") or "")
            return err if classify_failure_tier(err, retryable=False) == "tool_retry" else None

        return check

    @pytest.mark.asyncio
    async def test_semantic_failure_retried_within_budget_then_exhausts(self):
        policy = RetryPolicy(max_retries=2, backoff_base_ms=0)
        result, retry_count = await RetryRunner.execute_with_retry(
            self._always_transient(), {}, policy=policy, semantic_retry_check=self._check()
        )
        assert result["status_code"] == 503
        # max_retries=2 → 3 次真实调用，2 次语义重试（共享预算，与异常重试一致）。
        assert retry_count == 2

    @pytest.mark.asyncio
    async def test_semantic_success_stops_immediately(self):
        call_count = 0

        async def flaky(_input: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status_code": 503, "error": "temporary upstream 503"}
            return {"status_code": 200}

        policy = RetryPolicy(max_retries=3, backoff_base_ms=0)
        result, retry_count = await RetryRunner.execute_with_retry(
            flaky, {}, policy=policy, semantic_retry_check=self._check()
        )
        assert result["status_code"] == 200
        assert call_count == 2
        assert retry_count == 1

    @pytest.mark.asyncio
    async def test_semantic_check_none_keeps_exception_only_behaviour(self):
        policy = RetryPolicy(max_retries=3, retryable_errors=["timeout"], backoff_base_ms=0)
        result, retry_count = await RetryRunner.execute_with_retry(self._always_transient(), {}, policy=policy)
        # No semantic_retry_check → a returned result finalizes immediately (legacy).
        assert result["status_code"] == 503
        assert retry_count == 0

    @pytest.mark.asyncio
    async def test_exception_retries_still_share_budget(self):
        call_count = 0

        async def boom_then_ok(_input: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("timeout")
            return {"status_code": 200}

        policy = RetryPolicy(max_retries=2, retryable_errors=["timeout"], backoff_base_ms=0)
        result, retry_count = await RetryRunner.execute_with_retry(
            boom_then_ok, {}, policy=policy, semantic_retry_check=self._check()
        )
        assert result["status_code"] == 200
        assert call_count == 2
        assert retry_count == 1


# ── ToolExecutor 集成：语义失败先工具级重试，用尽才升级 ──


class TestExecutorSemanticRetry:
    @pytest.fixture
    def store(self):
        return EventStore(db_path=":memory:")

    @pytest.fixture
    async def init_store(self, store):
        await store.initialize()
        return store

    async def _run(self, store, input_data: dict[str, Any], tool_def: ToolDefinition, fn):
        executor = ToolExecutor(store=store)
        return await executor.execute(
            run_id="run-f5",
            tool_name=tool_def.name,
            input=input_data,
            tool_def=tool_def,
            tool_fn=fn,
        )

    async def _completed_events(self, store):
        events = await store.get_events("run-f5")
        return [e for e in events if e.event_type == EventType.TOOL_COMPLETED]

    @pytest.mark.asyncio
    async def test_transient_semantic_failure_retried_then_succeeds(self, init_store):
        store = init_store
        call_count = 0

        async def flaky(input_data: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status_code": 503, "error": "temporary upstream 503"}
            return {"status_code": 200}

        result = await self._run(store, {"url": "https://api.example.com/data"}, _http_get_def(), flaky)

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is False
        assert result.output["status_code"] == 200
        assert result.retry_attempts == 1
        assert call_count == 2, "基础设施瞬态语义失败应先在工具级重试"

        completed = await self._completed_events(store)
        assert len(completed) == 1, "重试不产生额外事件"
        tp = ToolCompletedPayload.model_validate(completed[0].payload)
        assert tp.result_type == ToolResultType.SUCCESS

    @pytest.mark.asyncio
    async def test_transient_semantic_failure_exhausted_escalates_once(self, init_store):
        store = init_store
        call_count = 0

        async def always_bad(input_data: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"status_code": 503, "error": "connection reset by peer"}

        result = await self._run(store, {"url": "https://api.example.com/data"}, _http_get_def(max_retries=2), always_bad)

        assert result.status == ExecutionStatus.COMPLETED
        assert result.has_semantic_error is True
        assert result.retry_attempts == 2
        assert call_count == 3, "max_retries=2 → 预算耗尽共 3 次调用，之后才升级"

        completed = await self._completed_events(store)
        assert len(completed) == 1
        tp = ToolCompletedPayload.model_validate(completed[0].payload)
        assert tp.result_type == ToolResultType.UNSUCCESSFUL

    @pytest.mark.asyncio
    async def test_business_semantic_failure_not_retried(self, init_store):
        store = init_store
        call_count = 0

        async def not_found(input_data: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"status_code": 404, "error": "resource not found"}

        result = await self._run(store, {"url": "https://api.example.com/data"}, _http_get_def(), not_found)

        assert result.has_semantic_error is True
        assert call_count == 1, "业务性不成功（404）绝不自动重试"
        assert result.retry_attempts == 0

    @pytest.mark.asyncio
    async def test_transient_but_mutating_op_not_retried(self, init_store):
        store = init_store
        call_count = 0

        async def post_flaky(input_data: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"status_code": 503, "error": "temporary upstream 503"}

        result = await self._run(
            store,
            {"url": "https://api.example.com/data", "method": "POST"},
            _http_get_def(),
            post_flaky,
        )

        assert result.has_semantic_error is True
        assert call_count == 1, "副作用操作即便文案瞬态也不得自动重试（防重复副作用）"

    @pytest.mark.asyncio
    async def test_retryable_errors_restricts_semantic_retry(self, init_store):
        store = init_store
        call_count = 0

        async def other_transient(input_data: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"status_code": 503, "error": "temporary upstream 503"}

        # retryable_errors=["rate limit"]：语义文案不含该子串 → 不自动重试。
        result = await self._run(
            store,
            {"url": "https://api.example.com/data"},
            _http_get_def(max_retries=2, retryable_errors=["rate limit"]),
            other_transient,
        )
        assert result.has_semantic_error is True
        assert call_count == 1
        assert result.retry_attempts == 0

    @pytest.mark.asyncio
    async def test_exception_retry_behavior_unchanged(self, init_store):
        store = init_store
        call_count = 0

        async def boom(input_data: dict[str, Any]) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("connection refused")

        # retryable_errors=["refused"] → executor marks the final failure retryable
        # AND RetryRunner retries each attempt; budget shared, unchanged behaviour.
        result = await self._run(
            store,
            {"url": "https://api.example.com/data"},
            _http_get_def(max_retries=2, retryable_errors=["refused"]),
            boom,
        )

        assert result.status == ExecutionStatus.FAILED
        assert result.retryable is True
        assert call_count == 3, "异常重试行为不变（共享同一预算）"
