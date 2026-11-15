"""证据要求对账（中立模块，纯函数）。

依赖方向：``agent/ -> evidence <- gate/``。这里只做差集，不判定证据质量、
不调用模型、不写事件。Propose 的输入与 gate 的 ``evidence_incomplete``
都必须调用本模块，禁止各写一份（grounding §2.4 / D-002）。
"""

from __future__ import annotations

from .contracts import EvidenceBundle, EvidenceReq, Plan


def missing(plan: Plan, evidence: EvidenceBundle | None) -> tuple[EvidenceReq, ...]:
    """返回 ``plan.evidence_requires`` 里尚未满足的项，保持计划中的声明顺序。

    满足 = ``evidence`` 中存在同一个 ``step_id``，且 ``observations`` 里有这个
    ``field``、值非空。空值按缺处理，与 ``citation_unresolved`` 的“解析不到或
    为空”口径一致；``evidence is None`` 表示一条都没拿到，全部算缺。
    """

    if evidence is None:
        return plan.evidence_requires

    present = {
        (item.step_id, field)
        for item in evidence.items
        for field, value in item.observations.items()
        if value != ""
    }
    return tuple(
        req
        for req in plan.evidence_requires
        if (req.step_id, req.field) not in present
    )
