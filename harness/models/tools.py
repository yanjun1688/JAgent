"""Tool Layer contract models — ToolDefinition, SideEffect, Guardrail, RetryPolicy."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

JSONSchema: TypeAlias = dict[str, Any]

# Field-role single source (D-01 / C-04 revision, see
# review_20260912_field_role_contract_diff.md): the only per-field declaration a
# tool author makes. Ref policy and planner visibility are DERIVED from it, so
# there is no parallel ``ref_allowed_fields`` list to keep in sync.
FIELD_ROLE_KEY = "x-role"


class FieldRole(str, Enum):
    """Data-flow role of a tool input field."""

    DATA = "data"  # data sink: a $ref (non-trusted-derived value) may flow in
    AUTHORITY = "authority"  # authority/selector sink (path/url/tool_name/...): literal only


def field_role(input_schema: JSONSchema | None, field: str) -> FieldRole:
    """Derive a field's role from its ``input_schema`` property annotation.

    The policy is fail-closed: a field is AUTHORITY unless its schema property
    explicitly declares ``x-role: data``. Only DATA fields accept ``$ref``.
    """
    props = (input_schema or {}).get("properties") if isinstance(input_schema, dict) else None
    if not isinstance(props, dict):
        return FieldRole.AUTHORITY
    prop = props.get(field)
    if isinstance(prop, dict) and prop.get(FIELD_ROLE_KEY) == FieldRole.DATA.value:
        return FieldRole.DATA
    return FieldRole.AUTHORITY


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


def unknown_tool_message(name: str) -> str:
    """Single source of the canonical "tool does not exist" fragment (R7).

    Every trusted seam that rejects an unregistered tool must derive its core
    message from this function (call sites may add their own prefix for triage
    clustering); none may hand-write ``unknown tool '{name}'`` again.

    Lives in the zero-dependency ``models`` layer — not ``tools.registry`` — so
    that the lowest contract layer (``models.intent``) can reuse it without
    depending upward on ``tools``/``core``. New code must import it from here;
    ``tools.registry`` only re-exports it for backward compatibility.
    """
    return f"unknown tool '{name}'"


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


class OperationAttributes(BaseModel):
    """工具级与 operation 级共享的契约属性（消除两份字段声明）。

    ``ToolDefinition`` 与 ``OperationContract`` 都继承本类；per-operation 值如何
    覆盖 tool-level 由 ``ToolDefinition.effective_*`` 注入器单点定义。
    """

    input_schema: JSONSchema = Field(default_factory=dict)
    output_schema: JSONSchema = Field(default_factory=dict)
    side_effects: list[SideEffect] = Field(default_factory=list)
    requires_confirmation: bool = False
    idempotency_key_fields: list[str] | None = None
    retry_policy: RetryPolicy | None = None


class OperationContract(OperationAttributes):
    """Per-operation tool contract (S02, 问题五 / C-04 / D-01).

    The Tool Layer previously treated a tool as an atomic unit: side effects,
    confirmation, idempotency and probe-allowance were declared for the whole
    tool, which wrongly made ``file_op.read`` inherit the write/delete side
    effects and ``http_request.GET`` inherit the external side effect.  This
    model declares those attributes *per operation* so a read-only operation
    can be probed safely while mutating operations stay guarded.

    ``ref_allowed`` (D-01/C-04): whether a ``$step.output`` reference is allowed
    in an input field is DERIVED from that field's role in the tool-level
    ``input_schema`` (``field_role``): only ``x-role: data`` fields accept a
    reference; everything else is AUTHORITY (fail-closed). There is no
    per-operation ``ref_allowed_fields`` list to maintain.
    """

    operation: str = ""
    probe_allowed: bool = False
    # ADR-010 D-02: 条件必填键 —— 命中该 operation 时补充校验 input 必填字段。
    required_input: list[str] = Field(default_factory=list)


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


class ToolDefinition(OperationAttributes):
    name: str
    description: str
    # 工具级必填副作用；per-operation 覆盖由 effective_side_effects 决定。
    side_effects: list[SideEffect]
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_ms: int = 30000
    guardrails: list[Guardrail] | None = None
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

    def effective_side_effects(self, input: dict) -> list[SideEffect]:
        """命中 operation 即用其声明（含空），否则回退工具级 —— 单一优先级。"""
        op = self.resolve_operation(input)
        return op.side_effects if op is not None else self.side_effects

    def effective_requires_confirmation(self, input: dict) -> bool:
        """命中 operation 即用其声明，否则回退工具级。"""
        op = self.resolve_operation(input)
        return op.requires_confirmation if op is not None else self.requires_confirmation

    def effective_idempotency_key_fields(self, input: dict) -> list[str] | None:
        """operation 显式声明了键则用之，否则回退工具级（None = 继承）。"""
        op = self.resolve_operation(input)
        if op is not None and op.idempotency_key_fields is not None:
            return op.idempotency_key_fields
        return self.idempotency_key_fields

    def ref_allowed(self, field: str) -> bool:
        """A ``$ref`` may flow into ``field`` only when it is ``x-role: data``."""
        return field_role(self.input_schema, field) is FieldRole.DATA
