"""JAgent v8 · contracts.

只放类型，不放行为。`agent/` 与 `harness/` 都只依赖这个文件，互相不认识。

设计约束（来自架构图的 15 个框 + Session）：
- 一个 run 的产物是 Result；一次建议的产物是 Advice。
- 判定只有 pass / fail，没有第三态；判不了按 fail 处理，理由写 evidence_missing。
- 每一步都带 verify 断言，断言由代码求值，模型不参与。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# canonical / hashing（vendored from harness/layers/kernel/canonical.py，纯函数）
# --------------------------------------------------------------------------- #

def _normalize(value: Any) -> Any:
    if isinstance(value, bool):  # bool 是 int 的子类，必须先判
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) or value is None:
        return value
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON requires string keys, got {type(key).__name__}")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise TypeError(f"cannot canonically encode {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    )


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #

class EventType(StrEnum):
    # turn 层
    TURN_RECEIVED = "turn.received"
    INTENT_ROUTED = "intent.routed"
    INTENT_CONFIRMED = "intent.confirmed"
    CLARIFY_ASKED = "clarify.asked"
    SLOT_UPDATED = "slot.updated"
    TURN_ENDED = "turn.ended"
    # 计划
    PLAN_PROPOSED = "plan.proposed"
    PLAN_REJECTED = "plan.rejected"
    GATE_CHECKED = "gate.checked"
    PLAN_FROZEN = "plan.frozen"
    PLAN_AMENDED = "plan.amended"
    # 执行
    STEP_STATE_CHANGED = "step.state_changed"
    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"
    ARTIFACT_WRITTEN = "artifact.written"
    STEP_VERIFIED = "step.verified"
    #: 步骤通过时的整树指纹（缺口 3）：恢复对账用「结束之后世界变没变」
    # 代替「冻结之后变没变」，写文件的步骤不再被自己的写多失效一次（D-029）。
    STEP_FINGERPRINTED = "step.fingerprinted"
    FAILURE_CLASSIFIED = "failure.classified"
    BUDGET_CONSUMED = "budget.consumed"
    # 人工
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RECEIVED = "approval.received"
    APPROVAL_RESOLVED = "approval.resolved"
    RUN_SUSPENDED = "run.suspended"
    RUN_RESUMED = "run.resumed"
    STEP_INVALIDATED = "step.invalidated"
    #: 恢复时的环境指纹快照（G-05）：只记录「恢复前采了一次指纹、和冻结时比变了没」，
    #: 不在这里判定谁该重跑——判定属于 Runner，这条事件只是事实。
    ENV_FINGERPRINTED = "env.fingerprinted"
    # 终态与投递
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    #: 用户取消（架构 §2：用户 / 人工确认中心取消）。终态，无出边；
    #: 载荷 {reason 必填, at_step 可选}——没有理由的取消不允许落账本（D-028）。
    RUN_CANCELLED = "run.cancelled"
    # 拒绝留痕：run 级非法转移也是事实（§2「拒绝本身也落事件」），但**不改投影**。
    RUN_TRANSITION_REJECTED = "run.transition_rejected"
    REPORT_RENDERED = "report.rendered"
    ADVICE_DELIVERED = "advice.delivered"
    #: L2 外部记忆（D-030）：模型起草的条目待用户确认；确认/取消落 `memory.resolved`。
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_RESOLVED = "memory.resolved"
    #: 可观测性（D-031 S1）：一次模型调用的观测事实。prompt/回复全文落产物库，
    #: 事件里只留 purpose、token、耗时、长度与 artifact_id——Run 原语的采集面。
    MODEL_CALLED = "model.called"


#: 每个事件必须具备的 payload 字段（多可以，少不行）。
REQUIRED_FIELDS: dict[EventType, tuple[str, ...]] = {
    EventType.TURN_RECEIVED: ("text_hash",),
    EventType.INTENT_ROUTED: ("type", "confidence", "missing_params"),
    EventType.INTENT_CONFIRMED: ("type",),
    EventType.CLARIFY_ASKED: ("round", "kind", "questions"),
    EventType.SLOT_UPDATED: ("name", "value", "source"),
    EventType.TURN_ENDED: ("outcome",),
    EventType.PLAN_PROPOSED: ("plan", "plan_hash", "attempt"),
    EventType.PLAN_REJECTED: ("attempt", "issues", "plan_hash"),
    EventType.GATE_CHECKED: ("profile", "pass", "ruleset"),
    EventType.PLAN_FROZEN: ("plan", "plan_hash", "version"),
    EventType.PLAN_AMENDED: ("plan", "plan_hash", "version", "reason", "invalidated"),
    EventType.STEP_STATE_CHANGED: ("step_id", "from", "to", "reason"),
    EventType.STEP_STARTED: ("step_id", "attempt", "idempotency_key"),
    EventType.STEP_FINISHED: ("step_id", "status", "artifact_refs"),
    EventType.ARTIFACT_WRITTEN: ("artifact_id", "sha256", "bytes"),
    EventType.STEP_VERIFIED: ("step_id", "result", "checks"),
    EventType.STEP_FINGERPRINTED: ("step_id", "fp_hash"),
    EventType.MEMORY_PROPOSED: ("entries",),
    EventType.MEMORY_RESOLVED: ("applied", "decision"),
    EventType.MODEL_CALLED: ("purpose", "tokens_in", "tokens_out", "duration_ms"),
    EventType.FAILURE_CLASSIFIED: ("step_id", "class", "error_code"),
    EventType.BUDGET_CONSUMED: ("kind", "delta", "total"),
    EventType.APPROVAL_REQUESTED: ("step_id", "token", "payload_hash", "expires_at"),
    EventType.APPROVAL_RECEIVED: ("token", "decision"),
    EventType.APPROVAL_RESOLVED: ("token", "applied"),
    EventType.RUN_SUSPENDED: ("at_step",),
    EventType.RUN_RESUMED: ("from_seq",),
    EventType.STEP_INVALIDATED: ("step_id", "reason"),
    EventType.ENV_FINGERPRINTED: ("fp_hash", "changed"),
    EventType.RUN_STARTED: ("run_id", "kind", "env"),
    EventType.RUN_COMPLETED: ("result",),
    EventType.RUN_FAILED: ("reason",),
    EventType.RUN_CANCELLED: ("reason",),
    EventType.RUN_TRANSITION_REJECTED: ("from", "to", "reason", "illegal"),
    EventType.REPORT_RENDERED: ("result",),
    EventType.ADVICE_DELIVERED: ("advice", "mode", "delivery"),
}


@dataclass(frozen=True)
class EventDraft:
    """写之前的草稿：store 负责补 seq / event_id / ts。"""

    type: EventType
    payload: dict[str, Any]
    session_id: str
    run_id: str | None = None
    step_id: str | None = None


@dataclass(frozen=True)
class Event:
    """写之后的事实。seq 全局单调，digest 覆盖前一条，用于重放比对。"""

    event_id: str
    seq: int
    type: EventType
    payload: dict[str, Any]
    session_id: str
    run_id: str | None
    step_id: str | None
    at: float
    schema_version: int = SCHEMA_VERSION
    digest: str = ""


def validate_draft(draft: EventDraft) -> None:
    if not isinstance(draft.type, EventType):
        raise ValueError(f"unknown event type: {draft.type!r}")
    if not isinstance(draft.payload, dict):
        raise ValueError(f"{draft.type.value}.payload must be a dict")
    missing = [key for key in REQUIRED_FIELDS.get(draft.type, ()) if key not in draft.payload]
    if missing:
        raise ValueError(f"{draft.type.value} is missing {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# 意图与槽位
# --------------------------------------------------------------------------- #

class IntentType(StrEnum):
    ADVISE = "advise"        # 模糊：价值判断，无 ground truth
    EXECUTE = "execute"      # 确定：有可机器验证的完成态
    #: 需要外部事实 + 结论是价值判断：先取数（只读计划），再基于证据给建议。
    GATHER = "gather"


@dataclass(frozen=True)
class IntentDecision:
    type: IntentType
    confidence: float
    missing_params: tuple[str, ...] = ()
    single_step: bool = False
    target_tool: str | None = None
    reason: str = ""

    @property
    def needs_path_choice(self) -> bool:
        return self.confidence < 0.7


@dataclass(frozen=True)
class Slot:
    name: str
    value: Any
    source: str = "user"          # user | inferred
    asked_round: int = 0


@dataclass(frozen=True)
class Question:
    slot: str
    text: str
    options: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# 计划与断言
# --------------------------------------------------------------------------- #

class OnFail(StrEnum):
    RETRY = "retry"          # 瞬时：执行器内重试
    REPAIR = "repair"        # 断言：改参数/改计划
    ASK_USER = "ask_user"    # 确定性：升级人工

@dataclass(frozen=True)
class Verify:
    """断言：在 step 产出的观测上求值。needs 声明要读哪些观测名。"""

    expr: str
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    verify: Verify | None = None
    on_fail: OnFail = OnFail.REPAIR
    gated: bool = False
    required: bool = True
    env_probes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceReq:
    """取数前声明的证据要求：某一步的某个观测名必须齐。"""

    step_id: str
    field: str


@dataclass(frozen=True)
class Plan:
    steps: tuple[Step, ...]
    version: int = 1
    #: 取数完成后要不要基于证据给建议（planner 显式声明，不靠路由猜）。
    #: 目前只允许 "advice" 或 None。
    then: str | None = None
    #: 取数前声明的证据清单；是否满足只由 jagent/evidence.py 的 missing() 计算。
    evidence_requires: tuple[EvidenceReq, ...] = ()

    def hash(self) -> str:
        # 注意：必须走 step_to_dict，不能用 s.__dict__——Verify 是 dataclass，
        # digest_of 只接受 JSON 可编码的值。
        return digest_of(
            {
                "steps": [step_to_dict(s) for s in self.steps],
                "version": self.version,
                "then": self.then,
                "evidence_requires": [evidence_req_to_dict(r) for r in self.evidence_requires],
            }
        )

    def by_id(self) -> dict[str, Step]:
        return {s.id: s for s in self.steps}

    def order(self) -> list[Step]:
        """拓扑序；有环直接抛。"""
        by_id = self.by_id()
        seen: set[str] = set()
        out: list[Step] = []

        def visit(step: Step, stack: tuple[str, ...]) -> None:
            if step.id in seen:
                return
            if step.id in stack:
                raise ValueError(f"cycle in plan: {' -> '.join((*stack, step.id))}")
            for dep in step.depends_on:
                if dep not in by_id:
                    raise ValueError(f"step {step.id} depends on unknown step {dep}")
                visit(by_id[dep], (*stack, step.id))
            seen.add(step.id)
            out.append(step)

        for step in self.steps:
            visit(step, ())
        return out

    def downstream(self, step_id: str) -> list[str]:
        """包含自己 + 所有（传递）下游。增量重跑的失效范围。"""
        children: dict[str, list[str]] = {}
        for step in self.steps:
            for dep in step.depends_on:
                children.setdefault(dep, []).append(step.id)
        out: list[str] = []
        stack = [step_id]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out


def evidence_req_to_dict(req: EvidenceReq) -> dict[str, Any]:
    return {"step_id": req.step_id, "field": req.field}


def step_to_dict(step: Step) -> dict[str, Any]:
    return {
        "id": step.id,
        "tool": step.tool,
        "args": step.args,
        "depends_on": list(step.depends_on),
        "verify": None if step.verify is None else {"expr": step.verify.expr, "needs": list(step.verify.needs)},
        "on_fail": step.on_fail.value,
        "gated": step.gated,
        "required": step.required,
        "env_probes": list(step.env_probes),
    }


def step_from_dict(data: dict[str, Any]) -> Step:
    verify_raw = data.get("verify")
    return Step(
        id=data["id"],
        tool=data["tool"],
        args=dict(data.get("args") or {}),
        depends_on=tuple(data.get("depends_on") or ()),
        verify=None if not verify_raw else Verify(
            expr=verify_raw["expr"], needs=tuple(verify_raw.get("needs") or ())
        ),
        on_fail=OnFail(data.get("on_fail", "repair")),
        gated=bool(data.get("gated", False)),
        required=bool(data.get("required", True)),
        env_probes=tuple(data.get("env_probes") or ()),
    )


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "steps": [step_to_dict(s) for s in plan.steps],
        "version": plan.version,
        "then": plan.then,
        "evidence_requires": [evidence_req_to_dict(r) for r in plan.evidence_requires],
    }


def plan_from_dict(data: dict[str, Any]) -> Plan:
    return Plan(
        steps=tuple(step_from_dict(s) for s in data["steps"]),
        version=int(data.get("version", 1)),
        then=data.get("then"),
        evidence_requires=tuple(
            EvidenceReq(step_id=r["step_id"], field=r["field"])
            for r in data.get("evidence_requires") or ()
        ),
    )


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

class SideEffect(StrEnum):
    NONE = "none"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class Idempotency(StrEnum):
    KEYED = "keyed"        # 支持按 key 去重（可对账）
    NATURAL = "natural"    # 天然幂等（读）
    NONE = "none"          # 不可幂等，必须 gated


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    ASSERTION = "assertion"
    DETERMINISTIC = "deterministic"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    args_schema: dict[str, Any]
    side_effect: SideEffect = SideEffect.NONE
    idempotency: Idempotency = Idempotency.NATURAL
    error_codes: tuple[str, ...] = ()
    timeout_s: float = 30.0
    readonly: bool = True
    description: str = ""
    #: 这个工具会产出哪些观测名。Planner 的断言只能引用这里声明过的名字，
    #: 这就是架构文档 §7.2 的 `needs ⊆ provides`。
    observations: tuple[str, ...] = ("ok",)

    def descriptor(self) -> dict[str, Any]:
        """喂给 Planner 的最小描述。"""
        return {
            "name": self.name,
            "args": self.args_schema,
            "side_effect": self.side_effect.value,
            "readonly": self.readonly,
            "error_codes": list(self.error_codes),
            "observations": list(self.observations),
            "description": self.description,
        }


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass
class StepOutcome:
    """一步执行完的观测。Verifier 只读这里，不读工具的私有结构。"""

    ok: bool
    observations: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str = ""
    attempt: int = 1


@dataclass
class VerifyOutcome:
    passed: bool
    checks: tuple[dict[str, Any], ...] = ()
    reason: str = ""


#: 失败分类的唯一依据：错误码 → 类别。没列到的一律按确定性处理。
ERROR_CLASS: dict[str, FailureClass] = {
    "timeout": FailureClass.TRANSIENT,
    "rate_limited": FailureClass.TRANSIENT,
    "upstream_5xx": FailureClass.TRANSIENT,
    "connection_reset": FailureClass.TRANSIENT,
    "not_found": FailureClass.DETERMINISTIC,
    "permission_denied": FailureClass.DETERMINISTIC,
    "command_not_found": FailureClass.DETERMINISTIC,
    "invalid_args": FailureClass.DETERMINISTIC,
    "sandbox_denied": FailureClass.DETERMINISTIC,
    "tool_exception": FailureClass.DETERMINISTIC,
}


def classify_error(error_code: str | None) -> FailureClass:
    if error_code is None:
        return FailureClass.ASSERTION
    return ERROR_CLASS.get(error_code, FailureClass.DETERMINISTIC)


# --------------------------------------------------------------------------- #
# 建议 / 合规 / 结果
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Citation:
    """建议里对某条观测的引用。

    ``seq`` **由系统回填**（Session 按 ``(step_id, field)`` 从 ``step.finished`` 或
    artifact 查），模型输出的 schema 里不出现此字段——模型看不到事件序号，给了就能伪造。
    """

    step_id: str      # 必须是本次 run 的已通过步骤
    field: str        # 观测名，例如 "data.pe" / "stdout"
    label: str = ""   # 给人看的来源名
    seq: int | None = None


@dataclass(frozen=True)
class Advice:
    info: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    framework: tuple[str, ...] = ()
    #: 对账用；有 refs 时展示层用 refs 渲染【信息来源】，忽略 sources。
    refs: tuple[Citation, ...] = ()
    sources: tuple[str, ...] = ()
    disclaimer: str = ""
    body: str = ""


@dataclass(frozen=True)
class EvidenceItem:
    """一次 run 里某一步产出的证据（观测值已截断）。"""

    step_id: str
    seq: int
    status: str
    observations: dict[str, str]
    truncated: tuple[str, ...]
    artifact_ids: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceBundle:
    """gather / execute 收尾时交给 Propose 的证据包；内部对象，不落事件。"""

    run_id: str
    items: tuple[EvidenceItem, ...]
    omitted_steps: int = 0


# --------------------------------------------------------------------------- #
# Planner 草稿（planner 除计划外的三种输出模式）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ClarifyDraft:
    """Planner 判定信息不足时的澄清草稿。

    问题由模型根据工具 args schema 现场生成，不来自静态槽位表；
    Session 只校验形状与合法性（slot 名、数量、长度），内容不校验。
    """

    questions: tuple[Question, ...]


@dataclass(frozen=True)
class ReplyDraft:
    """无需工具、也无合规风险的直接回答（闲聊 / 常识问答）。

    投递前仍要过 reply 侧合规扫描：命中指令性表述或无来源数字声明即拦截，
    拦下后走兜底文本，不让合规风险绕过闸门。
    """

    text: str


@dataclass(frozen=True)
class AdviceDraft:
    """结论性建议草稿，必须过 AdviceGate 才投递。"""

    advice: Advice


#: PlannerPort 的返回值：计划或三种草稿之一。
@dataclass(frozen=True)
class MemoryEntry:
    """一条长期记忆（D-030 L2）：四类之一，内容一句话，来源在落盘时标注。

    ``category`` ∈ ``user / feedback / project / reference``——写入口收敛，
    不允许第四种自造类别。"""

    category: str
    content: str


@dataclass(frozen=True)
class MemoryDraft:
    """模型起草的长期记忆（planner mode ``"memory"``）。

    起草不是写入：Session 落 ``memory.proposed`` 并等用户确认，确认后才进
    ``memory.md``（M6-A：模型起草、人点头、写入落事件留痕）。"""

    entries: tuple[MemoryEntry, ...] = ()


PlannerDraft = Plan | ClarifyDraft | ReplyDraft | AdviceDraft | MemoryDraft


@dataclass(frozen=True)
class Violation:
    code: str
    rule_id: str
    detail: str = ""


@dataclass(frozen=True)
class Fallback:
    """兜底原因。``reason`` 的取值是违规码 / ``policy_reject``（``run_aborted`` 已随 D-028 删除）。"""

    reason: str
    detail: str = ""


@dataclass(frozen=True)
class JudgeRecord:
    """T2 评审结论。judge 只出 REJECT / UNDECIDABLE / None，没有 PASS。"""

    verdict: str
    reason: str = ""
    model: str = ""


@dataclass(frozen=True)
class GateVerdict:
    profile: str               # advice（v8 只有建议侧）
    passed: bool
    violations: tuple[Violation, ...] = ()
    ruleset: str = "advice.v8.1"
    #: 只描述证据状况，由 gate 从 evidence_requires 推导；与是否兜底正交。
    mode: str = "framework"    # grounded | insufficient_evidence | framework
    partial: bool = False
    fallback: Fallback | None = None
    citations: tuple[Citation, ...] = ()
    judge: JudgeRecord | None = None


class RunVerdict(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class StepReport:
    id: str
    status: str
    artifact_refs: tuple[str, ...] = ()
    checks: tuple[dict[str, Any], ...] = ()
    evidence_seqs: tuple[int, ...] = ()


@dataclass(frozen=True)
class Result:
    run_id: str
    verdict: RunVerdict
    steps: tuple[StepReport, ...] = ()
    summary: str = ""
    remaining_plan: tuple[str, ...] = ()
    budget_used: dict[str, float] = field(default_factory=dict)
    generated_by: str = "template"


class Regime(StrEnum):
    """对外文本的可信档位（宪法 v2 I3）。

    三值，且**没有"通过"**：

    - ``VERIFIED``：文本由受信代码产生（模板 / 兜底 / 事实渲染），或由代码断言得出；
    - ``GROUNDED``：模型写的复述，已过确定性检查（数字⊆事实、引用⊆证据、无指令性表述）；
    - ``UNVERIFIED``：没有判据，只能标注——它永远不是"通过"，也不得流入受信输入。
    """

    VERIFIED = "verified"
    GROUNDED = "grounded"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class Delivery:
    """对外文本（对话气泡的唯一来源）。

    ``regime`` / ``checks`` / ``grounds`` / ``fallback`` **全部由代码写**，模型看不到、
    也改不了；``text`` 才是模型可能参与的那一部分。终态记录（``Result``）不装本对象的
    文本——事实与措辞分家（I8）。
    """

    text: str
    regime: Regime = Regime.VERIFIED
    #: 本次实际跑过且通过的确定性检查 id（见 `jagent/grounding.py`）。
    checks: tuple[str, ...] = ()
    #: 这段文本能指回的事件 seq（grounded 文本必有；unverified 恒空）。
    grounds: tuple[int, ...] = ()
    #: 降级与原因：``{"bool": bool, "reason": str}``；未降级时为空 dict。
    fallback: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None


# --------------------------------------------------------------------------- #
# 预算 / 配置
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Budget:
    max_steps: int = 10
    max_wall_s: float = 120.0
    transient_per_step: int = 3
    max_repairs: int = 2
    max_plan_attempts: int = 3
    max_advice_attempts: int = 3
    max_clarify_rounds: int = 3
    #: 证据包最多带几步（只取尾部，足以支撑结论）。
    evidence_max_steps: int = 8
    #: 证据里每一步观测值的截断长度（MCP 载荷可能很大，事件日志不当缓存用）。
    evidence_clip_chars: int = 600
    #: 允许跳过"不可逆工具必须人工确认"的工具名。默认空 = 全部要确认。
    #: 产品里应该由用户在界面上逐次批准，而不是预先写死。
    auto_approve_tools: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Budget:
        """从外部配置构造预算；缺字段 / 多字段 / 类型不对都抛 ``ValueError``。

        这是配置的**加载入口**：字段必须齐全。不用 dataclass 的字段默认值兜底，
        否则漏写一项会静默回落到代码里的旧值——"改了配置却没生效"必须表现为
        启动失败，而不是变成一个看不出来的实验变量。
        """
        names = {item.name for item in fields(cls)}
        missing = sorted(names - set(data))
        unknown = sorted(set(data) - names)
        if missing:
            raise ValueError(f"缺少字段：{', '.join(missing)}")
        if unknown:
            raise ValueError(f"未知字段：{', '.join(unknown)}")

        values = dict(data)
        raw_tools = values["auto_approve_tools"]
        if not isinstance(raw_tools, (list, tuple)) or not all(
            isinstance(item, str) for item in raw_tools
        ):
            raise ValueError("auto_approve_tools 必须是字符串数组")
        values["auto_approve_tools"] = tuple(raw_tools)

        for key in (
            "max_steps",
            "transient_per_step",
            "max_repairs",
            "max_plan_attempts",
            "max_advice_attempts",
            "max_clarify_rounds",
            "evidence_max_steps",
            "evidence_clip_chars",
        ):
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} 必须是整数")
        wall = values["max_wall_s"]
        if isinstance(wall, bool) or not isinstance(wall, (int, float)):
            raise ValueError("max_wall_s 必须是数字")
        values["max_wall_s"] = float(wall)
        return cls(**values)

    def as_dict(self) -> dict[str, float]:
        return {
            "max_steps": float(self.max_steps),
            "max_wall_s": self.max_wall_s,
            "transient_per_step": float(self.transient_per_step),
            "max_repairs": float(self.max_repairs),
            "max_plan_attempts": float(self.max_plan_attempts),
        }
