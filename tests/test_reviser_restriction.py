"""S08 — Reviser 限权（不可变目标字段强制）测试。

覆盖 D-01/D-02/D-05（契约不可变）、C-01（契约来源）、问题三（Reviser 权限过大）、
Q-06（mutating 覆盖只认 DeliveryContract，不认 declared_operations 自报）。
受信校验在 Scheduler 侧强制（validate_revision_invariants），不依赖 Reviser 自觉。
"""

from __future__ import annotations


from harness.core.planner import validate_revision_invariants
from harness.models.intent import DeliveryContract, DeliverySource
from harness.models.plan import DagPlan, DagStep
from harness.models.tools import OperationContract, SideEffect, ToolDefinition
from harness.tools.registry import ToolRegistry


def _caller_contract(**input_kwargs) -> DeliveryContract:
    return DeliveryContract(tool="file_op", input=input_kwargs, source=DeliverySource.CALLER)


def _extracted_contract(**input_kwargs) -> DeliveryContract:
    return DeliveryContract(tool="file_op", input=input_kwargs, source=DeliverySource.EXTRACTED)


def _registry():
    r = ToolRegistry()
    r._register(
        ToolDefinition(
            name="file_op",
            description="f",
            input_schema={
                "type": "object",
                "properties": {"operation": {"type": "string"}, "path": {"type": "string"}},
            },
            side_effects=[SideEffect.WRITE],
            operations=[
                OperationContract(operation="write", side_effects=[SideEffect.WRITE]),
                OperationContract(operation="read", side_effects=[]),
                OperationContract(operation="list", side_effects=[]),
            ],
        ),
        lambda x: {"success": True},
    )
    return r


def _plan(*steps: DagStep) -> DagPlan:
    return DagPlan(intent="t", user_intent="write and read blackbox.txt", steps=list(steps))


# ── 契约不可变：正向覆盖 ─────────────────────────────────────────


def test_revision_removing_write_contract_rejected():
    contracts = [_caller_contract(operation="write", path="blackbox.txt")]
    revised = _plan(DagStep(id="s1", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}))
    errors = validate_revision_invariants(contracts, "raw", revised, registry=_registry())
    assert any("removed required operation" in e for e in errors)


def test_revision_changing_path_rejected():
    contracts = [_caller_contract(operation="write", path="blackbox.txt")]
    revised = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "other.txt"}))
    errors = validate_revision_invariants(contracts, "raw", revised, registry=_registry())
    assert any("removed required operation" in e for e in errors) or any("changed" in e for e in errors)


def test_read_replaced_by_list_not_satisfied():
    contracts = [_caller_contract(operation="read", path="blackbox.txt")]
    revised = _plan(DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."}))
    errors = validate_revision_invariants(contracts, "raw", revised, registry=_registry())
    assert any("removed required operation" in e for e in errors)


# ── 合法修订不误杀 ───────────────────────────────────────────────


def test_legitimate_self_heal_passes():
    """read 失败后新增 list 定位再 read — 契约 read 最终达成 → 通过。"""
    contracts = [_caller_contract(operation="read", path="blackbox.txt")]
    revised = _plan(
        DagStep(id="s2", tool="file_op", input={"operation": "list", "path": "."}),
        DagStep(id="s3", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}, depends_on=["s2"]),
    )
    assert validate_revision_invariants(contracts, "raw", revised, registry=_registry()) == []


def test_contract_met_unchanged_passes():
    contracts = [_caller_contract(operation="write", path="blackbox.txt")]
    revised = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}))
    assert validate_revision_invariants(contracts, "raw", revised, registry=_registry()) == []


def test_extracted_contract_also_positive_covered():
    contracts = [_extracted_contract(operation="write", path="blackbox.txt")]
    revised = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}))
    assert validate_revision_invariants(contracts, "raw", revised, registry=_registry()) == []


# ── C-02 反向覆盖：未声明的 mutating 操作 ────────────────────────


def test_uncovered_mutating_step_rejected():
    contracts = [_caller_contract(operation="write", path="blackbox.txt")]
    revised = _plan(
        DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}),
        DagStep(id="s2", tool="file_op", input={"operation": "write", "path": "secret.txt"}),
    )
    errors = validate_revision_invariants(contracts, "raw", revised, registry=_registry())
    assert any("un-declared mutating step" in e for e in errors)


def test_mutating_step_not_authorized_by_declared_operations():
    """Q-06: 自报 declared_operations 不再授权新副作用 —— 只认 DeliveryContract 覆盖。

    Reviser 在 declared_operations 自报 write secret.txt，但无契约覆盖 → 被拒
    （堵 self-authorize 漏洞）。
    """
    contracts = [_caller_contract(operation="write", path="blackbox.txt")]
    revised = _plan(
        DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}),
        DagStep(id="s2", tool="file_op", input={"operation": "write", "path": "secret.txt"}),
    )
    from harness.models.plan import RequiredOperation

    revised.declared_operations = [
        RequiredOperation(tool="file_op", input={"operation": "write", "path": "secret.txt"})
    ]
    errors = validate_revision_invariants(contracts, "raw", revised, registry=_registry())
    assert any("un-declared mutating step" in e for e in errors)


def test_mutating_step_covered_by_contract_passes():
    """Q-06 正向: 新增 mutating step 被 DeliveryContract 覆盖时才被允许。"""
    contracts = [
        _caller_contract(operation="write", path="blackbox.txt"),
        _caller_contract(operation="write", path="secret.txt"),
    ]
    revised = _plan(
        DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "blackbox.txt"}),
        DagStep(id="s2", tool="file_op", input={"operation": "write", "path": "secret.txt"}),
    )
    assert validate_revision_invariants(contracts, "raw", revised, registry=_registry()) == []


def test_read_only_helper_step_not_flagged():
    contracts = [_caller_contract(operation="read", path="blackbox.txt")]
    revised = _plan(
        DagStep(id="s1", tool="file_op", input={"operation": "list", "path": "."}),
        DagStep(id="s2", tool="file_op", input={"operation": "read", "path": "blackbox.txt"}, depends_on=["s1"]),
    )
    assert validate_revision_invariants(contracts, "raw", revised, registry=_registry()) == []


def test_no_contracts_skips_reverse_coverage():
    revised = _plan(DagStep(id="s1", tool="file_op", input={"operation": "write", "path": "x.txt"}))
    assert validate_revision_invariants([], "raw", revised, registry=_registry()) == []


# ── 意图不可变 ───────────────────────────────────────────────────


def test_intent_immutability_handled_by_s05():
    """原始 intent 不可变由 S05 在 RunStarted 事件层保证（intent_raw），
    计划内 user_intent 仅是审计重述 — 不变量函数不因此拒绝。"""
    revised = DagPlan(intent="t", user_intent="", steps=[DagStep(id="s1", tool="file_op", input={})])
    assert validate_revision_invariants([], "raw", revised, registry=_registry()) == []
