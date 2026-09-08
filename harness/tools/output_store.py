"""v3.4 (F-2, ADR-011): large tool-output offload to workspace-scoped blobs.

Trusted Tool-Layer helper. When a tool output exceeds an inline budget it is
serialized, content-addressed (sha256) and written to a managed namespace inside
the run's workspace via the injected :class:`ExecutionBackend`. The event stream
then carries only a compact placeholder ``{summary, ref, sha256, bytes,
truncated}``; the full body is read back on demand by the trusted read-only
``fetch_output`` tool.

Design constraints:
- No new mutable state store: blobs live in the workspace (same scope as files).
- Refs are content-addressed and strictly validated (hex sha256 + ``.json``), so
  ``fetch`` can never path-traverse or read outside the managed namespace.
- fold/门控 never perform I/O — they only hold the ref string.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from harness.execution.base import ExecutionBackend

# Managed namespace inside the workspace root (LocalDirectoryBackend enforces
# containment; this path is always under the sandbox root).
BLOB_NAMESPACE = ".harness_outputs"

# The placeholder that stays in the event stream must stay small regardless of
# the inline threshold; the summary is capped to a fixed, human-readable size.
SUMMARY_MAX_CHARS = 120

# A blob ref is exactly a 64-char lowercase hex sha256 with a .json suffix and
# no path separators — this is the entire allow-list for fetch validation.
_REF_RE = re.compile(r"^[0-9a-f]{64}\.json$")


@dataclass(frozen=True)
class StoredBlob:
    ref: str
    sha256: str
    bytes: int
    summary: str
    truncated: bool = True
    tool_call_id: str = ""
    tool_name: str = ""
    step_id: str | None = None
    workspace_id: str | None = None


def is_valid_blob_ref(ref: str) -> bool:
    """True iff ref is a well-formed content-addressed blob reference (no I/O)."""
    return bool(ref) and bool(_REF_RE.match(ref))


def summarize_output(output: Any, max_chars: int) -> str:
    """Produce a bounded, single-line string summary of a tool output."""
    try:
        text = json.dumps(output, ensure_ascii=False, default=str, sort_keys=True)
    except Exception:
        text = repr(output)
    text = text.replace("\n", " ")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...(truncated)"


def _serialize(output: Any) -> str:
    return json.dumps(output, ensure_ascii=False, default=str, sort_keys=True)


def build_ref_placeholder(blob: StoredBlob, summary_max: int = 300) -> dict[str, Any]:
    """Compact, self-describing placeholder that replaces the full output in events."""
    return {
        "truncated": blob.truncated,
        "ref": blob.ref,
        "sha256": blob.sha256,
        "bytes": blob.bytes,
        "summary": blob.summary[:summary_max],
        "note": "full output offloaded to blob; use fetch_output(ref) to retrieve",
    }


class OutputBlobStore:
    """Offloads and retrieves large tool outputs via a workspace-scoped backend."""

    def __init__(self, backend: ExecutionBackend | None, inline_max_chars: int = 8000):
        self._backend = backend
        self.inline_max_chars = inline_max_chars

    async def store(
        self,
        output: Any,
        *,
        tool_call_id: str,
        tool_name: str,
        step_id: str | None = None,
        workspace_id: str | None = None,
    ) -> StoredBlob | None:
        """Offload ``output`` if it exceeds the inline budget; else return None.

        Returns None when there is no backend (caller keeps the inline output) or
        the output is small enough to stay inline.
        """
        if self._backend is None:
            return None
        serialized = _serialize(output)
        if len(serialized) <= self.inline_max_chars:
            return None
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        ref = f"{digest}.json"
        path = f"{BLOB_NAMESPACE}/{ref}"
        # Idempotent content-addressed write: same content → same path (dedupes).
        await self._backend.write(path, serialized)
        return StoredBlob(
            ref=ref,
            sha256=digest,
            bytes=len(serialized.encode("utf-8")),
            summary=summarize_output(output, min(self.inline_max_chars, SUMMARY_MAX_CHARS)),
            truncated=True,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            step_id=step_id,
            workspace_id=workspace_id,
        )

    async def fetch(self, ref: str) -> Any:
        """Read back a blob by ref, strictly validating the reference first."""
        if not is_valid_blob_ref(ref):
            raise ValueError(f"invalid output blob ref: {ref!r}")
        if self._backend is None:
            raise ValueError("output blob store has no backend configured")
        path = f"{BLOB_NAMESPACE}/{ref}"
        result = await self._backend.read(path)
        content = result.get("content") if isinstance(result, dict) else None
        if content is None:
            raise ValueError(f"blob not found: {ref}")
        return json.loads(content)
