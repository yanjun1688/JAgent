"""JAgent v8 · 唯一写入口。

任何状态变更都必须经过这里：它做三件事——
1. 校验转移（非法转移写一条 `illegal: true` 事件后抛错，拒绝本身也是事实）；
2. 写事件（append-only）；
3. 把事件喂给内存里的 World，保证读到的永远是刚写下的东西。

run 侧的拒绝走 `run.transition_rejected`（RT-04b）：不新增“假状态”，也不复用
`step.state_changed`（那会把 step 语义塞进 run）。折叠层对未知事件是宽容的
（只记 diagnostic），所以这条事件只留痕、不改投影——详见 fold.apply_event。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from ..contracts import Event, EventDraft, EventType
from .fold import World, apply_event
from .statemachine import RUN_TERMINAL, IllegalTransitionError, ensure_run, ensure_step
from .store import EventStore

#: 会改变 `run.status` 的事件 → 它想让 run 去的状态（RT-04c）。
#: 这张表只用来维护「同一批里前一条落完之后 run 在哪」；终态守卫本身不看它，
#: 而是拒绝**任何**带终态 run_id 的事件（I1：终态不可改写，见 emit_many）。
_RUN_STATE_TARGETS: dict[EventType, str] = {
    EventType.RUN_STARTED: "planning",
    EventType.PLAN_FROZEN: "running",
    EventType.APPROVAL_REQUESTED: "waiting_human",
    EventType.APPROVAL_RECEIVED: "running",
    EventType.RUN_SUSPENDED: "waiting_human",
    EventType.RUN_RESUMED: "running",
    EventType.RUN_COMPLETED: "completed",
    EventType.RUN_FAILED: "failed",
    EventType.RUN_CANCELLED: "cancelled",
}


class Writer:
    def __init__(self, store: EventStore, world: World, session_id: str, *, clock=time.time) -> None:
        self.store = store
        self.world = world
        self.session_id = session_id
        self._clock = clock

    # ---------------------------------------------------------------- basics --
    def emit(self, type_: EventType, payload: dict[str, Any], *, run_id: str | None = None,
             step_id: str | None = None) -> Event:
        # 终态保护统一在 emit_many 里做：拒绝时写一条 run.transition_rejected 代替原事件，
        # 所以这里拿到的是“实际写下的事实”（拒绝本身也是事实），不是被拒的草稿。
        return self.emit_many([EventDraft(type_, payload, self.session_id, run_id, step_id)])[0]

    def emit_many(self, drafts: Sequence[EventDraft]) -> list[Event]:
        # I1：终态是吸收态。run 一旦 completed/failed/cancelled，任何带这个 run_id
        # 的事件都拒绝——不只是会改 run.status 的 8 种 run 状态事件：`step.finished`
        # 能清空 artifact_refs / 追加 evidence_seqs，`plan.amended` 能把 passed 改回
        # pending，`artifact.written` 能往终态 run 里塞产物（BUG2-01）。
        # 拒绝本身是事实：写一条 run.transition_rejected，丢弃原事件，不静默吞掉。
        # 用 projected 而不是 world 直接判断，保证同一批里 [run.completed, step.finished]
        # 的第二条也按“前一条落完之后”的状态来判断。
        appended = self._apply_terminal_guard(drafts)
        events = self.store.append(appended)
        # D-045：写投影与 HTTP 线程池的读互斥；一次 emit 是一批原子投影。
        with self.world.lock:
            for event in events:
                apply_event(self.world, event)
        return events

    def _apply_terminal_guard(self, drafts: Sequence[EventDraft]) -> list[EventDraft]:
        """终态吸收 + 非终态合法性校验：两种拒绝都写成 `run.transition_rejected`。"""
        projected: dict[str, str] = {
            run_id: run.status for run_id, run in self.world.runs.items()
        }
        accepted: list[EventDraft] = []
        for draft in drafts:
            target = _RUN_STATE_TARGETS.get(draft.type)
            current = projected.get(draft.run_id) if draft.run_id is not None else None
            if draft.type is not EventType.RUN_TRANSITION_REJECTED and current is not None:
                if current in RUN_TERMINAL:
                    # 吸收态：终态 run 上的任何事件（含无 target 的 step 级事件）都拒绝。
                    accepted.append(
                        self._rejected_draft(draft, current, target or current, absorbing=True)
                    )
                    continue
                if target is not None:
                    # BUG2-02：非终态的 run 状态事件同样过合法性校验——
                    # running→planning 的倒退/重复不再静默改写投影。
                    try:
                        ensure_run(current, target)
                    except IllegalTransitionError:
                        accepted.append(
                            self._rejected_draft(draft, current, target, absorbing=False)
                        )
                        continue
            accepted.append(draft)
            if target is not None and draft.run_id is not None:
                projected[draft.run_id] = target
        return accepted

    @staticmethod
    def _rejected_draft(
        draft: EventDraft, current: str, to: str, *, absorbing: bool
    ) -> EventDraft:
        """把被拒的草稿替换成一条 `run.transition_rejected`（拒绝本身也是事实）。"""
        return EventDraft(
            EventType.RUN_TRANSITION_REJECTED,
            {
                "from": current,
                "to": to,
                "reason": (
                    f"run is already {current}"
                    if absorbing
                    else f"illegal run transition {current} -> {to}"
                ),
                "illegal": True,
                # 被拒的原事件类型：拒绝事件必须能独立回答“谁想改什么”。
                "rejected_type": draft.type.value,
                # current：`to` 对非 run 状态事件没有意义，读这个字段。
                "target_status": current,
            },
            draft.session_id,
            draft.run_id,
            draft.step_id,
        )

    # ------------------------------------------------------------ transitions --
    def step_to(self, run_id: str, step_id: str, to: str, reason: str) -> Event:
        run = self.world.runs[run_id]
        current = run.steps[step_id].status if step_id in run.steps else "pending"
        try:
            ensure_step(current, to)
        except IllegalTransitionError:
            # 拒绝也要留痕，然后再抛
            self.emit(
                EventType.STEP_STATE_CHANGED,
                {"step_id": step_id, "from": current, "to": to, "reason": reason, "illegal": True},
                run_id=run_id, step_id=step_id,
            )
            raise
        return self.emit(
            EventType.STEP_STATE_CHANGED,
            {"step_id": step_id, "from": current, "to": to, "reason": reason},
            run_id=run_id, step_id=step_id,
        )

    def run_to(self, run_id: str, to: str, reason: str = "illegal_run_transition") -> None:
        run = self.world.runs[run_id]
        try:
            ensure_run(run.status, to)
        except IllegalTransitionError:
            # RT-04b：run 的拒绝也要留痕（§2「拒绝本身也落事件」），然后再抛。
            self._record_rejected_transition(run_id, run.status, to, reason, step_id=None)
            raise

    def _record_rejected_transition(
        self, run_id: str, from_: str, to: str, reason: str, *, step_id: str | None
    ) -> Event:
        """写一条 `run.transition_rejected`：拒绝是事实，但投影一点都不能动。"""
        return self.emit(
            EventType.RUN_TRANSITION_REJECTED,
            {"from": from_, "to": to, "reason": reason, "illegal": True},
            run_id=run_id,
            step_id=step_id,
        )

    # -------------------------------------------------------------- helpers --
    def budget(self, run_id: str, kind: str, delta: float = 1.0) -> None:
        run = self.world.runs[run_id]
        total = float(run.budget.get(kind, 0.0)) + float(delta)
        self.emit(
            EventType.BUDGET_CONSUMED,
            {"kind": kind, "delta": float(delta), "total": total},
            run_id=run_id,
        )
