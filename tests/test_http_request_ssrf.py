"""SSRF regression tests for the http_request tool (findings 3/8/16/23)."""

from __future__ import annotations

import datetime
import ipaddress

import httpx
import pytest

from harness.models.events import EventType
from harness.models.tools import Guardrail, ToolDefinition, ToolScopeTarget
from harness.tools import egress as egress_module
from harness.tools import http_request as http_request_module
from harness.tools.egress import EgressBlockedError, is_blocked_ip, validate_egress_url
from harness.tools.guardrails import EgressGuardrail, GuardrailRunner, validate_registration_safety
from harness.tools.http_request import HTTP_REQUEST_DEF, HttpRequestTool, http_request_fn


def _http_tool(guardrail_types: list[str]) -> ToolDefinition:
    return ToolDefinition(
        name="http_request",
        description="test",
        side_effects=[],
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        guardrails=[Guardrail(guardrail_type=guardrail_type, config={}) for guardrail_type in guardrail_types],
        scope_targets=[ToolScopeTarget(kind="domain", input_field="url")],
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://017700000001/",
        "http://127.1/",
    ],
)
async def test_blocks_non_global_literal_addresses(url):
    with pytest.raises(EgressBlockedError):
        await validate_egress_url(url)


async def test_blocks_hostname_resolving_to_loopback(monkeypatch):
    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("127.0.0.1")}

    monkeypatch.setattr(egress_module, "_resolve_ips", fake_resolve)
    with pytest.raises(EgressBlockedError):
        await validate_egress_url("http://example.com/")


async def test_blocks_when_any_resolved_address_is_private(monkeypatch):
    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("10.0.0.1")}

    monkeypatch.setattr(egress_module, "_resolve_ips", fake_resolve)
    with pytest.raises(EgressBlockedError):
        await validate_egress_url("http://example.com/")


async def test_allows_public_ip(monkeypatch):
    async def fake_resolve(host: str, port: int):
        return {ipaddress.ip_address("93.184.216.34")}

    monkeypatch.setattr(egress_module, "_resolve_ips", fake_resolve)
    await validate_egress_url("http://example.com/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://127.0.0.1/", "ftp://example.com/"])
async def test_blocks_non_http_schemes(url):
    with pytest.raises(EgressBlockedError):
        await validate_egress_url(url)


async def test_blocks_embedded_credentials():
    with pytest.raises(EgressBlockedError):
        await validate_egress_url("http://user:pass@93.184.216.34/")


def test_is_blocked_ip_classification():
    assert is_blocked_ip(ipaddress.ip_address("127.0.0.1"))
    assert is_blocked_ip(ipaddress.ip_address("169.254.169.254"))
    assert is_blocked_ip(ipaddress.ip_address("224.0.0.1"))
    assert is_blocked_ip(ipaddress.ip_address("255.255.255.255"))
    assert is_blocked_ip(ipaddress.ip_address("::ffff:127.0.0.1"))
    assert not is_blocked_ip(ipaddress.ip_address("93.184.216.34"))
    assert not is_blocked_ip(ipaddress.ip_address("2001:4860:4860::8888"))


async def test_egress_guardrail_blocks_before_execution():
    result = await EgressGuardrail.check(_http_tool(["egress"]), {"url": "http://127.0.0.1/"}, {})
    assert not result.passed
    assert result.guardrail_id == "egress"
    assert "127.0.0.1" in result.reason


async def test_guardrail_runner_blocks_ssrf():
    runner = GuardrailRunner()
    results = await runner.run(_http_tool(["scope", "egress"]), {"url": "http://169.254.169.254/"})
    assert results[-1].guardrail_id == "egress"
    assert not results[-1].passed


def test_registration_requires_egress_for_domain_scope():
    violations = validate_registration_safety(_http_tool(["scope"]))
    assert any("egress" in violation for violation in violations)


def test_http_request_declares_egress_guardrail():
    def ids(guardrails):
        return {guardrail.guardrail_type for guardrail in guardrails}

    assert "egress" in ids(HTTP_REQUEST_DEF.guardrails or [])
    assert "egress" in ids(HttpRequestTool().to_definition().guardrails or [])


async def test_http_client_does_not_follow_redirects():
    await http_request_module.close_client()
    client = await http_request_module._get_client()
    try:
        assert client.follow_redirects is False
    finally:
        await http_request_module.close_client()


async def test_tool_executor_blocks_ssrf_before_tool_runs(store):
    from harness.tools.executor import ExecutionStatus, ToolExecutor

    called = False

    async def tool_fn(input):
        nonlocal called
        called = True
        return {"status_code": 200}

    executor = ToolExecutor(store)
    result = await executor.execute(
        "run-ssrf",
        "http_request",
        {"url": "http://127.0.0.1:8000/"},
        HTTP_REQUEST_DEF,
        tool_fn,
    )

    assert result.status == ExecutionStatus.GUARDRAIL_BLOCKED
    assert result.guardrail_id == "egress"
    assert called is False
    events = await store.get_events("run-ssrf")
    assert any(event.event_type == EventType.TOOL and event.phase == "blocked" for event in events)
    assert not any(event.event_type == EventType.TOOL and event.phase == "called" for event in events)


async def test_redirect_response_is_returned_without_following():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        response = httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        response._elapsed = datetime.timedelta(milliseconds=1)
        return response

    await http_request_module.close_client()
    http_request_module._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    try:
        result = await http_request_fn({"url": "https://example.com/redir"})
    finally:
        await http_request_module.close_client()

    assert result["status_code"] == 302
    assert result["headers"]["location"] == "http://127.0.0.1/admin"
    assert len(calls) == 1
