from __future__ import annotations

from typing import Any

import httpx

from harness.models.tools import (
    Guardrail,
    OperationContract,
    SideEffect,
    SuccessIndicator,
    ToolDefinition,
    ToolScopeTarget,
)
from harness.tools.base import BaseTool, operation

_client: httpx.AsyncClient | None = None
_lock: Any = None

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 65536


def _get_lock() -> Any:
    import asyncio

    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client

    async with _get_lock():
        if _client is not None and not _client.is_closed:
            return _client
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


HTTP_REQUEST_DEF = ToolDefinition(
    name="http_request",
    description="Send an HTTP request to a remote server. Supports GET, POST, PUT, DELETE, PATCH, HEAD.",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL"},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
                "default": "GET",
            },
            "headers": {
                "type": "object",
                "description": "Optional HTTP headers (key-value pairs)",
                "additionalProperties": {"type": "string"},
            },
            "body": {
                "type": "object",
                "description": "JSON body for POST/PUT/PATCH requests",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Request timeout in milliseconds",
                "default": 30000,
            },
            "max_response_bytes": {
                "type": "integer",
                "description": "Maximum response body size in bytes (0 = unlimited)",
                "default": 65536,
            },
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string"},
            "status_code": {"type": "integer"},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "elapsed_ms": {"type": "integer"},
        },
    },
    idempotency_key_fields=["url", "method", "headers", "body"],
    side_effects=[SideEffect.EXTERNAL],
    guardrails=[Guardrail(guardrail_type="scope", config={})],
    timeout_ms=60000,
    success_indicator=SuccessIndicator(field="status_code", op="lt", value=400),
    # ADR-010 D-02: 判别键与默认操作（取代原魔法 `_OPERATION_KEYS` / 名称特判）。
    operation_key="method",
    default_operation="GET",
    # ADR-010 D-04: scope 目标契约化（取代 ScopeGuardrail 名称特判）。
    scope_targets=[ToolScopeTarget(kind="domain", input_field="url")],
    # S02: per-operation contracts — GET/HEAD are read-only (probe allowed);
    # POST/PUT/PATCH/DELETE have an external side effect (no probe).
    # url is never referenceable (D-01); body/headers may consume upstream
    # output (e.g. reusing a previous step's extracted value).
    operations=[
        OperationContract(
            operation="GET",
            output_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "status_code": {"type": "integer"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                    "elapsed_ms": {"type": "integer"},
                },
            },
            side_effects=[],
            probe_allowed=True,
            ref_allowed_fields={"url": False, "body": True, "headers": True},
        ),
        OperationContract(
            operation="HEAD",
            output_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "status_code": {"type": "integer"},
                    "headers": {"type": "object"},
                    "elapsed_ms": {"type": "integer"},
                },
            },
            side_effects=[],
            probe_allowed=True,
            ref_allowed_fields={"url": False},
        ),
        OperationContract(
            operation="POST",
            output_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "status_code": {"type": "integer"},
                    "headers": {"type": "object"},
                    "body": {"type": "string"},
                    "elapsed_ms": {"type": "integer"},
                },
            },
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"url": False, "body": True, "headers": True},
        ),
        OperationContract(
            operation="PUT",
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"url": False, "body": True, "headers": True},
        ),
        OperationContract(
            operation="PATCH",
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"url": False, "body": True, "headers": True},
        ),
        OperationContract(
            operation="DELETE",
            side_effects=[SideEffect.EXTERNAL],
            probe_allowed=False,
            ref_allowed_fields={"url": False, "body": True, "headers": True},
        ),
    ],
)


async def http_request_fn(input: dict[str, Any]) -> dict[str, Any]:
    url = input["url"]
    method = input.get("method", "GET").upper()
    headers = input.get("headers")
    request_body = input.get("body")
    timeout_ms = input.get("timeout_ms", 30000)
    max_bytes = input.get("max_response_bytes", DEFAULT_MAX_BYTES)

    client = await _get_client()

    req_kwargs: dict[str, Any] = {}
    if headers:
        req_kwargs["headers"] = headers
    if request_body is not None and method in ("POST", "PUT", "PATCH"):
        req_kwargs["json"] = request_body

    per_req_timeout = httpx.Timeout(timeout_ms / 1000.0)

    response = await client.request(method, url, timeout=per_req_timeout, **req_kwargs)

    response_text = response.text
    elapsed_ms = int(response.elapsed.total_seconds() * 1000)

    if max_bytes > 0 and len(response_text) > max_bytes:
        original_len = len(response_text)
        response_text = response_text[:max_bytes] + f"\n... (truncated, {original_len} total bytes)"

    return {
        "url": url,
        "method": method,
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response_text,
        "elapsed_ms": elapsed_ms,
    }


class HttpRequestTool(BaseTool):
    """http_request 声明式实现（ADR-010 D-01/D-08）— 取代 HTTP_REQUEST_DEF + fn。

    共享 httpx client 生命周期收敛到 ``close()``（D-08），业务执行复用
    ``http_request_fn``。
    """

    name = "http_request"
    description = "Send an HTTP request to a remote server. Supports GET, POST, PUT, DELETE, PATCH, HEAD."
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Target URL"},
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
                "default": "GET",
            },
            "headers": {"type": "object", "description": "Optional HTTP headers"},
            "body": {"type": "object", "description": "JSON body for POST/PUT/PATCH"},
            "timeout_ms": {"type": "integer", "default": 30000},
            "max_response_bytes": {"type": "integer", "default": 65536},
        },
        "required": ["url"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string"},
            "status_code": {"type": "integer"},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "elapsed_ms": {"type": "integer"},
        },
    }
    operation_key = "method"
    default_operation = "GET"
    side_effects = [SideEffect.EXTERNAL]
    idempotency_key_fields = ["url", "method", "headers", "body"]
    guardrails = [Guardrail(guardrail_type="scope", config={})]
    timeout_ms = 60000
    success_indicator = SuccessIndicator(field="status_code", op="lt", value=400)
    scope_targets = [ToolScopeTarget(kind="domain", input_field="url")]

    async def _do_request(self, input: dict) -> dict:
        return await http_request_fn(input)

    @operation("GET", probe_allowed=True, ref_allowed_fields={"url": False, "body": True, "headers": True})
    async def get(self, input):
        return await self._do_request(input)

    @operation("HEAD", probe_allowed=True, ref_allowed_fields={"url": False})
    async def head(self, input):
        return await self._do_request(input)

    @operation(
        "POST",
        side_effects=[SideEffect.EXTERNAL],
        ref_allowed_fields={"url": False, "body": True, "headers": True},
    )
    async def post(self, input):
        return await self._do_request(input)

    @operation(
        "PUT",
        side_effects=[SideEffect.EXTERNAL],
        ref_allowed_fields={"url": False, "body": True, "headers": True},
    )
    async def put(self, input):
        return await self._do_request(input)

    @operation(
        "PATCH",
        side_effects=[SideEffect.EXTERNAL],
        ref_allowed_fields={"url": False, "body": True, "headers": True},
    )
    async def patch(self, input):
        return await self._do_request(input)

    @operation(
        "DELETE",
        side_effects=[SideEffect.EXTERNAL],
        ref_allowed_fields={"url": False, "body": True, "headers": True},
    )
    async def delete(self, input):
        return await self._do_request(input)

    async def close(self) -> None:
        await close_client()
