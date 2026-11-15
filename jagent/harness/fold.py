"""JAgent v8 · 状态投影（fold）。

纯函数：`fold(events) -> World`。不读时钟、不读文件、不调模型。
同一事件流重放两次必须得到同一个 World（`World.fingerprint()` 可直接断言）。

折叠对非法转移是**宽容**的（记 diagnostic，不抛）；拒绝发生在写入侧（state machine）。
这条分工来自旧内核的教训：历史不该被后来的规则改写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..contracts import Event, EventType, Plan, Slot, plan_from_dict
from .statemachine import RUN_TERMINAL


@dataclass
class StepState:
    id: str
    status: str = "pending"
    attempts: int = 0
    idempotency_key: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    result_status: str | None = None
    failure_class: str | None = None
    evidence_seqs: list[int] = field(default_factory=list)
    invalidated_by: str | None = None
    #: 人拒绝过这一步的审批（RT-09/§4.12）：runner 遇到「已拒的必需步骤」
    #  直接 run.failed，遇到「已拒的非必需步骤」剪枝跳过——绝不重新发 token。
    rejected_by_human: bool = False
    #: 人**批准时**那一次 payload 的摘要（`approval.requested.payload_hash`）。
    #: 执行前要拿它和当前 args 重算的摘要比对：不一致就是「批准的是 A、执行的是 B」。
    approved_hash: str | None = None
    #: 最后一次 `step.started` / `step.finished` / `step.verified` 的 seq。用序号而不是布尔，
    #: 是因为同一步可以「开始→结束→又开始」：只有比较序号才知道谁在谁之后。
    #: - `finished_seq <= started_seq` → 开始了没结束 = in-doubt（不知道做没做）；
    #: - `verified_seq < finished_seq` → 跑完了没判定 = 只差一次 Verifier；
    #: - `verified_seq > finished_seq > started_seq` → 判定完了没改状态 = 只差一次状态转移。
    started_seq: int = 0
    finished_seq: int = 0
    verified_seq: int = 0
    #: 最后一次 `step.verified` 的结论（"pass" / "fail"）。恢复时不必再算一遍断言。
    verified_result: str = ""
    verified_reason: str = ""
    #: 步骤**通过时**的整树指纹（缺口 3，`step.fingerprinted`）：恢复对账时
    #「世界在它结束之后没变」就不算受害者——写文件的步骤不再被自己的写多失效。
    end_fp: str = ""


@dataclass
class RunState:
    run_id: str
    kind: str
    status: str = "planning"
    plan: Plan | None = None
    plan_hash: str = ""
    version: int = 1
    steps: dict[str, StepState] = field(default_factory=dict)
    budget: dict[str, float] = field(default_factory=lambda: {
        "steps": 0.0, "transient": 0.0, "repairs": 0.0, "wall_s": 0.0,
    })
    approval: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    fail_reason: str = ""
    #: 取消原因（run.cancelled.reason）。取消的 run 没有结论可给：result 恒为空，
    #: 用户在会话流里收到的只是 reply 通知（D-028）。
    cancel_reason: str = ""
    last_seq: int = 0
    #: 冻结时采的环境指纹（G-05）。恢复时重采，和它比对得出「变没变」。
    env_fp: str = ""
    #: 最近一次恢复对账的结论（只记录，不在这里判定谁重跑）。
    env_changed: bool = False
    #: run epoch：冻结是 0，每采一次恢复指纹 +1。事件里带 epoch，日志能按「第几次恢复」切开。
    epoch: int = 0
    #: 冻结时逐步骤记下的 `step.env_probes` 指纹：`{step_id: {声明路径: sha256}}`。
    #: 恢复时逐项比，才能只失效「自己探测的文件真变了」的那些步骤。
    probe_fps: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 冻结时的工具契约快照（缺口 4）：派发前对当前 registry 复核，不符即拒。
    tool_snapshot: str = ""

    def step_list(self) -> list[StepState]:
        return list(self.steps.values())


@dataclass
class SessionState:
    session_id: str
    turns: list[str] = field(default_factory=list)
    slots: dict[str, Slot] = field(default_factory=dict)
    #: planner 现场澄清问过的槽位白名单（BUG2-04）：随 CLARIFY_ASKED 持久，
    #: 重启后用户的回答才不会被静默丢弃。
    dynamic_slots: set[str] = field(default_factory=set)
    #: 对话历史（D-030 L1）：`{"role": "user"/"assistant", "text": 原文}`。
    #: user 来自 TURN_RECEIVED.text，assistant 来自 advice.delivered 与
    #: report.rendered——数据已在事件日志里，这里只是接线，零新存储。
    history: list[dict[str, str]] = field(default_factory=list)
    #: 待确认的长期记忆条目（D-030 L2）：`memory.proposed` 写入、
    #: `memory.resolved` 清空——随事件持久，重启后确认流仍然成立。
    pending_memory: list[dict[str, str]] = field(default_factory=list)
    #: 模型调用观测（D-031 S1）：每次 `model.called` 的载荷，token 合计随行。
    #: S2 metrics 的会话级成本/效率指标从这里派生。
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    clarify_round: int = 0
    advice_rejects: int = 0
    runs: list[str] = field(default_factory=list)
    last_intent: dict[str, Any] | None = None
    last_outcome: str = ""
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class World:
    sessions: dict[str, SessionState] = field(default_factory=dict)
    runs: dict[str, RunState] = field(default_factory=dict)
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    seq: int = 0
    digest: str = ""
    diagnostics: list[str] = field(default_factory=list)

    def session(self, session_id: str) -> SessionState:
        if session_id not in self.sessions:
            self.sessions[session_id] = SessionState(session_id=session_id)
        return self.sessions[session_id]

    def run(self, run_id: str) -> RunState:
        return self.runs[run_id]

    def fingerprint(self) -> str:
        """重放等价断言用：不变量 + 每个 run 的关键字段。"""
        import json

        def step_fp(s: StepState) -> Any:
            return [s.id, s.status, s.attempts, s.artifact_refs, s.checks, s.error_code, s.evidence_seqs]

        body = {
            "runs": {
                rid: {
                    "status": r.status, "kind": r.kind, "plan_hash": r.plan_hash,
                    "budget": r.budget,
                    "steps": {sid: step_fp(s) for sid, s in sorted(r.steps.items())},
                    "result": r.result,
                }
                for rid, r in sorted(self.runs.items())
            },
            "sessions": {
                sid: {
                    "slots": {k: [v.value, v.source] for k, v in sorted(s.slots.items())},
                    "clarify_round": s.clarify_round,
                    "advice_rejects": s.advice_rejects,
                    "last_outcome": s.last_outcome,
                }
                for sid, s in sorted(self.sessions.items())
            },
        }
        return json.dumps(body, sort_keys=True, ensure_ascii=False)


def fold(events: Iterable[Event]) -> World:
    world = World()
    for event in events:
        apply_event(world, event)
    return world


def apply_event(world: World, event: Event) -> None:
    world.seq = max(world.seq, event.seq)
    world.digest = event.digest
    if _after_terminal(world, event):
        # 双保险：写侧已经拒绝并改写成 run.transition_rejected；这里兜住**历史日志**
        # （BUG2-01 之前写下的“完成后又被 step.finished / plan.amended 改写”的流）。
        # 重放必须得到未被改写的投影，否则审计无法通过重放发现事后改写。
        world.diagnostics.append(f"ignored {event.type.value} on terminal run {event.run_id}")
        return
    handler = _HANDLERS.get(event.type)
    if handler is None:
        world.diagnostics.append(f"unknown event type {event.type}")
        return
    handler(world, event)


def _after_terminal(world: World, event: Event) -> bool:
    """这条事件是不是落在终态 run 上（因而不许改投影）。

    `run.transition_rejected` 是诊断事件：它必须能重放（否则 BUG2-03 会记
    `unknown event type`），也绝不能改投影，所以单独放行。
    """
    if event.type is EventType.RUN_TRANSITION_REJECTED:
        return False
    run = world.runs.get(event.run_id or "")
    return run is not None and run.status in RUN_TERMINAL


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #

def _session(world: World, event: Event) -> SessionState:
    return world.session(event.session_id)


def _run(world: World, event: Event) -> RunState | None:
    return world.runs.get(event.run_id or "")


def _turn_received(world: World, event: Event) -> None:
    state = _session(world, event)
    state.turns.append(event.payload["text_hash"])
    # D-030 L1：user 轮原文进历史（事件载荷本来就有，零新存储）。
    state.history.append({"role": "user", "text": str(event.payload.get("text") or "")})


def _intent_routed(world: World, event: Event) -> None:
    s = _session(world, event)
    s.last_intent = {
        "type": event.payload["type"],
        "confidence": event.payload["confidence"],
        "missing_params": list(event.payload.get("missing_params") or ()),
        "phase": event.payload.get("phase", "initial"),
        "single_step": event.payload.get("single_step", False),
    }


def _intent_confirmed(world: World, event: Event) -> None:
    s = _session(world, event)
    if s.last_intent is not None:
        s.last_intent["type"] = event.payload["type"]
        s.last_intent["confirmed"] = True


def _clarify_asked(world: World, event: Event) -> None:
    state = _session(world, event)
    state.clarify_round = max(state.clarify_round, int(event.payload["round"]))
    # BUG2-04：白名单随事件持久——CLARIFY_ASKED 载荷里本来就有 questions[].slot。
    for question in event.payload.get("questions") or ():
        slot = str(question.get("slot") or "")
        if slot:
            state.dynamic_slots.add(slot)


def _slot_updated(world: World, event: Event) -> None:
    _session(world, event).slots[event.payload["name"]] = Slot(
        name=event.payload["name"],
        value=event.payload["value"],
        source=event.payload.get("source", "user"),
    )


def _turn_ended(world: World, event: Event) -> None:
    s = _session(world, event)
    s.last_outcome = event.payload["outcome"]
    # 一个 turn 真正交付了（建议给了 / run 交出去了），本轮的阶梯预算就结束。
    # 否则同一个会话问满 3 次之后，后续任何话题都没法再澄清。
    if s.last_outcome in ("answered", "handed_off"):
        s.clarify_round = 0
        s.advice_rejects = 0


def _gate_checked(world: World, event: Event) -> None:
    if event.payload.get("profile") == "advice" and not event.payload.get("pass"):
        _session(world, event).advice_rejects += 1


def _run_started(world: World, event: Event) -> None:
    run_id = event.payload["run_id"]
    world.runs[run_id] = RunState(run_id=run_id, kind=event.payload["kind"], last_seq=event.seq)
    _session(world, event).runs.append(run_id)


def _plan_frozen(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        world.diagnostics.append(f"plan.frozen without run {event.run_id}")
        return
    plan = plan_from_dict(event.payload["plan"])
    run.plan = plan
    run.plan_hash = event.payload["plan_hash"]
    run.version = int(event.payload.get("version", 1))
    run.env_fp = str(event.payload.get("env_fp") or "")
    run.probe_fps = {
        str(step_id): {str(path): str(sha) for path, sha in (probes or {}).items()}
        for step_id, probes in (event.payload.get("probe_fps") or {}).items()
    }
    # 缺口 4：工具契约快照随事件流持久；派发前 executor 对当前 registry 复核。
    run.tool_snapshot = str(event.payload.get("tool_snapshot") or "")
    run.status = "running"
    for step in plan.steps:
        run.steps.setdefault(step.id, StepState(id=step.id))


def _model_called(world: World, event: Event) -> None:
    """D-031 S1：模型调用观测进会话账本（purpose/token/耗时/长度）。"""
    state = _session(world, event)
    state.model_calls.append(dict(event.payload))
    state.tokens_in += int(event.payload.get("tokens_in") or 0)
    state.tokens_out += int(event.payload.get("tokens_out") or 0)


def _memory_proposed(world: World, event: Event) -> None:
    """D-030 L2：模型起草的记忆条目进入待确认状态（重启后确认流不丢）。"""
    entries = [
        {"category": str(e.get("category") or ""), "content": str(e.get("content") or "")}
        for e in (event.payload.get("entries") or [])
        if isinstance(e, dict)
    ]
    _session(world, event).pending_memory = entries


def _memory_resolved(world: World, event: Event) -> None:
    """确认/取消都清空待确认状态：文件写入与否由 resolved.applied 表达。"""
    _session(world, event).pending_memory = []


def _advice_delivered(world: World, event: Event) -> None:
    """D-030 L1：assistant 建议原文进对话历史（advice.body 是模型说出的话）。"""
    advice = event.payload.get("advice") or {}
    body = str((advice.get("body") if isinstance(advice, dict) else None) or "")
    if body:
        _session(world, event).history.append({"role": "assistant", "text": body})


def _step_fingerprinted(world: World, event: Event) -> None:
    """缺口 3：记录步骤通过时的整树指纹（StepState.end_fp），供恢复对账精确判。"""
    run = _run(world, event)
    if run is None:
        return
    step_id = str(event.payload.get("step_id") or "")
    if not step_id:
        return
    state = run.steps.setdefault(step_id, StepState(id=step_id))
    state.end_fp = str(event.payload.get("fp_hash") or "")


def _plan_amended(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    # 载荷是**完整计划**（与 plan.frozen 同形）。旧日志只有 steps：不猜、不拼装，
    # 直接拒绝——按 steps 增量重建会让 then / evidence_requires 及以后新增的字段
    # 在重放时静默丢失（D-019）。
    if "plan" not in event.payload:
        raise ValueError(
            "plan.amended payload must carry the full plan (plan/plan_hash/version); "
            "the legacy steps-only shape is not supported"
        )
    plan = plan_from_dict(event.payload["plan"])
    run.plan = plan
    run.plan_hash = event.payload["plan_hash"]
    run.version = int(event.payload["version"])
    for step in plan.steps:
        run.steps.setdefault(step.id, StepState(id=step.id))
    for step_id in event.payload.get("invalidated") or []:
        st = run.steps.get(step_id)
        if st is not None:
            st.status = "pending"
            st.invalidated_by = event.payload.get("reason", "amended")


def _plan_rejected(world: World, event: Event) -> None:
    # 拒绝也是事实：计划没进 Executor，但它被拒过这件事必须可查。
    world.diagnostics.append(
        f"plan rejected: {[i.get('code') for i in event.payload.get('issues', [])]}"
    )


def _step_state_changed(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    if event.payload.get("illegal"):
        # 非法转移只记诊断，不许改投影：否则"拒绝"反而把状态改成了它拒绝的那个。
        world.diagnostics.append(
            f"illegal transition {event.payload.get('from')}→{event.payload.get('to')}"
            f" on {event.payload.get('step_id')}"
        )
        return
    step = run.steps.setdefault(event.payload["step_id"], StepState(id=event.payload["step_id"]))
    step.status = event.payload["to"]


def _step_started(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    step = run.steps.setdefault(event.payload["step_id"], StepState(id=event.payload["step_id"]))
    step.attempts = int(event.payload["attempt"])
    step.idempotency_key = event.payload["idempotency_key"]
    step.started_seq = event.seq
    step.status = "running"


def _step_finished(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    step = run.steps.setdefault(event.payload["step_id"], StepState(id=event.payload["step_id"]))
    step.result_status = event.payload["status"]
    step.artifact_refs = list(event.payload.get("artifact_refs") or [])
    step.error_code = event.payload.get("error_code")
    step.observations = dict(event.payload.get("observations") or {})
    step.finished_seq = event.seq
    step.evidence_seqs.append(event.seq)


def _env_fingerprinted(world: World, event: Event) -> None:
    """恢复时的环境指纹：只记录，不判定。

    「变了要不要重跑」是 Runner 的策略（`reconcile.drift_victims`），
    fold 只负责让重放能看到「第几次恢复、看到的是哪个指纹、变没变」。
    """
    run = _run(world, event)
    if run is None:
        return
    run.env_fp = str(event.payload["fp_hash"])
    run.env_changed = bool(event.payload["changed"])
    run.epoch = int(event.payload.get("epoch", run.epoch + 1))


def _artifact_written(world: World, event: Event) -> None:
    world.artifacts[event.payload["artifact_id"]] = {
        "sha256": event.payload["sha256"],
        "bytes": event.payload["bytes"],
        "mime": event.payload.get("mime", "application/octet-stream"),
        "producer": event.step_id,
        "producer_run": event.run_id,
        "seq": event.seq,
    }


def _step_verified(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    step = run.steps.setdefault(event.payload["step_id"], StepState(id=event.payload["step_id"]))
    step.checks = list(event.payload.get("checks") or [])
    step.verified_result = str(event.payload.get("result") or "")
    step.verified_reason = str(event.payload.get("reason") or "")
    step.verified_seq = event.seq
    step.evidence_seqs.append(event.seq)


def _failure_classified(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    step = run.steps.setdefault(event.payload["step_id"], StepState(id=event.payload["step_id"]))
    step.failure_class = event.payload["class"]
    step.error_code = event.payload.get("error_code")


def _budget_consumed(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    kind = event.payload["kind"]
    run.budget[kind] = float(event.payload["total"])


def _approval_requested(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    run.approval = dict(event.payload)
    run.status = "waiting_human"
    # 人批准的是「这一次的 payload」。摘要落在 step 上，执行前才有东西可比（§4.7）。
    step_id = event.payload.get("step_id")
    if step_id:
        step = run.steps.setdefault(step_id, StepState(id=step_id))
        step.approved_hash = str(event.payload.get("payload_hash") or "") or None


def _approval_received(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    # 拒绝不等于恢复：`reject` 之后 run 由调用方收尾（Session 会置 skipped + failed），
    # 这里替它宣布 "running" 就是篡改事实。
    if event.payload.get("decision") == "reject":
        # RT-09/§4.12：把「被拒」记在步骤上——绕过 Session 直接 resume 的调用方
        # 也不能对这个步骤重新发 token（§4.12 剪枝 / run.failed）。
        step_id = str((run.approval or {}).get("step_id") or "")
        if step_id:
            state = run.steps.setdefault(step_id, StepState(id=step_id))
            state.rejected_by_human = True
        return
    run.status = "running"


def _approval_resolved(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is not None and event.payload.get("applied"):
        run.approval = None


def _run_suspended(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is not None:
        run.status = "waiting_human"


def _run_resumed(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is not None:
        run.status = "running"


def _step_invalidated(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is None:
        return
    step = run.steps.setdefault(event.payload["step_id"], StepState(id=event.payload["step_id"]))
    step.status = "stale"
    step.invalidated_by = event.payload["reason"]


def _run_completed(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is not None:
        run.status = "completed"
        run.result = dict(event.payload["result"])


def _run_failed(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is not None:
        run.status = "failed"
        run.fail_reason = event.payload.get("reason", "")


def _run_cancelled(world: World, event: Event) -> None:
    """取消落终态：状态置 cancelled、原因随事件留档、待批 token 作废。

    已完成的步骤事实（passed / budget / 产物）原样保留——事件是真相，
    取消只终结 run，不改写历史。
    """
    run = _run(world, event)
    if run is not None:
        run.status = "cancelled"
        run.cancel_reason = str(event.payload.get("reason", ""))
        # token 作废：取消后旧审批不可用（fold 清投影；resume 侧另有终态防线）。
        run.approval = None


def _report_rendered(world: World, event: Event) -> None:
    run = _run(world, event)
    if run is not None:
        run.result = dict(event.payload["result"])
    # D-030 L1：assistant 收尾进历史（run 侧的总结也是模型说出的话）。
    summary = str(((event.payload.get("result") or {}).get("summary")) or "")
    if summary:
        _session(world, event).history.append({"role": "assistant", "text": summary})


def _run_transition_rejected(world: World, event: Event) -> None:
    """拒绝也是事实，但**不是**状态：重放只认它存在，不改任何投影。

    这条 handler 的职责只有一个：让 `run.transition_rejected` 在重放时可识别
    （BUG2-03：未知事件类型会记 diagnostic），而不是把拒绝的 `to` 应用上去。
    """


_HANDLERS = {
    EventType.TURN_RECEIVED: _turn_received,
    EventType.INTENT_ROUTED: _intent_routed,
    EventType.INTENT_CONFIRMED: _intent_confirmed,
    EventType.CLARIFY_ASKED: _clarify_asked,
    EventType.SLOT_UPDATED: _slot_updated,
    EventType.TURN_ENDED: _turn_ended,
    EventType.PLAN_PROPOSED: lambda w, e: None,
    EventType.PLAN_REJECTED: _plan_rejected,
    EventType.GATE_CHECKED: _gate_checked,
    EventType.PLAN_FROZEN: _plan_frozen,
    EventType.PLAN_AMENDED: _plan_amended,
    EventType.STEP_STATE_CHANGED: _step_state_changed,
    EventType.STEP_STARTED: _step_started,
    EventType.STEP_FINISHED: _step_finished,
    EventType.ARTIFACT_WRITTEN: _artifact_written,
    EventType.STEP_VERIFIED: _step_verified,
    EventType.STEP_FINGERPRINTED: _step_fingerprinted,
    EventType.FAILURE_CLASSIFIED: _failure_classified,
    EventType.BUDGET_CONSUMED: _budget_consumed,
    EventType.APPROVAL_REQUESTED: _approval_requested,
    EventType.APPROVAL_RECEIVED: _approval_received,
    EventType.APPROVAL_RESOLVED: _approval_resolved,
    EventType.RUN_SUSPENDED: _run_suspended,
    EventType.RUN_RESUMED: _run_resumed,
    EventType.STEP_INVALIDATED: _step_invalidated,
    EventType.ENV_FINGERPRINTED: _env_fingerprinted,
    EventType.RUN_STARTED: _run_started,
    EventType.RUN_COMPLETED: _run_completed,
    EventType.RUN_FAILED: _run_failed,
    EventType.RUN_CANCELLED: _run_cancelled,
    EventType.RUN_TRANSITION_REJECTED: _run_transition_rejected,
    EventType.REPORT_RENDERED: _report_rendered,
    EventType.ADVICE_DELIVERED: _advice_delivered,
    EventType.MEMORY_PROPOSED: _memory_proposed,
    EventType.MEMORY_RESOLVED: _memory_resolved,
    EventType.MODEL_CALLED: _model_called,
}
