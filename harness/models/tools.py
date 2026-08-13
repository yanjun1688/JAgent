"""Tool Layer contract models — ToolDefinition, SideEffect, Guardrail, RetryPolicy."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

JSONSchema: TypeAlias = dict[str, Any]


class SuccessIndicator(BaseModel):
    """Declares how to determine if a tool's output is semantically successful.

    During post-execution evaluation the executor reads ``field`` from the
    output dict and compares it against ``value`` using ``op``.  When the
    comparison succeeds the output is considered a semantic success; otherwise
    it is UNSUCCESSFUL (still TOOL_COMPLETED, not TOOL_FAILED).
    """

    field: str
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "in"]
    value: Any


class SideEffect(str, Enum):
    WRITE = "write"
    DELETE = "delete"
    EXTERNAL = "external"


class ToolScopeTarget(BaseModel):
    """工具声明的 scope 目标（ADR-010 D-02），取代 ScopeGuardrail 名称特判。

    ``kind`` 声明目标类型（path/domain/command），``input_field`` 声明从
    input 哪个字段提取目标；``config_key`` 指定 guardrail 白名单键，空则按
    kind 默认（allowed_directories / allowed_domains / allowed_commands）。
    """

    kind: Literal["path", "domain", "command"]
    input_field: str
    config_key: str = ""


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


class OperationContract(BaseModel):
    """Per-operation tool contract (S02, 问题五 / C-04 / D-01).

    The Tool Layer previously treated a tool as an atomic unit: side effects,
    confirmation, idempotency and probe-allowance were declared for the whole
    tool, which wrongly made ``file_op.read`` inherit the write/delete side
    effects and ``http_request.GET`` inherit the external side effect.  This
    model declares those attributes *per operation* so a read-only operation
    can be probed safely while mutating operations stay guarded.

    ``ref_allowed_fields`` (C-04): input field name → whether a ``$step.output``
    reference is allowed in that field.  Unlisted fields default to False —
    references are denied unless explicitly allowed (``file_op.path`` and
    ``file_op.content`` are never allowed, D-01).
    """

    operation: str = ""
    input_schema: JSONSchema = Field(default_factory=dict)
    output_schema: JSONSchema = Field(default_factory=dict)
    side_effects: list[SideEffect] = Field(default_factory=list)
    requires_confirmation: bool = False
    idempotency_key_fields: list[str] | None = None
    probe_allowed: bool = False
    retry_policy: RetryPolicy | None = None
    ref_allowed_fields: dict[str, bool] = Field(default_factory=dict)
    # ADR-010 D-02: 条件必填键 —— 命中该 operation 时补充校验 input 必填字段。
    required_input: list[str] = Field(default_factory=list)

    def ref_allowed(self, field: str) -> bool:
        return bool(self.ref_allowed_fields.get(field, False))


def resolve_operation_contract(tool_def: ToolDefinition, input: dict) -> OperationContract | None:
    """Resolve the ``OperationContract`` matching an input dict.

    The discriminant key is declared by the tool itself via ``operation_key``
    (ADR-010 D-02) — ``operation`` for file_op, ``method`` for http_request,
    ``action`` for browser.  When the input has no discriminant value and the
    tool declares ``default_operation``, that operation is returned (e.g.
    http_request defaults to GET).  Returns ``None`` when the tool declares no
    per-operation contracts or no value matches — callers then fall back to
    tool-level behavior (backward compatible).
    """
    if not tool_def.operations:
        return None
    value = input.get(tool_def.operation_key)
    if value is not None:
        for op in tool_def.operations:
            if op.operation == value:
                return op
        return None
    if tool_def.default_operation is not None:
        return next((op for op in tool_def.operations if op.operation == tool_def.default_operation), None)
    return None


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
    # S02 (per-operation contracts): when non-empty, per-operation attributes
    # OVERRIDE the tool-level ones for matching operations.  Empty list keeps
    # the legacy tool-level behaviour (backward compatible).
    operations: list[OperationContract] = Field(default_factory=list)
    # ADR-010 D-02: operation 判别键与默认操作（取代魔法 `_OPERATION_KEYS`）。
    operation_key: str = "operation"
    default_operation: str | None = None
    # ADR-010 D-02: scope 目标声明（取代 ScopeGuardrail 名称特判）。
    scope_targets: list[ToolScopeTarget] = Field(default_factory=list)

    def resolve_operation(self, input: dict) -> OperationContract | None:
        return resolve_operation_contract(self, input)
