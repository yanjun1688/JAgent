"""Idempotency key computation — SHA256 hash of tool_name + canonical JSON of key fields."""

import hashlib
import json

from harness.models.tools import ToolDefinition


class IdempotencyKeyGenerator:
    @staticmethod
    def compute(tool_def: ToolDefinition, input: dict) -> str | None:
        if tool_def.idempotency_key_fields is None:
            return None
        fields = tool_def.idempotency_key_fields
        subset = {k: input[k] for k in fields if k in input} if fields else {}
        payload = json.dumps(subset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        seed = f"{tool_def.name}:{payload}"
        return hashlib.sha256(seed.encode()).hexdigest()
