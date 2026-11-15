"""缺口 3 · 步骤结束指纹（STATUS 缺口表 #3：写文件的步骤每 epoch 多失效一次）。

G-05 的整树指纹以**冻结时**为基准：一个 ``fs.write`` 步骤通过后，workspace
的树变了（它自己的产物），恢复对账时 frozen≠current → 它被自己的写失效一次。
修复：步骤**通过时**记一次整树指纹（新事件 ``step.fingerprinted``，落在
``StepState.end_fp``）；恢复对账时「世界在它结束之后没变」就不算受害者——
没有结束指纹的旧日志退回整树判据（不能证明没变，就不复用结论）。

事件词表新增 → 四处同步（contracts/fold/invariants/SPEC_VOCAB + 架构事件表），
台账记 D-029。
"""

from __future__ import annotations

import dataclasses

from jagent.contracts import Budget, EventType, Plan, Step, ToolResult, Verify

from .conftest import Rig


def _counting_fs_write(rig: Rig) -> list[str]:
    """覆盖内置 fs.write：同 spec（快照哈希不变），执行换成测试自己的写 + 计数。"""
    calls: list[str] = []
    spec = rig.registry.get("fs.write")

    def counting(args, workspace, timeout):
        path = str(args.get("path", ""))
        calls.append(path)
        (workspace / path).write_text(str(args.get("content", "")), encoding="utf-8")
        return ToolResult(ok=True)

    rig.registry.add(dataclasses.replace(spec), counting)
    return calls


def _write_then_gate_plan() -> Plan:
    return Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "out.txt", "content": "x"},
                verify=Verify("ok", needs=("ok",)),
            ),
            Step(
                id="gate",
                tool="fs.write",
                args={"path": "gate.txt", "content": "y"},
                verify=Verify("ok", needs=("ok",)),
                gated=True,
            ),
        ),
    )


def test_writer_step_is_not_invalidated_by_its_own_write(rig: Rig) -> None:
    """Given write 步骤通过（workspace 树因此变化）、gated 步骤挂起，
    When 批准恢复，Then write 步骤不被自己的写失效（零重跑、零 step.invalidated），
    gated 步骤正常执行完成。"""
    calls = _counting_fs_write(rig)
    runner = rig.runner(budget=Budget(auto_approve_tools=("fs.write",)))

    first = runner.start_and_run(_write_then_gate_plan())
    assert first.status == "waiting_human"
    assert calls == ["out.txt"], f"前置失败：{calls}"

    pending = rig.human.pending(first.run_id)
    assert pending is not None
    assert rig.human.resolve(first.run_id, pending["token"], "approve") is True
    resumed = runner.resume(first.run_id, approved_steps=frozenset({"gate"}))

    assert resumed.status == "completed", f"恢复后没能完成：{resumed.reason}"
    assert calls == ["out.txt", "gate.txt"], (
        f"写步骤被自己的写多失效了一次：{calls}"
    )
    invalidated = [
        e.payload
        for e in rig.events()
        if e.type is EventType.STEP_INVALIDATED and e.payload.get("step_id") == "write"
    ]
    assert invalidated == [], "write 步骤不应被自己的写作废"

    # 新事件落盘：每个 passed 步骤都有结束指纹，重放可恢复。
    fingerprints = [
        e.payload
        for e in rig.events()
        if e.type is EventType.STEP_FINGERPRINTED
    ]
    assert {f["step_id"] for f in fingerprints} == {"write", "gate"}
    assert all(f["fp_hash"] for f in fingerprints)


def test_unrelated_change_still_invalidates_coupled_steps(rig: Rig) -> None:
    """Given 写步骤通过后、恢复之前，**别的文件**被外部改动，
    When 恢复，Then 耦合步骤仍然失效重跑——精确判据不放过真变化。"""
    calls = _counting_fs_write(rig)
    runner = rig.runner(budget=Budget(auto_approve_tools=("fs.write",)))

    plan = Plan(
        steps=(
            Step(
                id="write",
                tool="fs.write",
                args={"path": "out.txt", "content": "x"},
                verify=Verify("ok", needs=("ok",)),
            ),
            Step(
                id="gate",
                tool="fs.write",
                args={"path": "gate.txt", "content": "y"},
                verify=Verify("ok", needs=("ok",)),
                gated=True,
            ),
        ),
    )
    first = runner.start_and_run(plan)
    assert first.status == "waiting_human"

    # 外部真实改动：不是 write 步骤的产物。
    (rig.ws / "external.txt").write_text("changed", encoding="utf-8")

    pending = rig.human.pending(first.run_id)
    assert rig.human.resolve(first.run_id, pending["token"], "approve") is True
    resumed = runner.resume(first.run_id, approved_steps=frozenset({"gate"}))

    assert resumed.status == "completed"
    assert calls.count("out.txt") == 2, "世界在步骤结束之后变了：必须重跑"
    invalidated = [
        e.payload
        for e in rig.events()
        if e.type is EventType.STEP_INVALIDATED and e.payload.get("step_id") == "write"
    ]
    assert invalidated, "真实变化必须留作废痕迹"
