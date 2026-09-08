"""F-2 (v3.4): 大工具输出收口为 blob 引用（ADR-011）。

回归 run e05087b6：http/MCP 大输出（open-meteo 67796 字符）全量落库、折叠、
计入 token 估算，误触发紧急压缩并撑大 Event Store。收口后事件只存
``{summary, ref, sha256, bytes, truncated}``，完整体落 workspace blob，按需经
``fetch_output`` 取回。
"""

from __future__ import annotations

import pytest

from harness.tools.output_store import (
    OutputBlobStore,
    build_ref_placeholder,
    is_valid_blob_ref,
    summarize_output,
)


class _FakeBackend:
    """In-memory ExecutionBackend stand-in for blob writes/reads."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def write(self, path: str, content: str) -> dict:
        self.files[path] = content
        return {"path": path, "bytes": len(content)}

    async def read(self, path: str) -> dict:
        if path not in self.files:
            raise FileNotFoundError(path)
        return {"path": path, "content": self.files[path]}


class TestSummarize:
    def test_small_output_not_stored(self):
        # store() is async; tested below. Here just the summary helper.
        assert summarize_output({"a": 1}, 50).startswith("{")

    def test_summary_bounded(self):
        big = {"data": "x" * 5000}
        s = summarize_output(big, 200)
        assert len(s) <= 230
        assert "truncated" in s or len(s) <= 200


class TestBlobRefValidation:
    @pytest.mark.parametrize(
        "ref",
        ["a" * 64 + ".json", "0123456789abcdef" * 4 + ".json"],
    )
    def test_valid_refs(self, ref):
        assert is_valid_blob_ref(ref)

    @pytest.mark.parametrize(
        "ref",
        [
            "../escape.json",
            "a/b.json",
            "a" * 63 + ".json",
            "z" * 64 + ".json",  # non-hex
            "",
            "x.txt",
            ".json",
            "a" * 64 + ".json/..",
        ],
    )
    def test_invalid_refs(self, ref):
        assert not is_valid_blob_ref(ref)


class TestOutputBlobStore:
    async def test_small_output_returns_none(self):
        backend = _FakeBackend()
        store = OutputBlobStore(backend=backend, inline_max_chars=1000)
        result = await store.store({"ok": True}, tool_call_id="tc-1", tool_name="echo")
        assert result is None
        assert backend.files == {}

    async def test_large_output_stored_as_blob(self):
        backend = _FakeBackend()
        store = OutputBlobStore(backend=backend, inline_max_chars=200)
        big = {"daily": {"temperature_2m_max": list(range(200))}}
        stored = await store.store(big, tool_call_id="tc-9", tool_name="http_request", step_id="s1")
        assert stored is not None
        assert stored.truncated is True
        assert stored.bytes > 200
        assert is_valid_blob_ref(stored.ref)
        assert len(stored.sha256) == 64
        # Blob physically written under the managed namespace.
        assert any(stored.ref in path for path in backend.files)
        # Placeholder is small and self-describing.
        placeholder = build_ref_placeholder(stored)
        assert placeholder["truncated"] is True
        assert placeholder["ref"] == stored.ref
        assert placeholder["bytes"] == stored.bytes
        assert len(placeholder["summary"]) <= 200
        # The placeholder is bounded and orders of magnitude smaller than the body.
        import json as _json

        assert len(_json.dumps(placeholder, ensure_ascii=False)) < 700
        assert stored.bytes > 700

    async def test_fetch_roundtrip_and_tamper_rejection(self):
        backend = _FakeBackend()
        store = OutputBlobStore(backend=backend, inline_max_chars=50)
        big = {"v": "y" * 300}
        stored = await store.store(big, tool_call_id="tc-1", tool_name="http_request")
        fetched = await store.fetch(stored.ref)
        assert fetched["v"] == "y" * 300
        # Path traversal / foreign refs must be rejected before touching backend.
        with pytest.raises(ValueError):
            await store.fetch("../escape.json")

    async def test_content_addressing_dedupes(self):
        backend = _FakeBackend()
        store = OutputBlobStore(backend=backend, inline_max_chars=10)
        a = await store.store({"x": "z" * 100}, tool_call_id="tc-1", tool_name="t")
        b = await store.store({"x": "z" * 100}, tool_call_id="tc-2", tool_name="t")
        assert a.ref == b.ref
        # Same content → single physical blob.
        assert len([p for p in backend.files if p.endswith(".json")]) == 1
