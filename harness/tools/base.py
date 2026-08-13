"""Tool Layer 声明式抽象（ADR-010 D-01）— BaseTool + @operation。

工具作者只需继承 ``BaseTool``：
  - 用类属性声明契约（schema / operation_key / side_effects / guardrails ...）
  - 用 ``@operation(name, ...)`` 声明 per-operation 业务方法，或覆写 ``run()``
  - ``needs_backend`` / ``needs_mcp_manager`` 声明依赖，由装配器/executor 注入

``to_definition()`` 自动合成 ``ToolDefinition``（operation 未声明 output_schema
时继承工具级）；``invoke()`` 统一入口注入依赖并按 operation_key 分发。
``BaseTool`` 是非受信"定义载体"：横切能力（schema/guardrails/幂等/确认/事件/
超时/重试/语义）由受信 ToolExecutor 持有，本类只表达契约与业务。
"""

from __future__ import annotations

import contextvars
from abc import ABC
from typing import Any, Callable, ClassVar

from harness.models.tools import (
    DependencyConstraint,
    Guardrail,
    JSONSchema,
    OperationContract,
    RetryPolicy,
    SideEffect,
    SuccessIndicator,
    ToolDefinition,
    ToolScopeTarget,
)

# run 级可变依赖注入：executor 在调用前设置，invoker 读取后传给工具（ADR-010 D-03）。
# 沿用现有 current_run_id 的 contextvar 模式，避免按工具名 partial 特判。
current_backend: contextvars.ContextVar[Any] = contextvars.ContextVar("current_backend", default=None)


def operation(
    name: str,
    *,
    input_schema: JSONSchema | None = None,
    output_schema: JSONSchema | None = None,
    side_effects: list[SideEffect] | None = None,
    requires_confirmation: bool = False,
    idempotency_key_fields: list[str] | None = None,
    probe_allowed: bool = False,
    retry_policy: RetryPolicy | None = None,
    ref_allowed_fields: dict[str, bool] | None = None,
    required_input: list[str] | None = None,
) -> Callable[[Callable], Callable]:
    """标记业务方法并生成 ``OperationContract``（ADR-010 D-01）。

    未声明 ``output_schema`` 时，``BaseTool.to_definition()`` 让对应 contract
    继承工具级 ``output_schema``。
    """

    def deco(fn: Callable) -> Callable:
        fn._op_contract = OperationContract(  # type: ignore[attr-defined]
            operation=name,
            input_schema=input_schema or {},
            output_schema=output_schema or {},
            side_effects=side_effects or [],
            requires_confirmation=requires_confirmation,
            idempotency_key_fields=idempotency_key_fields,
            probe_allowed=probe_allowed,
            retry_policy=retry_policy,
            ref_allowed_fields=ref_allowed_fields or {},
            required_input=required_input or [],
        )
        return fn

    return deco


class BaseTool(ABC):
    """声明式工具基类 — 契约声明 + 业务实现 + 统一入口。"""

    # ── 声明（类属性，工具作者填写）──────────────────────────────
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    input_schema: ClassVar[JSONSchema] = {}
    output_schema: ClassVar[JSONSchema] = {}
    operation_key: ClassVar[str] = "operation"
    default_operation: ClassVar[str | None] = None
    side_effects: ClassVar[list[SideEffect]] = []
    idempotency_key_fields: ClassVar[list[str] | None] = None
    guardrails: ClassVar[list[Guardrail]] = []
    scope_targets: ClassVar[list[ToolScopeTarget]] = []
    requires_confirmation: ClassVar[bool] = False
    timeout_ms: ClassVar[int] = 30000
    retry_policy: ClassVar[RetryPolicy] = RetryPolicy()
    success_indicator: ClassVar[SuccessIndicator | None] = None
    dangerous_with: ClassVar[list[str]] = []
    max_parallel: ClassVar[int] = 10
    depends_on: ClassVar[list[DependencyConstraint]] = []
    needs_backend: ClassVar[bool] = False
    needs_mcp_manager: ClassVar[bool] = False

    # ── 实例级（装配/executor 注入）──────────────────────────────
    backend: Any = None
    mcp_manager: Any = None
    _run_id: str = ""

    # ── operation 处理器（@operation 装饰器收集，类级）────────────
    _operations: dict[str, OperationContract] = {}
    _operation_handlers: dict[str, Callable] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        ops: dict[str, OperationContract] = {}
        handlers: dict[str, Callable] = {}
        for member_name, member in vars(cls).items():
            meta = getattr(member, "_op_contract", None)
            if meta is not None:
                ops[meta.operation] = meta
                handlers[meta.operation] = member
        cls._operations = ops
        cls._operation_handlers = handlers

    # ── 契约合成 ─────────────────────────────────────────────────

    def to_definition(self) -> ToolDefinition:
        """从类声明合成 ``ToolDefinition``（ADR-010 D-01）。"""
        ops: list[OperationContract] = []
        for contract in self._operations.values():
            if not contract.output_schema:
                contract = contract.model_copy(update={"output_schema": self.output_schema})
            ops.append(contract)
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            idempotency_key_fields=self.idempotency_key_fields,
            side_effects=self.side_effects,
            guardrails=self.guardrails or None,
            timeout_ms=self.timeout_ms,
            retry_policy=self.retry_policy,
            success_indicator=self.success_indicator,
            requires_confirmation=self.requires_confirmation,
            depends_on=self.depends_on,
            dangerous_with=self.dangerous_with,
            max_parallel=self.max_parallel,
            operations=ops,
            operation_key=self.operation_key,
            default_operation=self.default_operation,
            scope_targets=self.scope_targets,
        )

    # ── 统一入口 ─────────────────────────────────────────────────

    async def invoke(self, input: dict, *, backend: Any = None, mcp_manager: Any = None, run_id: str = "") -> Any:
        """注入依赖 → dispatch 到 ``run()`` / ``@operation`` 方法（ADR-010 D-03）。"""
        if backend is not None:
            self.backend = backend
        if mcp_manager is not None:
            self.mcp_manager = mcp_manager
        self._run_id = run_id
        return await self.run(input)

    async def run(self, input: dict) -> Any:
        """默认实现：按 ``operation_key`` 分发到 ``@operation`` 方法。

        无 operation 的工具直接覆写本方法。
        """
        value = input.get(self.operation_key)
        handler = self._operation_handlers.get(value) if value is not None else None
        if handler is None:
            raise KeyError(f"Unknown operation '{value}' for tool '{self.name}'")
        return await handler(self, input)

    # ── 生命周期（可选覆写）──────────────────────────────────────

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None


def make_invoker(tool: BaseTool) -> Callable[[dict], Any]:
    """生成经受信 ToolExecutor 调用的 invoker（ADR-010 D-03）。

    invoker 签名 ``(input)``，与旧 fn 一致；backend / run_id 从 contextvar
    读取（executor 调用前注入），无 backend 声明的工具忽略之。
    """

    async def invoker(input: dict) -> Any:
        from harness.tools.executor import current_run_id
        from harness.tools.mcp_call import get_manager

        mcp_manager = get_manager() if tool.needs_mcp_manager else None
        return await tool.invoke(
            input,
            backend=current_backend.get(),
            mcp_manager=mcp_manager,
            run_id=current_run_id.get(),
        )

    return invoker
