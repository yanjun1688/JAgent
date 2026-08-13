"""Delivery contract models (S05) — C-01 收敛后的单一交付契约。

把"用户到底要求了什么"从自由文本升级为受信契约。契约来源只有两个入口
（C-06）：调用方 API 显式提交（``caller``）或系统从 intent 抽取（``extracted``）。
来源只用于审计与降级，不参与受信判定（C-01）。
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class DeliverySource(str, Enum):
    CALLER = "caller"
    EXTRACTED = "extracted"


class DeliveryOperationInput(BaseModel):
    """Public request shape shared by Run and Conversation APIs."""

    tool: str
    input: dict[str, Any] = Field(default_factory=dict)


class DeliveryContract(BaseModel):
    """单一交付契约（C-01 收敛 `RequiredOperation` 与旧 `DeliveryContract` 概念）。

    ``input`` 存底判定性键值（operation/path/content 等）。D-03：content 存底但
    不参与匹配。Q-05：``after`` 字段已移除 —— 契约不再承载时序职责，时序归
    ``DagStep.depends_on``（ADR-009 Q-03/Q-05）。历史事件中残留的 ``after`` 由
    Pydantic 忽略未知字段处理，无需数据迁移。
    """

    contract_id: str = ""
    source: DeliverySource = DeliverySource.EXTRACTED
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ensure_contract_id(self) -> "DeliveryContract":
        if not self.contract_id:
            raw = json.dumps({"tool": self.tool, "input": self.input}, sort_keys=True, ensure_ascii=False)
            self.contract_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return self


def validate_delivery_contract_input(tool: str, op_input: Any, tool_def: Any) -> list[str]:
    """Trusted validation shared by API callers and the LLM extractor.

    ADR-010 D-04 (§7.3): contract-driven required keys —
      required = input_schema.required ∪ (operation_key if the tool declares
      per-operation contracts) ∪ 命中 OperationContract.required_input.
    The operation discriminant key is only mandatory for tools that declare
    operations (mcp_call has none → tool_name only).
    """
    if tool_def is None:
        return [f"unknown tool '{tool}'"]
    if not isinstance(op_input, dict) or not op_input:
        return ["input must be a non-empty object"]

    errors: list[str] = []
    required = list(tool_def.input_schema.get("required", ()))
    if tool_def.operations:
        required.append(tool_def.operation_key)
    op_contract = tool_def.resolve_operation(op_input)
    if op_contract is not None:
        for field in op_contract.required_input:
            if field not in required:
                required.append(field)
    for field in required:
        if field not in op_input or op_input[field] in (None, ""):
            errors.append(f"{tool}.input.{field} is required")
    properties = tool_def.input_schema.get("properties", {})
    for field, definition in properties.items():
        if field not in op_input:
            continue
        value = op_input[field]
        if "enum" in definition and value not in definition["enum"]:
            errors.append(f"{tool}.input.{field} must be one of {definition['enum']}")
        if definition.get("type") == "string" and not isinstance(value, str):
            errors.append(f"{tool}.input.{field} must be a string")
        if definition.get("type") == "object" and not isinstance(value, dict):
            errors.append(f"{tool}.input.{field} must be an object")
    if (
        tool_def.name == "file_op"
        and op_contract is not None
        and op_contract.operation in {"write", "append"}
        and "content" in op_input
        and not isinstance(op_input["content"], str)
    ):
        errors.append("file_op.input.content must be a string")
    if errors:
        return errors
    if tool_def.operations:
        operation = op_input.get(tool_def.operation_key)
        if not any(op.operation == operation for op in tool_def.operations):
            errors.append(f"{tool} has no declared operation '{operation}'")
    return errors


class UserIntent(BaseModel):
    """用户原始意图（受信不可变）。

    ``raw`` 是原始用户请求，Planner/Reviser 的 intent/user_intent 只是 LLM
    重述，不得写回 ``raw``。``contracts`` 为该系统运行要强制的交付契约。
    """

    raw: str
    contracts: list[DeliveryContract] = Field(default_factory=list)
    source_note: str = ""
