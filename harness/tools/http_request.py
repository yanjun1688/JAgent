from __future__ import annotations

from typing import Any

import httpx

from harness.models.tools import SideEffect, ToolDefinition

CLIENT = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

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
                "default": 1_048_576,
            },
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status_code": {"type": "integer"},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "elapsed_ms": {"type": "integer"},
        },
    },
    idempotency_key_fields=["url", "method", "body"],
    side_effects=[SideEffect.EXTERNAL],
    timeout_ms=60000,
)


async def http_request_fn(input: dict[str, Any]) -> dict[str, Any]:
    url = input["url"]
    method = input.get("method", "GET").upper()
    headers = input.get("headers")
    body = input.get("body")
    timeout_ms = input.get("timeout_ms", 30000)
    max_bytes = input.get("max_response_bytes", 1_048_576)

    req_kwargs: dict[str, Any] = {}
    if headers:
        req_kwargs["headers"] = headers
    if body is not None and method in ("POST", "PUT", "PATCH"):
        req_kwargs["json"] = body

    async with httpx.AsyncClient(timeout=timeout_ms / 1000.0, follow_redirects=True) as client:
        response = await client.request(method, url, **req_kwargs)

    body_text = response.text
    if max_bytes > 0 and len(body_text) > max_bytes:
        body_text = body_text[:max_bytes] + f"\n... (truncated, {len(body_text)} total bytes)"

    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": body_text,
        "elapsed_ms": int(response.elapsed.total_seconds() * 1000),
    }
