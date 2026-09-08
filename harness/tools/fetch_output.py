"""v3.4 (F-2, ADR-011): ``fetch_output`` — trusted read-only blob retrieval tool.

Large tool outputs are offloaded to workspace-scoped content-addressed blobs
(see :mod:`harness.tools.output_store`). The event stream keeps only a compact
``{ref, sha256, bytes, summary}`` placeholder. When the Agent needs the full
body it calls this tool with the ``ref``; retrieval is read-only, idempotent and
strictly confined to the managed blob namespace (the ref is validated as a
64-hex sha256 + ``.json`` before the backend is ever touched, so path traversal
or cross-workspace reads are impossible).
"""

from __future__ import annotations

from harness.core.logger import guard_logger
from harness.models.tools import SideEffect
from harness.tools.base import BaseTool, operation
from harness.tools.output_store import BLOB_NAMESPACE, is_valid_blob_ref

_log = guard_logger("tool.fetch_output")


class FetchOutputTool(BaseTool):
    """Retrieve a previously offloaded tool output by its content-addressed ref."""

    name = "fetch_output"
    description = (
        "Retrieve the full content of a large tool result that was offloaded "
        "to an output blob. Pass the 'ref' from a truncated tool result "
        "(shape: '<64-hex-sha256>.json'). Read-only; confined to this run's outputs."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["fetch"]},
            "ref": {
                "type": "string",
                "description": "Content-addressed blob ref, e.g. '<sha256>.json'",
            },
        },
        "required": ["operation", "ref"],
    }
    output_schema = {}
    operation_key = "operation"
    # Read-only: no external side effects, safe to mark probe / retry.
    side_effects: list[SideEffect] = []
    idempotency_key_fields = ["ref"]
    timeout_ms = 15000
    needs_backend = True

    @operation("fetch", probe_allowed=True, idempotency_key_fields=["ref"], ref_allowed_fields={"ref": False})
    async def fetch(self, input):
        ref = str(input.get("ref") or "")
        if not is_valid_blob_ref(ref):
            # Fail-closed: never touch the backend with an unvalidated ref.
            return {"success": False, "error": f"invalid output ref: {ref!r} (expected <64-hex>.json)"}
        if self.backend is None:
            raise RuntimeError("Execution backend is required for fetch_output")
        path = f"{BLOB_NAMESPACE}/{ref}"
        result = await self.backend.read(path)
        if not (isinstance(result, dict) and result.get("success") and result.get("content") is not None):
            error = result.get("error", "blob not found") if isinstance(result, dict) else "blob not found"
            return {"success": False, "ref": ref, "error": error}
        import json

        try:
            return json.loads(result["content"])
        except (ValueError, TypeError) as exc:
            return {"success": False, "ref": ref, "error": f"corrupt blob: {exc}"}

    async def run(self, input):
        if self.backend is None:
            raise RuntimeError("Execution backend is required for fetch_output")
        value = input.get(self.operation_key) or "fetch"
        handler = self._operation_handlers.get(value)
        if handler is None:
            raise KeyError(f"Unknown operation '{value}' for tool '{self.name}'")
        return await handler(self, input)
