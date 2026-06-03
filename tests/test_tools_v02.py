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
    file_op_fn,
    http_request_fn,
    set_sandbox_root,
)
from harness.tools.skill import Skill

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
        registry.register(td, fn)
        assert registry.get_tool_def("test_tool") is td
        assert registry.get_tool_fn("test_tool") is fn
        assert "test_tool" in registry
        assert len(registry) == 1

    def test_register_duplicate_raises(self):
        registry = ToolRegistry()
        td = ToolDefinition(
            name="dup", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]
        )
        registry.register(td, lambda i: {})
        td2 = ToolDefinition(
            name="dup", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]
        )
        with pytest.raises(ValueError, match="already registered"):
            registry.register(td2, lambda i: {})

    def test_list_tool_defs(self):
        registry = ToolRegistry()
        td1 = ToolDefinition(
            name="a", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]
        )
        td2 = ToolDefinition(
            name="b", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]
        )
        registry.register(td1, lambda i: {})
        registry.register(td2, lambda i: {})
        defs = registry.list_tool_defs()
        assert len(defs) == 2
        names = {d.name for d in defs}
        assert names == {"a", "b"}

    def test_list_tool_fns(self):
        registry = ToolRegistry()
        fn_a = lambda i: {"x": 1}  # noqa: E731
        fn_b = lambda i: {"y": 2}  # noqa: E731
        registry.register(
            ToolDefinition(name="a", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            fn_a,
        )
        registry.register(
            ToolDefinition(name="b", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            fn_b,
        )
        fns = registry.list_tool_fns()
        assert fns["a"] is fn_a
        assert fns["b"] is fn_b

    def test_remove_tool(self):
        registry = ToolRegistry()
        td = ToolDefinition(
            name="temp", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]
        )
        registry.register(td, lambda i: {})
        assert "temp" in registry
        registry.remove("temp")
        assert "temp" not in registry
        assert len(registry) == 0

    def test_tool_names_property(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(name="x", description="", idempotency_key_fields=[], side_effects=[SideEffect.WRITE]),
            lambda i: {},
        )
        registry.register(
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
        registry.register(td, lambda i: {"greeting": f"Hello, {i['name']}!"})
        schemas = registry.build_llm_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "greet"
        assert schemas[0]["type"] == "function"


# ── http_request ──────────────────────────────────────────────────


class TestHttpRequestDefinition:
    def test_definition_fields(self):
        assert HTTP_REQUEST_DEF.name == "http_request"
        assert HTTP_REQUEST_DEF.idempotency_key_fields == ["url", "method", "body"]
        assert SideEffect.EXTERNAL in HTTP_REQUEST_DEF.side_effects
        assert HTTP_REQUEST_DEF.timeout_ms == 60000

    def test_schema_requires_url(self):
        props = HTTP_REQUEST_DEF.input_schema["properties"]
        assert "url" in props
        assert "method" in props
        assert HTTP_REQUEST_DEF.input_schema["required"] == ["url"]


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
        with tempfile.TemporaryDirectory() as tmp:
            set_sandbox_root(tmp)
            yield tmp

    async def test_write_and_read(self):
        result = await file_op_fn({"operation": "write", "path": "hello.txt", "content": "world"})
        assert result["success"] is True

        result = await file_op_fn({"operation": "read", "path": "hello.txt"})
        assert result["success"] is True
        assert result["content"] == "world"

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


# ── ToolRegistry + real tool defs integration ─────────────────────

class TestToolRegistryWithBuiltinDefs:
    def test_register_http_request(self):
        registry = ToolRegistry()
        registry.register(HTTP_REQUEST_DEF, http_request_fn)
        assert registry.get_tool_def("http_request") is HTTP_REQUEST_DEF
        assert registry.get_tool_fn("http_request") is http_request_fn

    def test_register_file_op(self):
        registry = ToolRegistry()
        registry.register(FILE_OP_DEF, file_op_fn)
        assert registry.get_tool_def("file_op") is FILE_OP_DEF

    def test_build_llm_schemas_multiple(self):
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(name="a", description="", idempotency_key_fields=[], side_effects=[]),
            lambda i: {},
        )
        registry.register(
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
                asyncio.run(file_op_fn({"operation": "read", "path": "..\\..\\secret.txt"}))
