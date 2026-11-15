"""建议闸门测试的共享构造器（grounding v3 §3 / §6）。

这里只建 **公开契约**（Advice / Citation / EvidenceBundle / Plan），
不复制 gate 的内部逻辑；judge 一律用本地替身，测试不联网。
"""

from __future__ import annotations

from jagent.contracts import (
    Advice,
    Citation,
    EvidenceBundle,
    EvidenceItem,
    EvidenceReq,
    Plan,
    Step,
)
from jagent.harness.judge import JudgeVerdict


def item(
    step_id: str = "s1",
    observations: dict[str, str] | None = None,
    *,
    seq: int = 1,
    status: str = "ok",
    truncated: tuple[str, ...] = (),
    artifact_ids: tuple[str, ...] = (),
) -> EvidenceItem:
    return EvidenceItem(
        step_id=step_id,
        seq=seq,
        status=status,
        observations=dict(observations or {}),
        truncated=tuple(truncated),
        artifact_ids=tuple(artifact_ids),
    )


def bundle(*items: EvidenceItem, run_id: str = "r1", omitted_steps: int = 0) -> EvidenceBundle:
    return EvidenceBundle(run_id=run_id, items=tuple(items), omitted_steps=omitted_steps)


def req(step_id: str = "s1", field: str = "data.pe") -> EvidenceReq:
    return EvidenceReq(step_id=step_id, field=field)


def plan(
    *requires: EvidenceReq,
    step_ids: tuple[str, ...] = ("s1", "s2"),
    non_required: tuple[str, ...] = (),
    then: str | None = "advice",
) -> Plan:
    """``non_required`` 里的 step 用 ``Step(required=False)``；其余用真实默认 True。"""

    return Plan(
        steps=tuple(
            Step(id=step_id, tool="probe", required=step_id not in non_required)
            for step_id in step_ids
        ),
        then=then,
        evidence_requires=tuple(requires),
    )


def cite(
    step_id: str = "s1",
    field: str = "data.pe",
    *,
    label: str = "",
    seq: int | None = 1,
) -> Citation:
    return Citation(step_id=step_id, field=field, label=label, seq=seq)


def advice(
    body: str = "",
    *,
    refs: tuple[Citation, ...] = (),
    framework: tuple[str, ...] = (),
) -> Advice:
    return Advice(
        body=body,
        framework=framework,
        refs=refs,
        disclaimer="以上内容仅为信息整理与通用决策框架，不构成具体行动指令。",
    )


class RecordingJudge:
    """返回固定结论，并记录每次请求（用来断言"T1 失败时不调用 judge"）。"""

    def __init__(self, outcome: JudgeVerdict | None = None) -> None:
        self.outcome = outcome
        self.calls: list[object] = []
        self.model = "fake-judge"

    def judge(self, request: object) -> JudgeVerdict | None:
        self.calls.append(request)
        return self.outcome


class RejectJudge(RecordingJudge):
    def __init__(self) -> None:
        super().__init__(JudgeVerdict.REJECT)


class UndecidableJudge(RecordingJudge):
    def __init__(self) -> None:
        super().__init__(JudgeVerdict.UNDECIDABLE)


class NoOpinionJudge(RecordingJudge):
    def __init__(self) -> None:
        super().__init__(None)


class UnavailableJudge:
    """judge 调不通：调用即抛，模拟网络/服务不可用。"""

    model = "fake-unavailable"

    def __init__(self) -> None:
        self.calls = 0

    def judge(self, request: object) -> None:
        self.calls += 1
        raise RuntimeError("judge service unavailable")
