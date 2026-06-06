"""Idempotency key computation — SHA256 hash of tool_name + canonical JSON of key fields."""

import hashlib
import json

from harness.core.logger import guard_logger
from harness.models.tools import ToolDefinition

_log_idem = guard_logger("executor.idempotency")


class IdempotencyKeyGenerator:
    @staticmethod
    def compute(tool_def: ToolDefinition, input: dict) -> str | None:
        if tool_def.idempotency_key_fields is None:
            _log_idem.debug("Idempotency disabled for tool '%s' (idempotency_key_fields=None)", tool_def.name)
            return None
        fields = tool_def.idempotency_key_fields
        subset = {k: input[k] for k in fields if k in input} if fields else {}
        payload = json.dumps(subset, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        seed = f"{tool_def.name}:{payload}"
        h = hashlib.sha256(seed.encode()).hexdigest()
        _log_idem.debug("Computed ik for %s: fields=%s keys=%d hash=%.12s",
                        tool_def.name, fields, len(subset), h)
        return h
