"""Step JSON Schema contract shared by prompt generation and response parsing.

Schema definition → prompt generation → output validation use a single source.
"""

from __future__ import annotations

_STEP_SCHEMA_SIMPLE = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique step id, e.g. 's1', 's2'"},
        "tool": {"type": "string", "description": "Tool name from available tools"},
        "input": {
            "type": "object",
            "description": "ALL action/url/query params go HERE, not at step level",
            "additionalProperties": True,
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs of steps this step depends on (empty if independent)",
        },
        "description": {"type": "string", "description": "What this step does"},
        "probe": {
            "type": "boolean",
            "description": "v2.2 (D4): 探测型步骤声明 — 目标是'查清楚'，答案'没有/不存在'就是正确答案。"
            "仅无副作用（只读/查询）工具步骤可标，否则计划被系统拒绝。"
            "声明 probe 后，该步骤 UNSUCCESSFUL 算正常（step_normal=True）。",
        },
    },
    "required": ["id", "tool", "input"],
    "additionalProperties": False,
}


def build_step_schema_text() -> str:
    """Return LLM-readable schema text with single braces (for direct use)."""
    return """Top-level JSON MUST contain:
  - "intent" (string, required): a one-sentence summary of what this plan aims to accomplish.
    Rephrase the user's goal in your own words — DO NOT copy-paste the user intent verbatim.
  - "steps" (array): list of step objects. Use [] for no-action plans.
  - "declared_operations" (array of objects, recommended): the operations YOU declare this plan
    covers — a LLM self-check declaration, NOT the user's delivery contract. Each item is
    {"tool": "<tool>", "input": {key: value}} declaring a covered operation
    (e.g. {"tool": "file_op", "input": {"operation": "write", "path": "x.txt"}}).
    The system uses it ONLY to check that your plan is self-consistent; it never authorizes
    new side effects and never decides completion. Real delivery requirements are enforced
    by the trusted delivery contracts. Do NOT omit operations the user explicitly requested
    (creating/writing/deleting files, fetching URLs, etc.).

Each step MUST be a JSON object with exactly these fields:
  - "id" (string, required): unique identifier, e.g. "s1"
  - "tool" (string, required): tool name from the available tools list
  - "input" (object, required): ALL parameters go inside this object.
    NEVER put parameters like 'action', 'url', 'query' at the step level.
    Good: {"id": "s1", "tool": "http_request", "input": {"action": "GET", "url": "..."}}
    Bad:  {"id": "s1", "tool": "http_request", "action": "GET", "url": "..."}
  - "depends_on" (array of strings, optional): step dependencies for DAG ordering
  - "description" (string, optional): what this step does
  - "probe" (boolean, optional, v2.2): set true ONLY when the step's goal is to
    CHECK something and "not found / does not exist" IS the correct answer
    (e.g. verifying an endpoint, checking a flag). Only read-only / query tools
    may be marked probe — the system rejects plans that mark a mutating tool as
    probe. A probe step that returns "not found" counts as normally completed.

No other fields are allowed at the step level."""


def validate_step(step: dict, step_index: int) -> str | None:
    """验证单个 step 是否符合 schema，返回错误描述（None 表示通过）。"""
    import jsonschema
    from jsonschema import ValidationError

    try:
        jsonschema.validate(instance=step, schema=_STEP_SCHEMA_SIMPLE)
    except ValidationError as e:
        bad_field = ".".join(str(p) for p in e.path) if e.path else "structure"
        return (
            f"Step '{step.get('id', f'#{step_index}')}' has an error: "
            f"field '{bad_field}': {e.message}. "
            f"Remember: ALL tool parameters must be inside 'input'."
        )
    return None


# Pre-compute for retry_prompt (single braces, no .format())
_STEP_SCHEMA_RAW = build_step_schema_text()


def retry_prompt(last_error: str) -> str:
    """生成带具体错误信息的重试提示。"""
    return (
        f"Your previous response had a format error:\n{last_error}\n\n"
        f"Please fix this and output ONLY valid JSON.\n"
        f"Remember the required format:\n{_STEP_SCHEMA_RAW}"
    )
