"""tests/v8_eval 共享夹具：手工 emit 合法事件流的 rig。

与 tests/v8/conftest 的最小 rig 同构（真 store + 真 fold + 真 Writer），
但不经 Runner 驱动——检查单元测试只需要**可折叠的合法事件流**，用
contracts 的 plan_to_dict 构造真实形状的 payload（不是手拼 dict）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from jagent.contracts import EventType, Plan, Step, plan_to_dict
from jagent.harness.fold import World
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer

SESSION_ID = "s-eval-test"

ENV = {"git_commit": "test", "config_hash": "hash-test"}


@dataclass
class EvalRig:
    root: Path
    store: EventStore
    writer: Writer

    def events(self) -> list:
        return self.store.read()

    # ---------------------------------------------------------------- 事件 --
    def emit(
        self,
        event_type: EventType,
        payload: dict,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> None:
        self.writer.emit(event_type, payload, run_id=run_id, step_id=step_id)

    # ---------------------------------------------------------------- 场景 --
    def completed_run(
        self,
        run_id: str = "r1",
        *,
        steps: tuple[str, ...] = ("s1",),
        gated_step: str | None = None,
    ) -> None:
        """一个走到 completed 的 run：started → frozen → 每步 running→passed
        （gated 步额外带审批三件套）→ verified pass → completed。"""
        plan = Plan(
            steps=tuple(
                Step(id=sid, tool="fs.read", gated=(sid == gated_step)) for sid in steps
            )
        )
        self.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": "execute", "env": ENV},
            run_id=run_id,
        )
        self.emit(
            EventType.PLAN_FROZEN,
            {"plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": 1},
            run_id=run_id,
        )
        for sid in steps:
            if sid == gated_step:
                self.emit(
                    EventType.APPROVAL_REQUESTED,
                    {
                        "step_id": sid,
                        "token": f"tok-{run_id}-{sid}",
                        "payload_hash": "p-hash",
                        "expires_at": 0.0,
                    },
                    run_id=run_id,
                    step_id=sid,
                )
                self.emit(
                    EventType.APPROVAL_RECEIVED,
                    {"token": f"tok-{run_id}-{sid}", "decision": "approve"},
                    run_id=run_id,
                    step_id=sid,
                )
                self.emit(
                    EventType.APPROVAL_RESOLVED,
                    {"token": f"tok-{run_id}-{sid}", "applied": True},
                    run_id=run_id,
                    step_id=sid,
                )
            self.emit(
                EventType.STEP_STATE_CHANGED,
                {"step_id": sid, "from": "pending", "to": "running", "reason": "start"},
                run_id=run_id,
                step_id=sid,
            )
            self.emit(
                EventType.STEP_STARTED,
                {"step_id": sid, "attempt": 1, "idempotency_key": f"{run_id}:{sid}"},
                run_id=run_id,
                step_id=sid,
            )
            self.emit(
                EventType.STEP_FINISHED,
                {"step_id": sid, "status": "ok", "artifact_refs": []},
                run_id=run_id,
                step_id=sid,
            )
            self.emit(
                EventType.STEP_VERIFIED,
                {"step_id": sid, "result": "pass", "checks": []},
                run_id=run_id,
                step_id=sid,
            )
            self.emit(
                EventType.STEP_STATE_CHANGED,
                {"step_id": sid, "from": "running", "to": "passed", "reason": "verified"},
                run_id=run_id,
                step_id=sid,
            )
        self.emit(
            EventType.REPORT_RENDERED, {"result": {"summary": "ok"}}, run_id=run_id
        )
        self.emit(
            EventType.RUN_COMPLETED, {"result": {"summary": "ok"}}, run_id=run_id
        )

    def failed_run(self, run_id: str = "r1", *, reason: str = "assert") -> None:
        self.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": "execute", "env": ENV},
            run_id=run_id,
        )
        self.emit(EventType.RUN_FAILED, {"reason": reason}, run_id=run_id)

    def cancelled_run(self, run_id: str = "r1", *, reason: str = "user") -> None:
        self.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": "execute", "env": ENV},
            run_id=run_id,
        )
        self.emit(EventType.RUN_CANCELLED, {"reason": reason}, run_id=run_id)

    def turn_end(self, outcome: str = "answered") -> None:
        self.emit(EventType.TURN_ENDED, {"outcome": outcome})

    def turn_received(self, text: str = "hi") -> None:
        self.emit(
            EventType.TURN_RECEIVED,
            {"text_hash": f"h-{abs(hash(text))}", "text": text},
        )

    def model_call(
        self,
        *,
        purpose: str = "planner",
        tokens_in: int = 100,
        tokens_out: int = 50,
    ) -> None:
        self.emit(
            EventType.MODEL_CALLED,
            {
                "purpose": purpose,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "duration_ms": 10.0,
            },
        )

    def clarify(self, *, round: int = 1) -> None:
        self.emit(
            EventType.CLARIFY_ASKED,
            {"round": round, "kind": "slot", "questions": []},
        )

    def advice_delivery(
        self,
        *,
        mode: str = "grounded",
        passed: bool = True,
        judge: str = "none",
        citations: int = 1,
        fallback: bool = False,
        violations: tuple[str, ...] = (),
        partial: bool = False,
    ) -> None:
        """建议路径收尾：gate.checked + advice.delivered（payload 形状对齐
        session.py 的 _gate_payload / ADVICE_DELIVERED emit 点）。"""
        self.emit(
            EventType.GATE_CHECKED,
            {
                "profile": "advice",
                "pass": passed,
                "violations": [
                    {"code": code, "rule_id": code, "detail": ""} for code in violations
                ],
                "ruleset": "advice.v8.2",
                "mode": mode,
                "partial": partial,
                "fallback": {"bool": fallback, "reason": "x" if fallback else ""},
                "citations": [
                    {"step_id": f"s{i}", "field": "ok", "label": f"c{i}", "seq": i}
                    for i in range(citations)
                ],
                "judge": {"verdict": judge, "reason": "", "model": "stub"},
            },
        )
        self.emit(
            EventType.ADVICE_DELIVERED,
            {
                "advice": {"body": "b", "refs": []},
                "mode": mode,
                "partial": partial,
                "fallback": {"bool": fallback, "reason": "x" if fallback else ""},
                "artifact_id": "a1",
                # 输出分级（I3）：投递事件必带 regime。与生产 `_advice_delivery` 同规则：
                # 不过闸 → verified（兜底模板）；过闸且有引用 → grounded；过闸无引用 → unverified。
                "delivery": {
                    "regime": (
                        "grounded" if passed and citations
                        else "unverified" if passed
                        else "verified"
                    ),
                    "checks": ["citations.grounded"] if passed and citations else [],
                    "grounds": [i for i in range(citations)],
                    "fallback": {"bool": fallback, "reason": "x" if fallback else ""},
                },
                "evidence": {
                    "run_id": None,
                    "citations": [
                        {"step_id": f"s{i}", "field": "ok", "label": f"c{i}", "seq": i}
                        for i in range(citations)
                    ],
                },
            },
        )


@pytest.fixture()
def rig(tmp_path: Path):
    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, World(), SESSION_ID)
    item = EvalRig(tmp_path, store, writer)
    yield item
    store.close()
