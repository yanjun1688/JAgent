"""v8 验收（grounding §6）：只走 Session，不碰 gate/harness 的内部判定。"""

from __future__ import annotations

from pathlib import Path

from jagent.contracts import Advice, Citation
from jagent.harness.judge import JudgeVerdict
from jagent.ports import ModelReply


class FakeJudge:
    """可返回 REJECT 的 judge 替身；默认 UNDECIDABLE（放行但标记）。"""

    model = "fake-judge"

    def __init__(self, outcome=None) -> None:
        self.outcome = JudgeVerdict.UNDECIDABLE if outcome is None else outcome
        self.requests: list[object] = []

    def judge(self, request: object):
        self.requests.append(request)
        return self.outcome


class RejectOpinion:
    """带理由的 REJECT：gate 会把它写进 gate.checked.judge.reason。"""

    value = "reject"

    def __init__(self, reason: str) -> None:
        self.reason = reason


class FakePropose:
    """记录 compose 入参并返回固定建议；不联网。"""

    def __init__(self, advice: Advice) -> None:
        self.advice = advice
        self.calls: list[dict] = []

    def compose(self, **kwargs) -> Advice:
        self.calls.append(kwargs)
        return self.advice


def advice_from_payload(payload: dict) -> Advice:
    """把 advice.delivered 里的 dict 还原成 Advice（重放时比较文本用）。"""
    return Advice(
        info=tuple(payload.get("info") or ()),
        risks=tuple(payload.get("risks") or ()),
        framework=tuple(payload.get("framework") or ()),
        refs=tuple(
            Citation(
                step_id=ref["step_id"],
                field=ref["field"],
                label=ref.get("label", ""),
                seq=ref.get("seq"),
            )
            for ref in (payload.get("refs") or ())
        ),
        sources=tuple(payload.get("sources") or ()),
        disclaimer=payload.get("disclaimer", ""),
        body=payload.get("body", ""),
    )


def empty_model():
    """一个计数模型：这些验收用例不应该调用它。"""
    from jagent.agent.model import CountingModel, ScriptedModel

    return CountingModel(ScriptedModel([]))


def write_doc(app) -> None:
    (Path(app.root) / "ws" / "doc.md").write_text("hello\n", encoding="utf-8")


__all__ = [
    "FakeJudge",
    "FakePropose",
    "ModelReply",
    "RejectOpinion",
    "advice_from_payload",
    "empty_model",
    "write_doc",
]
