"""V0.2 Tool Layer tests — ToolRegistry + http_request + file_op + browser + mcp_call + SKILL."""

from __future__ import annotations

import tempfile
from typing import Any

import pytest

from harness.models.tools import SideEffect, ToolDefinition
from harness.tools import (
    FILE_OP_DEF,
    HTTP_REQUEST_DEF,
    ToolRegistry,
    file_op_fn as _file_op_fn,
    http_request_fn,
    set_sandbox_root,
)
from harness.tools.skill import Skill
from harness.tools.executor import ToolExecutor, current_run_id
from harness.storage.event_store import EventStore
from harness.models.events import EventType
from harness.execution.local import LocalDirectoryBackend

_TEST_BACKEND = None

async def file_op_fn(input: dict[str, Any]):
    """Route direct tests through an explicit sandbox backend."""
    return await _file_op_fn(input, backend=_TEST_BACKEND)

# ── ToolRegistry ──────────────────────────────────────────────────


class TestToolRegistry:
    def test_register_and_retrieve(self):
        registry = ToolRegistry()
        td = ToolDefinition(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
            output_schema={"type": "object", "properties": {"y": {"type": "integer"}}},
            idempotency_key_fields=["x"],
            side_effects=[SideEffect.WRITE],
            timeout_ms=5000,
        )
        fn = lambda input: {"y": input["x"] * 2}  # noqa: E731
        registry._register(td, fn)
        assert registry.get_tool_def("test_tool") is td
        assert registry.get_tool_fn("test_tool") is fn
        assert "test_tool" in registry
        assert len(registry) == 1

    def test_register_duplicate_raises(self):
        registry = ToolRegistry()
        td = ToolDefinition(name="dup", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE])
        registry._register(td, lambda i: {})
        td2 = ToolDefinition(name="dup", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE])
        with pytest.raises(ValueError, match="already registered"):
            registry._register(td2, lambda i: {})

    def test_list_tool_defs(self):
        registry = ToolRegistry()
        td1 = ToolDefinition(name="a", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE])
        td2 = ToolDefinition(name="b", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE])
        registry._register(td1, lambda i: {})
        registry._register(td2, lambda i: {})
        defs = registry.list_tool_defs()
        assert len(defs) == 2
        names = {d.name for d in defs}
        assert names == {"a", "b"}

    def test_list_tool_fns(self):
        registry = ToolRegistry()
        fn_a = lambda i: {"x": 1}  # noqa: E731
        fn_b = lambda i: {"y": 2}  # noqa: E731
        registry._register(
            ToolDefinition(name="a", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            fn_a,
        )
        registry._register(
            ToolDefinition(name="b", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            fn_b,
        )
        fns = registry.list_tool_fns()
        assert fns["a"] is fn_a
        assert fns["b"] is fn_b

    def test_remove_tool(self):
        registry = ToolRegistry()
        td = ToolDefinition(name="temp", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE])
        registry._register(td, lambda i: {})
        assert "temp" in registry
        registry.remove("temp")
        assert "temp" not in registry
        assert len(registry) == 0

    def test_tool_names_property(self):
        registry = ToolRegistry()
        registry._register(
            ToolDefinition(name="x", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            lambda i: {},
        )
        registry._register(
            ToolDefinition(name="y", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            lambda i: {},
        )
        assert sorted(registry.tool_names) == ["x", "y"]

    def test_build_llm_schemas(self):
        registry = ToolRegistry()
        td = ToolDefinition(
            name="greet",
            description="Greet someone",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            output_schema={"type": "object", "properties": {"greeting": {"type": "string"}}},
            idempotency_key_fields=["name"],
            side_effects=[],
        )
        registry._register(td, lambda i: {"greeting": f"Hello, {i['name']}!"})
        schemas = registry.build_llm_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "greet"
        assert schemas[0]["type"] == "function"


# ── http_request ──────────────────────────────────────────────────


class TestHttpRequestDefinition:
    def test_definition_fields(self):
        assert HTTP_REQUEST_DEF.name == "http_request"
        assert HTTP_REQUEST_DEF.idempotency_key_fields == ["url", "method", "headers", "body"]
        assert SideEffect.EXTERNAL in HTTP_REQUEST_DEF.side_effects
        assert HTTP_REQUEST_DEF.timeout_ms == 60000

    def test_schema_requires_url(self):
        props = HTTP_REQUEST_DEF.input_schema["properties"]
        assert "url" in props
        assert "method" in props
        assert HTTP_REQUEST_DEF.input_schema["required"] == ["url"]


# ── http_request client lifecycle & behaviour ──────────────────────


class TestHttpRequestClientLifecycle:
    """Shared client should be reused across calls and properly cleaned up."""

    async def test_client_reuse(self):
        from harness.tools.http_request import _get_client, close_client

        try:
            c1 = await _get_client()
            c2 = await _get_client()
            assert c1 is c2, "should return the same client instance"
        finally:
            await close_client()

    async def test_close_client_nullifies(self):
        from harness.tools.http_request import _get_client, close_client
        from harness.tools.http_request import _client

        await _get_client()
        await close_client()
        assert _client is None

    async def test_get_client_after_close_creates_new(self):
        from harness.tools.http_request import _get_client, close_client

        try:
            c1 = await _get_client()
            await close_client()
            c2 = await _get_client()
            assert c1 is not c2, "should create a new client after close"
        finally:
            await close_client()

    async def test_concurrent_init_produces_same_client(self):
        import asyncio
        from harness.tools.http_request import _get_client, close_client

        try:
            c1, c2 = await asyncio.gather(_get_client(), _get_client())
            assert c1 is c2, "concurrent init should yield the same client"
        finally:
            await close_client()


class TestHttpRequestFn:
    """Test http_request_fn behaviour: truncation, timeout, error paths."""

    @pytest.mark.asyncio
    async def test_truncation_preserves_original_byte_count(self):
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        long_text = "x" * 2000

        mock_resp = AsyncMock()
        mock_resp.text = long_text
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.elapsed = dt.timedelta(milliseconds=42)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            result = await http_request_fn(
                {
                    "url": "https://test.local/data",
                    "method": "GET",
                    "max_response_bytes": 100,
                }
            )

        assert result["status_code"] == 200
        assert "truncated" in result["body"]
        assert "2000 total bytes" in result["body"], f"should show original len, got: {result['body']}"
        assert result["elapsed_ms"] == 42

    @pytest.mark.asyncio
    async def test_no_truncation_when_under_limit(self):
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        text = "short response"

        mock_resp = AsyncMock()
        mock_resp.text = text
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.elapsed = dt.timedelta(milliseconds=5)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            result = await http_request_fn(
                {
                    "url": "https://test.local/data",
                    "max_response_bytes": 1000,
                }
            )

        assert result["body"] == text
        assert "truncated" not in result["body"]

    @pytest.mark.asyncio
    async def test_unlimited_response(self):
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        text = "a" * 100000

        mock_resp = AsyncMock()
        mock_resp.text = text
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.elapsed = dt.timedelta(milliseconds=10)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            result = await http_request_fn(
                {
                    "url": "https://test.local/big",
                    "max_response_bytes": 0,
                }
            )

        assert result["body"] == text

    @pytest.mark.asyncio
    async def test_post_sends_json_body(self):
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        mock_resp = AsyncMock()
        mock_resp.text = '{"ok":true}'
        mock_resp.status_code = 201
        mock_resp.headers = {}
        mock_resp.elapsed = dt.timedelta(milliseconds=30)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            result = await http_request_fn(
                {
                    "url": "https://test.local/api",
                    "method": "POST",
                    "body": {"key": "value"},
                }
            )

        call_kwargs = mock_req.call_args.kwargs
        assert call_kwargs["json"] == {"key": "value"}
        assert result["status_code"] == 201
        assert result["method"] == "POST"

    @pytest.mark.asyncio
    async def test_default_method_is_get(self):
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        mock_resp = AsyncMock()
        mock_resp.text = "ok"
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.elapsed = dt.timedelta(milliseconds=1)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            result = await http_request_fn({"url": "https://test.local/"})

        assert mock_req.call_args.args[0] == "GET"
        assert result["method"] == "GET"

    @pytest.mark.asyncio
    async def test_result_includes_url_method_headers(self):
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        resp_headers = httpx.Headers({"content-type": "application/json", "x-req-id": "abc123"})
        mock_resp = AsyncMock()
        mock_resp.text = "{}"
        mock_resp.status_code = 200
        mock_resp.headers = resp_headers
        mock_resp.elapsed = dt.timedelta(milliseconds=15)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            result = await http_request_fn(
                {
                    "url": "https://test.local/v1",
                    "method": "DELETE",
                }
            )

        assert result["url"] == "https://test.local/v1"
        assert result["method"] == "DELETE"
        assert result["headers"]["content-type"] == "application/json"
        assert result["headers"]["x-req-id"] == "abc123"


class TestHttpRequestConcurrency:
    """Concurrent http_request calls should work correctly with shared client."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_complete(self):
        import asyncio
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        async def delayed_response(request, *args, **kwargs):
            await asyncio.sleep(0.01)
            mock = AsyncMock()
            mock.text = "ok"
            mock.status_code = 200
            mock.headers = {}
            mock.elapsed = dt.timedelta(milliseconds=10)
            return mock

        with patch.object(httpx.AsyncClient, "request", side_effect=delayed_response) as mock_req:
            tasks = [http_request_fn({"url": f"https://test.local/{i}", "method": "GET"}) for i in range(20)]
            results = await asyncio.gather(*tasks)

        assert len(results) == 20
        assert all(r["status_code"] == 200 for r in results)
        assert mock_req.call_count == 20

    @pytest.mark.asyncio
    async def test_concurrent_requests_use_shared_client(self):
        import asyncio
        import datetime as dt
        from unittest.mock import AsyncMock, patch
        import httpx
        from harness.tools.http_request import http_request_fn

        mock_resp = AsyncMock()
        mock_resp.text = "ok"
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.elapsed = dt.timedelta(milliseconds=5)

        with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_resp
            tasks = [http_request_fn({"url": f"https://test.local/page/{i}"}) for i in range(5)]
            await asyncio.gather(*tasks)

        assert mock_req.call_count == 5


# ── file_op ───────────────────────────────────────────────────────


class TestFileOpDefinition:
    def test_definition_fields(self):
        assert FILE_OP_DEF.name == "file_op"
        assert FILE_OP_DEF.idempotency_key_fields == ["operation", "path", "content"]
        assert SideEffect.WRITE in FILE_OP_DEF.side_effects
        assert SideEffect.DELETE in FILE_OP_DEF.side_effects

    def test_schema_requires_operation_path(self):
        assert FILE_OP_DEF.input_schema["required"] == ["operation", "path"]

    def test_schema_enumerates_operations(self):
        enum = FILE_OP_DEF.input_schema["properties"]["operation"]["enum"]
        assert "read" in enum
        assert "write" in enum
        assert "append" in enum
        assert "delete" in enum
        assert "list" in enum


class TestFileOpIntegration:
    @pytest.fixture(autouse=True)
    def _sandbox(self):
        global _TEST_BACKEND
        with tempfile.TemporaryDirectory() as tmp:
            set_sandbox_root(tmp)
            _TEST_BACKEND = LocalDirectoryBackend(tmp)
            yield tmp
        _TEST_BACKEND = None

    async def test_write_and_read(self):
        result = await file_op_fn({"operation": "write", "path": "hello.txt", "content": "world"})
        assert result["success"] is True

        result = await file_op_fn({"operation": "read", "path": "hello.txt"})
        assert result["success"] is True
        assert result["content"] == "world"

    async def test_direct_file_op_requires_backend(self):
        from harness.tools.file_op import file_op_fn as trusted_file_op_fn

        with pytest.raises(RuntimeError, match="Execution backend is required"):
            await trusted_file_op_fn({"operation": "write", "path": "unsafe.txt", "content": "x"})

    async def test_append(self):
        await file_op_fn({"operation": "write", "path": "log.txt", "content": "line1\n"})
        await file_op_fn({"operation": "append", "path": "log.txt", "content": "line2\n"})
        result = await file_op_fn({"operation": "read", "path": "log.txt"})
        assert result["content"] == "line1\nline2\n"

    async def test_delete(self):
        await file_op_fn({"operation": "write", "path": "todelete.txt", "content": "bye"})
        result = await file_op_fn({"operation": "delete", "path": "todelete.txt"})
        assert result["success"] is True
        result = await file_op_fn({"operation": "read", "path": "todelete.txt"})
        assert result["success"] is False

    async def test_list_directory(self):
        await file_op_fn({"operation": "write", "path": "a.txt", "content": ""})
        await file_op_fn({"operation": "write", "path": "b.txt", "content": ""})
        result = await file_op_fn({"operation": "list", "path": "."})
        assert result["success"] is True
        assert "a.txt" in result["content"]
        assert "b.txt" in result["content"]

    async def test_outside_sandbox_blocked(self):
        with pytest.raises(PermissionError):
            await file_op_fn({"operation": "read", "path": "../outside.txt"})

    async def test_list_non_existent(self):
        result = await file_op_fn({"operation": "list", "path": "nonexistent"})
        assert result["success"] is False

    async def test_delete_non_existent(self):
        result = await file_op_fn({"operation": "delete", "path": "ghost.txt"})
        assert result["success"] is False


class TestToolResultEnrichmentBug6:
    """Bug 6: Tool return values should include path/url/action for answer context."""

    @pytest.fixture(autouse=True)
    def _sandbox(self):
        global _TEST_BACKEND
        with tempfile.TemporaryDirectory() as tmp:
            set_sandbox_root(tmp)
            _TEST_BACKEND = LocalDirectoryBackend(tmp)
            yield tmp
        _TEST_BACKEND = None

    async def test_file_op_write_includes_path(self):
        result = await file_op_fn({"operation": "write", "path": "output.txt", "content": "hello"})
        assert result["success"] is True
        assert result["path"] == "output.txt", f"write should include path, got: {result}"

    async def test_file_op_read_includes_path(self):
        await file_op_fn({"operation": "write", "path": "data.txt", "content": "test"})
        result = await file_op_fn({"operation": "read", "path": "data.txt"})
        assert result["success"] is True
        assert result["path"] == "data.txt", f"read should include path, got: {result}"
        assert result["content"] == "test"

    async def test_file_op_delete_includes_path(self):
        await file_op_fn({"operation": "write", "path": "tmp.txt", "content": ""})
        result = await file_op_fn({"operation": "delete", "path": "tmp.txt"})
        assert result["success"] is True
        assert result["path"] == "tmp.txt", f"delete should include path, got: {result}"

    async def test_file_op_error_includes_path(self):
        result = await file_op_fn({"operation": "read", "path": "missing.txt"})
        assert result["success"] is False
        assert result["path"] == "missing.txt", f"error response should include path, got: {result}"

    async def test_http_request_schema_includes_url_and_method(self):
        """HTTP output_schema should include url and method for answer LLM context."""
        from harness.tools.http_request import HTTP_REQUEST_DEF

        props = HTTP_REQUEST_DEF.output_schema.get("properties", {})
        assert "url" in props, f"output_schema should include url, got: {list(props.keys())}"
        assert "method" in props, f"output_schema should include method, got: {list(props.keys())}"

    def test_browser_schema_includes_action_and_url(self):
        """Browser output_schema should include action and url."""
        from harness.tools.browser_tool import BROWSER_DEF

        props = BROWSER_DEF.output_schema.get("properties", {})
        assert "action" in props, f"output_schema should include action, got: {list(props.keys())}"
        assert "url" in props, f"output_schema should include url, got: {list(props.keys())}"

    def test_file_op_schema_includes_path(self):
        """File op output_schema should include path."""
        from harness.tools.file_op import FILE_OP_DEF

        props = FILE_OP_DEF.output_schema.get("properties", {})
        assert "path" in props, f"output_schema should include path, got: {list(props.keys())}"


# ── SKILL ─────────────────────────────────────────────────────────


class TestSkill:
    def test_skill_definition(self):
        skill = Skill(
            name="reverse_and_count",
            description="Reverse a string and count chars",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            steps=[],
        )
        td = skill.definition
        assert td.name == "reverse_and_count"
        assert td.idempotency_key_fields == ["text"]
        assert td.timeout_ms == 120000

    def test_skill_multiple_steps(self):
        def step1(ctx, tool_fns):
            text = ctx["input"]["text"]
            return {"reversed": text[::-1]}

        def step2(ctx, tool_fns):
            rev = ctx["intermediate"]["reversed"]
            return {"count": len(rev)}

        skill = Skill(
            name="reverse_and_count",
            description="Reverse a string and count chars",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            steps=[step1, step2],
        )

        tool_fns: dict[str, Any] = {}
        fn = skill.build_fn(lambda: tool_fns)
        import asyncio

        result = asyncio.run(fn({"text": "hello"}))
        assert result["result"]["reversed"] == "olleh"
        assert result["result"]["count"] == 5

    def test_skill_with_external_tool(self):
        def search_step(ctx, tool_fns):
            query = ctx["input"]["query"]
            search_fn = tool_fns.get("search")
            if search_fn:
                return search_fn({"q": query})
            return {"data": f"mock result for {query}"}

        skill = Skill(
            name="research",
            description="Research a topic",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            steps=[search_step],
        )

        search_fn = lambda i: {"data": f"Found: {i['q']}"}  # noqa: E731
        fn = skill.build_fn(lambda: {"search": search_fn})
        import asyncio

        result = asyncio.run(fn({"query": "opencode"}))
        assert result["result"]["data"] == "Found: opencode"


# ── Skill with Executor wiring ────────────────────────────────────


class TestSkillWithExecutor:
    @pytest.mark.asyncio
    async def test_executor_wired_skill_records_events(self, store):
        def search_step(ctx, tool_fns):
            search_fn = tool_fns.get("search")
            if search_fn:
                return search_fn({"q": ctx["input"]["query"]})
            return {"data": "mock"}

        skill = Skill(
            name="research",
            description="Research a topic",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            steps=[search_step],
        )

        executor = ToolExecutor(store)
        search_td = ToolDefinition(
            name="search",
            description="Search",
            idempotency_key_fields=["q"],
            side_effects=[],
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )

        def tool_defs_provider():
            return [search_td]

        search_fn_calls = []

        async def search_fn(input):
            search_fn_calls.append(input)
            return {"data": f"Found: {input['q']}"}

        fn = skill.build_fn(
            lambda: {"search": search_fn},
            executor=executor,
            tool_defs_provider=tool_defs_provider,
        )

        store2 = store
        run_id = "skill-exec-test"
        await store2.append_event(run_id, EventType.RUN_STARTED, {"intent": "test"})

        token = current_run_id.set(run_id)
        try:
            result = await fn({"query": "opencode"})
        finally:
            current_run_id.reset(token)

        assert result["result"]["data"] == "Found: opencode"
        assert len(search_fn_calls) == 1

        events = await store2.get_events(run_id)
        event_types = [e.event_type for e in events]
        assert EventType.TOOL_CALLED in event_types
        assert EventType.TOOL_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_executor_wired_skill_fallback_no_run_id(self):
        """Without a run_id context, executor-wired skill calls tool_fn directly."""

        def noop_step(ctx, tool_fns):
            fn = tool_fns.get("echo")
            return fn({"msg": "hello"}) if fn else {}

        skill = Skill(
            name="noop_skill",
            description="Noop",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            steps=[noop_step],
        )

        store = EventStore(":memory:")
        await store.initialize()
        executor = ToolExecutor(store)
        echo_td = ToolDefinition(
            name="echo",
            description="Echo",
            idempotency_key_fields=["msg"],
            side_effects=[],
        )

        echo_calls = []

        async def echo_fn(input):
            echo_calls.append(input)
            return {"echo": input}

        fn = skill.build_fn(
            lambda: {"echo": echo_fn},
            executor=executor,
            tool_defs_provider=lambda: [echo_td],
        )

        result = await fn({"x": "test"})

        assert result["result"]["echo"]["msg"] == "hello"
        assert len(echo_calls) == 1
        await store.close()


# ── ToolRegistry + real tool defs integration ─────────────────────


class TestToolRegistryWithBuiltinDefs:
    def test_register_http_request(self):
        registry = ToolRegistry()
        registry._register(HTTP_REQUEST_DEF, http_request_fn)
        assert registry.get_tool_def("http_request") is HTTP_REQUEST_DEF
        assert registry.get_tool_fn("http_request") is http_request_fn

    def test_register_file_op(self):
        registry = ToolRegistry()
        registry._register(FILE_OP_DEF, file_op_fn)
        assert registry.get_tool_def("file_op") is FILE_OP_DEF

    def test_build_llm_schemas_multiple(self):
        registry = ToolRegistry()
        registry._register(
            ToolDefinition(name="a", description="", idempotency_key_fields=[], side_effects=[]),
            lambda i: {},
        )
        registry._register(
            ToolDefinition(name="b", description="", idempotency_key_fields=[], side_effects=[]),
            lambda i: {},
        )
        schemas = registry.build_llm_schemas()
        schema_names = [s["function"]["name"] for s in schemas]
        assert "a" in schema_names
        assert "b" in schema_names

    def test_sandbox_violation_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_sandbox_root(tmp)
            with pytest.raises(PermissionError, match="outside the sandbox"):
                import asyncio

                asyncio.run(
                    _file_op_fn(
                        {"operation": "read", "path": "..\\..\\secret.txt"},
                        backend=LocalDirectoryBackend(tmp),
                    )
                )
