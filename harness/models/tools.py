"""Tool Layer contract models — ToolDefinition, SideEffect, Guardrail, RetryPolicy."""

from enum import Enum
from typing import Any, TypeAlias

from pydantic import BaseModel, Field

JSONSchema: TypeAlias = dict[str, Any]


class SideEffect(str, Enum):
    WRITE = "write"
    DELETE = "delete"
    EXTERNAL = "external"


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
    idempotency_key_fields: list[str]
    side_effects: list[SideEffect]
    timeout_ms: int = 30000
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    guardrails: list[Guardrail] | None = None
    requires_confirmation: bool = False
