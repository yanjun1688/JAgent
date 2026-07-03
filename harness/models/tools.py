"""Tool Layer contract models — ToolDefinition, SideEffect, Guardrail, RetryPolicy."""

from enum import Enum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

JSONSchema: TypeAlias = dict[str, Any]


class SuccessIndicator(BaseModel):
    """Declares how to determine if a tool's output is semantically successful.

    During post-execution evaluation the executor reads ``field`` from the
    output dict and compares it against ``value`` using ``op``.  When the
    comparison succeeds the output is considered a semantic success; otherwise
    it is a SOFT_ERROR (still TOOL_COMPLETED, not TOOL_FAILED).
    """
    field: str
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
    value: Any


class SideEffect(str, Enum):
    WRITE = "write"
    DELETE = "delete"
    EXTERNAL = "external"


class DependencyConstraint(BaseModel):
    """Declarative dependency: require a specific event type (with optional payload filter) to exist."""
    event_type: str
    payload_filter: dict[str, Any] = Field(default_factory=dict)
    message: str = ""

class Guardrail(BaseModel):
    guardrail_type: str
    config: dict[str, Any] = Field(default_factory=dict)


class RetryPolicy(BaseModel):
    max_retries: int = 3
    backoff_base_ms: int = 1000
    retryable_errors: list[str] = Field(default_factory=list)


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: JSONSchema = Field(default_factory=dict)
    output_schema: JSONSchema = Field(default_factory=dict)
    idempotency_key_fields: list[str] | None = None
    side_effects: list[SideEffect]
    timeout_ms: int = 30000
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    guardrails: list[Guardrail] | None = None
    requires_confirmation: bool = False
    depends_on: list[DependencyConstraint] = []
    dangerous_with: list[str] = []
    max_parallel: int = 10
    success_indicator: SuccessIndicator | None = None
