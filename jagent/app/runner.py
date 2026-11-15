"""JAgent v8 · 执行环（Planner → Executor → Verifier → 分类器 → Repair → Reporter）。

这个文件是图上右半边那条环的唯一实现：
- 严格按 DAG 依赖取就绪步；
- 每步执行完立刻断言（不攒到最后）；
- 失败三分：瞬时重试 / 断言修复 / 确定性失败；
- 修复只失效「本步 + 下游」，已通过的上游直接复用（不重复执行）；
- 需要人工的步骤挂起，进程可以退出，恢复时从挂起点续跑。

恢复（G-05）不是"把 drive 再跑一遍"，三件事都在这条环的外沿：
- 冻结时把环境钉住（`plan.frozen.env_fp`），恢复时重采一次并记 `env.fingerprinted`；
  变了只失效「已 passed 且产物来自 workspace」的步骤（判定在 `harness/reconcile.py`）；
- 执行**任何** gated/不可逆步骤之前，重算 `digest_of({tool, args})` 和人批准时的
  `approved_hash` 比对：不一致就不执行，重新走一次审批（§4.7）；
- 进程死在 `step.started` 之后时先对账（in-doubt）：能安全重跑的重排队（复用同一个
  幂等键），不能的（`shell.exec` 这种）不猜，转人工。
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import load_config, runtime_env
from ..contracts import (
    Budget,
    EventType,
    FailureClass,
    OnFail,
    Plan,
    Result,
    Step,
    StepOutcome,
    VerifyOutcome,
    classify_error,
    digest_of,
    plan_to_dict,
)
from ..harness.contract_snapshot import snapshot
from ..harness.fingerprint import Snapshot, probe_fingerprints, snapshot_workspace
from ..harness.plancheck import check_plan
from ..harness.reconcile import (
    awaiting_verification,
    drift_victims,
    invalidate,
    last_attempt,
    outcome_from_log,
    probe_drift,
    reconcile_in_doubt,
    unreconciled_steps,
    verdict_from_log,
)
from ..harness.repair import reuse_key
from ..harness.statemachine import RUN_TERMINAL
from ..harness.writer import Writer
from ..logsupport import event, fail, warn

#: `_settle` 的控制令牌：继续跑下一步。
CONTINUE = "continue"

#: 修复预算耗尽的失败原因前缀。带 step_id，由 `_fail` 补成含尝试次数与
#: 最后一次失败原因的可读文本；只用于报告，不是审批事件（I1 终态守卫）。
REPAIR_BUDGET_EXHAUSTED = "repair_budget_exhausted"


def _plan_detail(plan: Plan) -> str:
    """计划的逐步规划详情（日志）：工具、参数、断言、依赖、闸门。"""
    lines: list[str] = []
    for step in plan.steps:
        head = f"{step.id} · {step.tool}"
        if step.gated:
            head += " [gated]"
        if not step.required:
            head += " [optional]"
        if step.depends_on:
            head += f"  depends_on={list(step.depends_on)}"
        lines.append(head)
        lines.append(f"      args={json.dumps(step.args or {}, ensure_ascii=False)}")
        if step.verify is not None:
            lines.append(f"      verify: {step.verify.expr}  needs={list(step.verify.needs)}")
        else:
            lines.append("      verify: (none)")
    return "\n".join(lines)

#: 修复调用只喂失败事实：实际观测摘要与 stderr 的字符上限（runner 侧先截断一次，
#: 修复 prompt 侧还有第二道同类上限）。这两个值就是"失败事实"的输入边界：
#: 观测最多 8 个字段、每个值 120 字符、JSON 摘要合计 ≤ 1000 字符；stderr ≤ 1000 字符。
REPAIR_OBSERVATIONS_CHARS = 1000
REPAIR_OBSERVATION_FIELDS = 8
REPAIR_OBSERVATION_VALUE_CHARS = 120
REPAIR_ERROR_MESSAGE_CHARS = 1000

_REPAIR_TRUNCATED = "…（已截断）"


@dataclass
class RunOutcome:
    run_id: str
    status: str                 # completed | failed | cancelled | waiting_human
    result: Result | None = None
    reason: str = ""


class Runner:
    def __init__(self, *, writer: Writer, executor: Any, verifier: Any, repairer: Any,
                 reporter: Any, human: Any, budget: Budget,
                 registry: Any = None, auto_approve: frozenset[str] = frozenset(),
                 workspace: str | Path | None = None,
                 env: Mapping[str, str] | None = None,
                 artifacts: Any = None,
                 summarizer: Any = None) -> None:
        self.writer = writer
        self.executor = executor
        self.verifier = verifier
        self.repairer = repairer
        self.reporter = reporter
        self.human = human
        self.budget = budget
        self.registry = registry
        #: 缺口 5：报告全文落产物库；未注入（直接构造的测试）则跳过。
        self.artifacts = artifacts
        #: 收尾总结（D-042）：模型把事实包复述成一段人话，替换模板摘要。
        #: 未注入（离线/替身装配）时与没有这个能力时完全一致。
        self.summarizer = summarizer
        #: 本次启动的代码/配置指纹（I6）。装配处算一次传进来；直接构造 Runner 的
        #: 测试/工具没有注入时，按包内配置算一次，保证 run.started 永远带 env。
        self.env: dict[str, str] = dict(env) if env is not None else runtime_env(load_config())
        #: 不询问就能执行的不可逆工具。默认空 => 每个不可逆步骤都要人点头。
        self.auto_approve = auto_approve
        #: 环境指纹的作用域。缺省从 executor 上取（装配处不必多传一个参数，
        #: 红队/测试的 `make_runner` 也不会因为新参数而装配失败）。
        self.workspace = Path(workspace) if workspace is not None else _executor_workspace(executor)
        #: 已经因为"确定性错误"求过助的步骤（每步最多一次，避免死循环）。
        self._diagnosed: set[str] = set()
        #: 请求取消的 run（run_id → reason，D-028 边界取消）。只设标记不写事件：
        #: 事件由 drive 在步边界落，保证在跑步的 step.finished 先于 run.cancelled。
        #: waiting_human 的取消不走这里——没有循环可协作，Session 直接写事件。
        self._cancel_requested: dict[str, str] = {}

    def _needs_approval(self, step: Step) -> bool:
        if step.gated:
            return True
        if self.registry is None:
            return False
        try:
            spec = self.registry.get(step.tool)
        except KeyError:
            return False
        return spec.side_effect.value == "irreversible" and spec.name not in self.auto_approve

    # ------------------------------------------------------------------ api --
    def start_and_run(self, plan: Plan, *, kind: str = "execute") -> RunOutcome:
        run_id = "run_" + uuid.uuid4().hex[:12]
        snap = self._snapshot()
        self.writer.emit(
            EventType.RUN_STARTED,
            {"run_id": run_id, "kind": kind, "env": dict(self.env)},
            run_id=run_id,
        )
        self.writer.emit(
            EventType.PLAN_FROZEN,
            {
                "plan": plan_to_dict(plan), "plan_hash": plan.hash(), "version": plan.version,
                # 冻结时把环境钉住：隔天回来时，只有它变了才谈得上「失效」。
                "env_fp": "" if snap is None else snap.fp_hash,
                # 每步一份 probe 指纹：恢复时逐项比，才谈得上「失效是局部的」。
                "probe_fps": self._probe_baseline(plan),
                # 缺口 4：契约快照——冻结的是「工具还是不是刚才那组」，不只参数。
                "tool_snapshot": (
                    snapshot(self.registry.specs()) if self.registry is not None else ""
                ),
            },
            run_id=run_id,
        )
        event(
            "plan frozen", run=run_id, kind=kind,
            steps=[f"{s.id}:{s.tool}" for s in plan.steps],
            detail=_plan_detail(plan),
        )
        return self.drive(run_id)

    def drive(self, run_id: str, *, approved: frozenset[str] = frozenset()) -> RunOutcome:
        run = self.writer.world.runs[run_id]
        # 边界取消（D-028）：用户意图优先于一切防线，含下面的 plan-None 检查。
        if run_id in self._cancel_requested:
            return self._cancel(run_id, self._cancel_requested.pop(run_id))
        if run.plan is None:
            if run.status in RUN_TERMINAL:
                # 已终态的半写 run：安静返回当前态，不写事件（吸收态）。
                return RunOutcome(run_id=run_id, status=run.status)
            # BUG2-09：半写 run（崩溃在 plan.frozen 之前）不是"零步任务"——
            # 伪装成 completed(0/0) 会把故障漂白成成功。fail-closed。
            return self._fail(run_id, "no_frozen_plan")
        # 次数从投影里接着数：跨 resume 的第二次执行必须是 attempt=2，
        # 而不是"每次 resume 都从 1 开始"——否则日志里看不出重试过几次。
        attempts: dict[str, int] = {sid: state.attempts for sid, state in run.steps.items()}
        started = time.time()

        while True:
            # 边界取消（D-028）：用户意图优先于一切自动终止路径（墙钟/步预算）。
            # 检查点在循环顶部——在跑的步已在上一轮边界跑完，这里只决定
            # "还开不开下一个 step"（Roadmap 03 §7 的循环终止模式）。
            if run_id in self._cancel_requested:
                return self._cancel(run_id, self._cancel_requested.pop(run_id))

            if time.time() - started > self.budget.max_wall_s:
                # BUG2-12：墙钟也是账——超时出口记 wall_s，budget_used 不再恒 0。
                self.writer.budget(run_id, "wall_s", delta=time.time() - started)
                return self._fail(run_id, "wall_clock_exceeded")

            # 先收拾崩溃留下的"跑完但没走完"的步骤：重放判定，绝不重跑工具。
            decision = self._resume_settled(run_id, attempts)
            if decision != CONTINUE:
                return self._decision_outcome(run_id, decision)

            step = self._next_step(run_id)
            if step is None:
                self.writer.budget(run_id, "wall_s", delta=time.time() - started)
                return self._finish(run_id)
            # 预算只在"还有活要干"时才拦；刚好用满 max_steps 的计划应该正常完成。
            if run.budget["steps"] >= self.budget.max_steps:
                return self._fail(run_id, "step_budget_exceeded")

            # 人工确认：挂起，等外部决定（进程可以退出）
            if self._needs_approval(step):
                if step.id not in approved:
                    state = run.steps.get(step.id)
                    if state is not None and state.rejected_by_human:
                        # RT-09/§4.12：被拒过的步骤绝不重新发 token。
                        # 必需步骤 → run 失败；非必需步骤 → 剪枝跳过。
                        if step.required:
                            return self._fail(run_id, f"rejected_by_human:{step.id}")
                        self.writer.step_to(run_id, step.id, "skipped", "rejected_by_human")
                        continue
                    # 已经在等同一个 step 的决定就复用旧 token：resume 重复调用不该刷 token。
                    token = self._pending_token_for(run, step.id) or self._request_approval(run_id, step)
                    return RunOutcome(run_id=run_id, status="waiting_human", reason=f"token={token}")
                # §4.7：执行前重校验「人批准的那一次 payload」和「现在要执行的 payload」。
                # 不一致就不执行——否则批准的是 A、执行的是 B，人签的字白签。
                refusal = self._stale_approval(run, step)
                if refusal is not None:
                    self.writer.emit(EventType.APPROVAL_RESOLVED, refusal, run_id=run_id)
                    token = self._request_approval(run_id, step, note=refusal["reason"])
                    return RunOutcome(run_id=run_id, status="waiting_human", reason=f"token={token}")

            attempts[step.id] = attempts.get(step.id, 0) + 1
            outcome: StepOutcome = self.executor.run_step(run_id, step, attempt=attempts[step.id])
            decision = self._settle(run_id, step, outcome, attempts)
            if decision != CONTINUE:
                return self._decision_outcome(run_id, decision)

    # ---------------------------------------------------------------- cancel --
    def cancel(self, run_id: str, reason: str = "user cancelled") -> None:
        """请求取消（D-028 边界取消）：只设标记，`drive` 在下一个步边界消费。

        在跑的步让它跑完（工具无法安全中断）；本方法**不写事件**——
        run.cancelled 由 `_cancel` 在步边界落，事件顺序才是真的
        「step.finished 先于 run.cancelled」。重复请求以第一次原因为准。
        """
        self._cancel_requested.setdefault(run_id, reason)

    def _cancel(self, run_id: str, reason: str) -> RunOutcome:
        """在步边界落取消：不渲染报告（取消的 run 没有结论可给）。

        已终态的 run（重复取消、取消打到已完成 run）不再落事件——cancelled
        是吸收态，事件只允许一条；此时返回当前终态的 Outcome。
        """
        run = self.writer.world.runs[run_id]
        if run.status in RUN_TERMINAL:
            return RunOutcome(run_id=run_id, status=run.status, reason=reason)
        next_step = self._next_step(run_id)
        payload: dict[str, Any] = {"reason": reason}
        if next_step is not None:
            payload["at_step"] = next_step.id
        self.writer.emit(EventType.RUN_CANCELLED, payload, run_id=run_id)
        event("run CANCELLED", run=run_id, reason=reason)
        return RunOutcome(run_id=run_id, status="cancelled", reason=reason)

    def _settle(
        self,
        run_id: str,
        step: Step,
        outcome: StepOutcome,
        attempts: dict[str, int],
        *,
        verdict: VerifyOutcome | None = None,
    ) -> str:
        """一步执行之后的全部判定：断言 → 通过 / 分类 → 重试 / 修复 / 升级。

        返回控制令牌而不是直接 `continue`/`return`，是为了让**恢复路径**复用它：
        崩溃在 `step.finished` 之后的步骤只需要重放判定，不该重跑工具，
        但通过/失败之后的行为必须和执行路径一模一样。
        """
        run = self.writer.world.runs[run_id]
        if verdict is None:
            verdict = self.verifier.verify(step, outcome)
            passed_all = outcome.ok and verdict.passed
            self.writer.emit(
                EventType.STEP_VERIFIED,
                {
                    "step_id": step.id,
                    "result": "pass" if passed_all else "fail",
                    "checks": list(verdict.checks),
                    "reason": verdict.reason,
                },
                run_id=run_id, step_id=step.id,
            )
            if passed_all:
                event("verify pass", run=run_id, step=step.id, checks=len(verdict.checks))
            else:
                failed = [
                    str(check.get("detail") or check.get("expr") or "?")
                    for check in verdict.checks if not check.get("passed")
                ]
                warn(
                    "verify FAIL", run=run_id, step=step.id,
                    checks=failed or verdict.reason, reason=verdict.reason,
                )

        if outcome.ok and verdict.passed:
            self.writer.step_to(run_id, step.id, "passed", "assertions_passed")
            self.writer.budget(run_id, "steps")
            # 缺口 3：步骤通过时的整树指纹——恢复对账时「世界在它结束之后没变」
            # 就不算受害者，写文件的步骤不再被自己的写多失效一次（D-029）。
            snap = self._snapshot()
            if snap is not None:
                self.writer.emit(
                    EventType.STEP_FINGERPRINTED,
                    {"step_id": step.id, "fp_hash": snap.fp_hash},
                    run_id=run_id, step_id=step.id,
                )
            return CONTINUE

        cls = self._classify(outcome, verdict)
        error_code = outcome.error_code or ("assertion_failed" if outcome.ok else None)
        self.writer.emit(
            EventType.FAILURE_CLASSIFIED,
            {
                "step_id": step.id,
                "class": cls.value,
                "error_code": error_code,
                "reason": verdict.reason,
            },
            run_id=run_id, step_id=step.id,
        )
        warn(
            "failure classified", run=run_id, step=step.id,
            cls=cls.value, error=error_code, reason=verdict.reason,
        )
        self.writer.step_to(run_id, step.id, "failed", cls.value)

        # ① 瞬时：执行器内重试，不进 repair
        if cls is FailureClass.TRANSIENT and attempts.get(step.id, 0) < self.budget.transient_per_step:
            self.writer.budget(run_id, "transient")
            self.writer.step_to(run_id, step.id, "pending", "transient_retry")
            event("transient retry", run=run_id, step=step.id, attempt=attempts.get(step.id, 1))
            time.sleep(min(0.05 * attempts.get(step.id, 1), 0.2))
            return CONTINUE

        # ② 断言：改计划，只重跑本步 + 下游
        if cls is FailureClass.ASSERTION:
            if run.budget["repairs"] >= self.budget.max_repairs:
                # 预算已耗尽：不再调修复器（哪怕它这次能给出计划），也不发审批事件。
                # run 马上落 failed；"转人工"只是 `_fail` 写进报告给人看的一句话。
                return f"fail:{REPAIR_BUDGET_EXHAUSTED}:{step.id}"
            new_plan = self.repairer.repair(
                plan=run.plan, step_id=step.id,
                failure={
                    "class": cls.value, "reason": verdict.reason,
                    "error_code": outcome.error_code, "checks": list(verdict.checks),
                    # 修复只喂失败事实：实际观测（含 stdout/stderr）与 stderr 都先截断。
                    "observations": _bounded_observations(outcome.observations),
                    "error_message": _clip_text(
                        outcome.error_message
                        or str((outcome.observations or {}).get("stderr") or ""),
                        REPAIR_ERROR_MESSAGE_CHARS,
                    ),
                },
                attempt=attempts.get(step.id, 1),
            )
            warn(
                "repair attempted", run=run_id, step=step.id,
                new_plan="yes" if new_plan is not None else "no",
            )
            # `_apply_repair` 负责应用前的 check_plan 复核：复核不过就不应用、
            # 不记修复次数，落回下面的既有失败路径（不重试）。
            if new_plan is not None and self._apply_repair(run_id, step.id, new_plan):
                self.writer.budget(run_id, "repairs")
                return CONTINUE

        # ③ 确定性（404/权限/命令不存在）：不可自愈，升级人工（图的 E14）。
        #    但只对"人能修的环境问题"升级；策略类错误（越权/参数非法）直接失败。
        if (
            cls is FailureClass.DETERMINISTIC
            and step.id not in self._diagnosed
            and self._wants_human(step, outcome.error_code)
        ):
            self._diagnosed.add(step.id)
            problem = outcome.error_code or "未知错误"
            token = self.human.request(
                run_id, step.id, payload_hash=_payload_hash(step),
                context=f"{step.tool} {_brief(step.args)} → {problem}（这一步需要你处理）",
            )
            return f"waiting:{token}"

        return f"fail:{cls.value}:{step.id}"

    def _decision_outcome(self, run_id: str, decision: str) -> RunOutcome:
        """把 `_settle` 的控制令牌翻成对外结果。"""
        kind, _, value = decision.partition(":")
        if kind == "waiting":
            return RunOutcome(run_id=run_id, status="waiting_human", reason=f"token={value}")
        return self._fail(run_id, value if kind == "fail" else decision)

    def _resume_settled(self, run_id: str, attempts: dict[str, int]) -> str:
        """崩溃窗口：工具已经跑完，但日志里只走到 `step.finished`（或只差状态转移）。

        这不是 in-doubt（做没做是确定的），所以**不重跑工具**：
        有 `step.finished` 没 `step.verified` 就重放一次 Verifier；
        连 `step.verified` 都有了就直接用日志里的结论。
        """
        run = self.writer.world.runs[run_id]
        if run.plan is None:
            return CONTINUE
        step_ids = unreconciled_steps(run)
        if not step_ids:
            return CONTINUE
        step = run.plan.by_id()[step_ids[0]]
        state = run.steps[step.id]
        outcome = outcome_from_log(state)
        verdict = None if awaiting_verification(state) else verdict_from_log(state)
        return self._settle(run_id, step, outcome, attempts, verdict=verdict)

    def resume(self, run_id: str, *, approved_steps: frozenset[str] = frozenset()) -> RunOutcome:
        run = self.writer.world.runs[run_id]
        # 终态不可重开，但 resume 应当是幂等的：再调一次只是拿回结果，不再执行任何步骤。
        if run.status in ("completed", "failed", "cancelled"):
            return RunOutcome(run_id=run_id, status=run.status, reason="already_terminal")

        # ① 对账（一）：日志说不清做没做的步骤。必须先于 drive，
        #    否则 drive 会把停在 running 的步骤当成"永远不 ready"，最后给个 run.failed。
        uncertain = reconcile_in_doubt(
            run, self.registry, writer=self.writer, approved=approved_steps
        )
        if uncertain:
            return self._escalate_in_doubt(run_id, run, uncertain)

        # ② 对账（二）：重采一次环境指纹。声明了 env_probes 的步骤逐项比自己的探针，
        #    其余步骤比整树；只失效真的受影响的已通过步骤。
        self._adopt_epoch(run_id, run)

        # ③ 人工修好之后：被"确定性错误"打过回票（或被漂移作废）的步骤要重新排队，
        #    否则它会永远停在 failed / stale。
        for step_id in approved_steps:
            state = run.steps.get(step_id)
            if state is not None and state.status in ("failed", "stale"):
                self.writer.step_to(run_id, step_id, "pending", "operator_retry")

        self.writer.emit(EventType.RUN_RESUMED, {"from_seq": self.writer.world.seq}, run_id=run_id)
        return self.drive(run_id, approved=approved_steps)

    # -------------------------------------------------------------- helpers --
    def _snapshot(self) -> Snapshot | None:
        """采一次 workspace 指纹。没有 workspace 就返回 None——不猜"没变"。"""
        workspace = self.workspace
        if workspace is None or not Path(workspace).is_dir():
            return None
        return snapshot_workspace(workspace)

    def _probe_baseline(self, plan: Plan) -> dict[str, dict[str, str]]:
        """冻结时每步一份 probe 指纹：`{step_id: {声明路径: sha256}}`。"""
        workspace = self.workspace
        if workspace is None:
            return {}
        return {
            step.id: probe_fingerprints(workspace, step.env_probes)
            for step in plan.steps
            if step.env_probes
        }

    def _probe_now(self, run: Any) -> dict[str, dict[str, str]]:
        """恢复时现算一遍 probe 指纹（只 stat/hash 计划声明的那几个文件）。"""
        workspace = self.workspace
        if workspace is None or run.plan is None:
            return {}
        return {
            step.id: probe_fingerprints(workspace, step.env_probes)
            for step in run.plan.steps
            if step.env_probes
        }

    def _adopt_epoch(self, run_id: str, run: Any) -> list[str]:
        """恢复前采指纹：记 `env.fingerprinted`，只失效真正受影响的已通过步骤。

        两条判据并行：
        - 声明了 `env_probes` 的步骤：逐项比它自己探测的文件（改别的文件不算它变）；
        - 其余步骤：比整树指纹 + 是否与 workspace 耦合。
        """
        snap = self._snapshot()
        if snap is None:
            return []
        previous = run.env_fp
        # 没有冻结指纹（旧日志）时按"变了"处理：不能证明没变，就不该复用结论。
        workspace_changed = (not previous) or previous != snap.fp_hash
        changed_probes = probe_drift(run, self._probe_now(run))
        changed = workspace_changed or bool(changed_probes)
        victims = (
            drift_victims(
                run, self.registry,
                workspace_changed=workspace_changed, changed_probes=changed_probes,
                current_tree_fp=snap.fp_hash,
                step_end_fps={sid: s.end_fp for sid, s in run.steps.items() if s.end_fp},
            )
            if changed
            else []
        )
        self.writer.emit(
            EventType.ENV_FINGERPRINTED,
            {
                "fp_hash": snap.fp_hash, "changed": changed, "epoch": run.epoch + 1,
                "previous": previous, "mode": snap.mode, "files": snap.files,
                "truncated": snap.truncated, "invalidated": victims,
                "probe_drift": changed_probes,
            },
            run_id=run_id,
        )
        for step_id in victims:
            detail = {"changed_paths": changed_probes[step_id]} if step_id in changed_probes else None
            invalidate(self.writer, run_id, step_id, detail=detail)
        return victims

    def _pending_token_for(self, run: Any, step_id: str) -> str | None:
        """这个 step 是不是已经在等同一个决定？是就返回旧 token（resume 不许刷 token）。"""
        approval = run.approval or {}
        if approval.get("step_id") == step_id and approval.get("token"):
            return str(approval["token"])
        return None

    def _request_approval(self, run_id: str, step: Step, *, note: str = "") -> str:
        """把「要执行什么」连同 payload 摘要一起交给人；返回一次性 token。"""
        context = f"{step.tool} {_brief(step.args)}"
        if note == "payload_changed":
            context += "（批准之后要执行的参数变了，重新确认一次）"
        elif note == "payload_unverifiable":
            context += "（批准时没留下可核对的摘要，重新确认一次）"
        event("approval requested", run=run_id, step=step.id, tool=step.tool)
        return self.human.request(run_id, step.id, payload_hash=_payload_hash(step), context=context)

    def _stale_approval(self, run: Any, step: Step) -> dict[str, Any] | None:
        """批准时的摘要 vs 现在的 payload。返回 None 表示"就是批准的那一次"。

        两条拒绝理由都要如实写清：
        - `payload_changed`：两个摘要都可比对，但不一样（批准的是 A、执行的是 B）；
        - `payload_unverifiable`：批准时压根没留下可比对的摘要 → fail-closed，不执行。
        """
        state = run.steps.get(step.id)
        approved_hash = None if state is None else state.approved_hash
        current = _payload_hash(step)
        if _is_digest(approved_hash) and approved_hash == current:
            return None
        return {
            "token": (run.approval or {}).get("token", ""),
            "step_id": step.id,
            "applied": False,
            "duplicate": False,
            "decision": "approve",
            "reason": "payload_changed" if _is_digest(approved_hash) else "payload_unverifiable",
            "approved_hash": approved_hash or "",
            "current_hash": current,
        }

    def _escalate_in_doubt(self, run_id: str, run: Any, step_ids: list[str]) -> RunOutcome:
        """不可幂等的 in-doubt：不猜，问人。

        已经在等同一个 step 的回答时不再发 token——否则每次 resume 都多一个 token，
        「再点一次继续」就变成了刷 token（resume 必须幂等）。
        """
        for step_id in step_ids:
            token = self._pending_token_for(run, step_id)
            if token is not None:
                return RunOutcome(run_id=run_id, status="waiting_human", reason=f"token={token}")

        step_id = step_ids[0]
        step = run.plan.by_id().get(step_id) if run.plan is not None else None
        attempt = last_attempt(run, step_id)
        context = "这一步可能已经执行过，无法确认，要继续还是跳过？"
        if step is not None:
            context = f"{step.tool} {_brief(step.args)}：" + context
        if attempt is not None:
            context += (
                f"（上次尝试 #{attempt['attempt']}，"
                f"幂等键 {str(attempt['idempotency_key'])[:12]}）"
            )
        token = self.human.request(
            run_id, step_id, _payload_hash(step) if step is not None else "", context=context
        )
        return RunOutcome(run_id=run_id, status="waiting_human", reason=f"token={token}")

    def _wants_human(self, step: Step, error_code: str | None) -> bool:
        """哪些确定性错误值得打扰人：计划自己声明要问的，以及"环境坏了人能修"的。

        越权/参数非法不在此列——那是策略拒绝，问人也没用，直接失败更诚实。
        """
        if step.on_fail is OnFail.ASK_USER:
            return True
        return error_code in {"permission_denied", "command_not_found"}

    def _next_step(self, run_id: str) -> Step | None:
        run = self.writer.world.runs[run_id]
        if run.plan is None:
            return None
        for step in run.plan.order():
            state = run.steps[step.id]
            if state.status != "pending":
                continue
            if all(run.steps[d].status == "passed" for d in step.depends_on):
                return step
        return None

    def _classify(self, outcome: StepOutcome, verdict: VerifyOutcome) -> FailureClass:
        if not outcome.ok:
            return classify_error(outcome.error_code)
        if not verdict.passed:
            return FailureClass.ASSERTION
        return FailureClass.ASSERTION

    def _apply_repair(self, run_id: str, step_id: str, new_plan: Plan) -> bool:
        """复核并应用修复计划；不合法就**不应用、不记修复次数**，返回 ``False``。

        模型只能提出计划，不能改执行边界（D-026）：应用前用 registry 的 ToolSpecs
        跑一遍 ``check_plan``（工具已注册、每步有断言、``needs ⊆ observations``、
        无环、步数上限）。复核不过等同"修不了"，由调用方走既有失败路径，不重试。
        没有 registry 时无法复核 → fail-closed，也不应用。
        """
        run = self.writer.world.runs[run_id]
        if run.plan is None or self.registry is None or not isinstance(new_plan, Plan):
            return False
        try:
            specs = {name: self.registry.get(name) for name in self.registry.names()}
        except (KeyError, TypeError):
            return False

        # 修订版计划由**这里**升 version（旧 fold 的 run.version += 1 语义）。
        amended = dataclasses.replace(new_plan, version=run.plan.version + 1)
        try:
            issues = check_plan(amended, specs, max_steps=self.budget.max_steps)
        except (AttributeError, KeyError, TypeError, ValueError):
            # 模型给了 plancheck 都读不下去的形状：同样按"修不了"处理。
            return False
        if issues:
            return False
        try:
            plan_hash = amended.hash()
        except (TypeError, ValueError):
            # 连 plan_hash 都算不出来（args 不可 JSON 编码）：不是可应用的计划。
            return False

        invalidated = self._invalidated_steps(run, run.plan, amended, step_id)
        self.writer.emit(
            EventType.PLAN_AMENDED,
            {
                "plan": plan_to_dict(amended),
                "plan_hash": plan_hash,
                "version": amended.version,
                "reason": "assertion_repair",
                "invalidated": invalidated,
            },
            run_id=run_id,
        )
        event(
            "plan amended", run=run_id, step=step_id, version=amended.version,
            invalidated=invalidated or "-",
        )
        for sid in invalidated:
            status = run.steps[sid].status
            if status == "passed":
                self.writer.step_to(run_id, sid, "stale", "upstream_changed")
                self.writer.step_to(run_id, sid, "pending", "invalidated")
            elif status == "failed":
                self.writer.step_to(run_id, sid, "pending", "repaired")
        return True

    def _invalidated_steps(
        self, run: Any, old_plan: Plan, new_plan: Plan, failed_step_id: str
    ) -> list[str]:
        """修订计划后**必须重跑**的步骤：键变了 / 新加的步骤 + 它们的传递下游。

        复用键由 `harness.repair.reuse_key` 计算（D-027）：
        - 旧键：旧 step 的 ``depends_on`` + 投影里现有的 ``artifact_refs``；
        - 新键：新 step 的 ``depends_on`` + 投影里现有的 ``artifact_refs``
          （新加的依赖还没有产物，按空元组算）。

        因此：
        - 键没变且旧状态是 ``passed`` 的步骤不进列表，保持 passed / 产物 / 观测不动；
        - 失败步**总是 root**：即使键没变也要重跑（failed → pending），
          并把它在新计划里的传递下游一起失效——失败步重跑可能产出不同产物，
          下游继续复用就是把旧产物当成了新事实；
        - 键变了/new 的步骤同样把下游一起拖下水。
        """

        states = run.steps
        old_keys = {
            sid: reuse_key(step, _upstream_refs(step, states))
            for sid, step in old_plan.by_id().items()
        }
        new_keys = {
            sid: reuse_key(step, _upstream_refs(step, states))
            for sid, step in new_plan.by_id().items()
        }
        roots = {sid for sid, key in new_keys.items() if old_keys.get(sid) != key}
        if failed_step_id in new_keys:
            roots.add(failed_step_id)

        children: dict[str, list[str]] = {}
        for step in new_plan.steps:
            for dep in step.depends_on:
                children.setdefault(dep, []).append(step.id)

        affected = set(roots)
        stack = list(roots)
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in affected:
                    affected.add(child)
                    stack.append(child)

        return [step.id for step in new_plan.order() if step.id in affected]

    def _report_artifact_id(self, result: Result) -> str | None:
        """缺口 5：报告全文落产物库，事件里留 artifact_id；未注入则跳过。"""
        if self.artifacts is None:
            return None
        return self.artifacts.put_text(result.summary).artifact_id

    def _summarize(self, run: Any, result: Result, *, note: str = "") -> Result:
        """收尾总结（D-042）：模型复述事实包，**只替换 summary 文本**。

        I3 的边界在这里是机械的：返回的是 ``dataclasses.replace`` 出来的新 Result，
        判定字段（verdict / steps / remaining_plan / budget_used）逐项来自受信组件，
        模型写不出相反的结论；失败/空输出退回模板摘要（warn 一行，不重试、不编）。
        """
        if self.summarizer is None:
            return result
        try:
            draft = self.summarizer.summarize(_summary_facts(run, result, note=note))
        except Exception as exc:  # noqa: BLE001 - 总结是装饰性文本：失败不能拖垮终态
            warn("summary fallback", run=run.run_id, error=f"{type(exc).__name__}: {exc}")
            return result
        text = draft.strip() if isinstance(draft, str) else ""
        if not text:
            reason = "empty draft" if isinstance(draft, str) else "no draft"
            warn("summary fallback", run=run.run_id, error=reason)
            return result
        # 模型写的总结也是"对外的回答文本"：走一遍 reply 合规扫描（T3 口径）——
        # 命中指令性表述/无来源数字声明就退回模板，不让它绕过闸门。
        violations = _scan_reply(text)
        if violations:
            warn(
                "summary rejected", run=run.run_id,
                codes=[getattr(v, "code", "?") for v in violations],
            )
            return result
        event("summary drafted", run=run.run_id, chars=len(text))
        return dataclasses.replace(result, summary=text, generated_by="template+model")

    def _finish(self, run_id: str) -> RunOutcome:
        run = self.writer.world.runs[run_id]
        remaining = [s.id for s in run.steps.values() if s.status != "passed"]
        if remaining:
            return self._fail(run_id, f"unfinished:{','.join(remaining)}")
        result = self.reporter.render(run, status_override="completed")
        # D-042：拿得到收尾总结就换掉模板摘要；拿不到就是现在的样子。
        result = self._summarize(run, result)
        self.writer.emit(
            EventType.REPORT_RENDERED,
            {"result": _result_dict(result), "artifact_id": self._report_artifact_id(result)},
            run_id=run_id,
        )
        self.writer.emit(EventType.RUN_COMPLETED, {"result": _result_dict(result)}, run_id=run_id)
        event("run COMPLETED", run=run_id, summary=result.summary)
        return RunOutcome(run_id=run_id, status="completed", result=result)

    def _fail(self, run_id: str, reason: str) -> RunOutcome:
        run = self.writer.world.runs[run_id]
        # 修复预算耗尽：把"试了几次、最后一次为什么失败、要转人工"写进失败原因与
        # 报告摘要。报告是给人看的最后一条事实；run 随后落 failed，绝不再发审批事件。
        note = ""
        exhausted_step = _repair_budget_step(reason)
        if exhausted_step is not None:
            reason, note = _repair_budget_failure(
                run, exhausted_step, self.budget.max_repairs
            )
        run.fail_reason = reason
        result = self.reporter.render(run, status_override="failed")
        if note:
            result = dataclasses.replace(result, summary=f"{result.summary}；{note}")
        # D-042：「转人工」这类运行侧补的说明以 note 进事实包，总结不许把它吞掉。
        result = self._summarize(run, result, note=note)
        self.writer.emit(
            EventType.REPORT_RENDERED,
            {"result": _result_dict(result), "artifact_id": self._report_artifact_id(result)},
            run_id=run_id,
        )
        self.writer.emit(EventType.RUN_FAILED, {"reason": reason, "result": _result_dict(result)}, run_id=run_id)
        fail("run FAILED", run=run_id, reason=reason, summary=result.summary)
        return RunOutcome(run_id=run_id, status="failed", result=result, reason=reason)


def _upstream_refs(step: Step, states: Mapping[str, Any]) -> tuple[str, ...]:
    """一步的上游产物引用：按 ``depends_on`` 从投影里收集，顺序留给 ``reuse_key`` 排。"""

    refs: list[str] = []
    for dep in step.depends_on:
        state = states.get(dep)
        if state is not None:
            refs.extend(state.artifact_refs or ())
    return tuple(refs)


def _repair_budget_step(reason: str) -> str | None:
    """从 ``fail:repair_budget_exhausted:<step_id>`` 里取失败步；不是这个原因返回 None。"""

    prefix = f"{REPAIR_BUDGET_EXHAUSTED}:"
    if not reason.startswith(prefix):
        return None
    step_id = reason[len(prefix):].strip()
    return step_id or None


def _repair_budget_failure(run: Any, step_id: str, max_repairs: int) -> tuple[str, str]:
    """修复预算耗尽的可读事实：失败原因 + 报告摘要里那一行（含尝试次数与最后原因）。"""

    state = run.steps.get(step_id)
    attempts = int(getattr(state, "attempts", 0) or 0)
    last = str(
        getattr(state, "verified_reason", "")
        or getattr(state, "error_code", "")
        or ""
    ).strip()
    used = int(run.budget.get("repairs", 0) or 0)
    last_text = _clip_text(last, 300) if last else "（无记录）"
    reason = (
        f"{REPAIR_BUDGET_EXHAUSTED}:{step_id}:attempts={attempts}:"
        f"repairs={used}/{max_repairs}:last={last_text}"
    )
    summary = (
        f"修复预算已用尽（{used}/{max_repairs} 次），转人工；"
        f"{step_id} 共尝试 {attempts} 次；最后一次失败：{last_text}"
    )
    return reason, summary


def _executor_workspace(executor: Any) -> Path | None:
    """执行器知道自己作用在哪个 workspace；Runner 借它采环境指纹，少一个装配参数。"""
    raw = getattr(executor, "workspace", None)
    return None if raw is None else Path(raw)


def _payload_hash(step: Step) -> str:
    """「批准的是这一次的 (tool, args)」的那个摘要。同一表达式算两次才有得比。"""
    return digest_of({"tool": step.tool, "args": step.args})


def _is_digest(value: str | None) -> bool:
    """是不是 `digest_of` 会产出的 sha256 摘要。

    不是的话（旧日志、手工事件、外部伪造）就不存在"可比对的批准"：
    执行前复核按 fail-closed 处理——不执行，重新问一次。
    """
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _brief(args: dict[str, Any], limit: int = 200) -> str:
    """给人工确认看的一行参数摘要。太长就截断，但绝不省略命令本身。"""

    try:
        text = json.dumps(args, ensure_ascii=False)
    except Exception:
        text = str(args)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _clip_text(text: str, limit: int) -> str:
    """把文本压到 ``limit`` 个字符以内（含固定截断标注）。"""

    body = str(text)
    if len(body) <= limit:
        return body
    if limit <= len(_REPAIR_TRUNCATED):
        return _REPAIR_TRUNCATED[:limit]
    return body[: limit - len(_REPAIR_TRUNCATED)] + _REPAIR_TRUNCATED


def _value_text(value: Any) -> str:
    """观测值 → 短文本；容器先 JSON 化，序列化不了就 ``repr``。"""

    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr 已兜住绝大多数
        return repr(value)


def _bounded_observations(observations: dict[str, Any]) -> dict[str, str]:
    """实际观测的有界摘要：最多 8 个字段、每值 120 字符、JSON 合计 ≤ 1000 字符。

    只给修复器"实际值"这一项事实，不给全量事件历史；也不把大载荷整包塞进 prompt。
    键名截到 40 字符，保证单条记录的 JSON 不会因为键名超长而突破总上限。
    """

    bounded: dict[str, str] = {}
    for key, value in list((observations or {}).items())[:REPAIR_OBSERVATION_FIELDS]:
        bounded[_clip_text(str(key), 40)] = _clip_text(
            _value_text(value), REPAIR_OBSERVATION_VALUE_CHARS
        )
        try:
            size = len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):  # pragma: no cover - 值已经全是字符串
            size = REPAIR_OBSERVATIONS_CHARS + 1
        if size > REPAIR_OBSERVATIONS_CHARS:
            bounded.popitem()
            break
    return bounded


#: 事实包里最多带几步（只取前 N 步 + 省略计数）：总结是复述，不是审计报告。
SUMMARY_MAX_STEPS = 12
#: 事实包里单条文字（失败原因 / 目标 / note）的上限。
SUMMARY_TEXT_CHARS = 300


def _summary_facts(run: Any, result: Result, *, note: str = "") -> dict[str, Any]:
    """收尾总结的事实包（D-042）：只装受信组件已有的事实，不装推断。

    来源与边界：

    - ``verdict`` / ``remaining_plan`` / ``budget_used`` 来自 ``Result``（代码判定）；
    - 每步的 status / attempts / failure_class / error_code / verified_reason 来自
      事件投影（``RunState``）；
    - observations 走 ``_bounded_observations``（与修复事实同一量级：8 个字段 ×
      120 字符）：总结要的是"做出了什么结果"，不是把整份 stdout 塞进 prompt。

    模型只拿到这些东西，所以它写不出事实之外的新数字或相反的判定。
    """

    plan_steps = run.plan.by_id() if run.plan is not None else {}
    states = run.step_list()
    steps: list[dict[str, Any]] = []
    for state in states:
        step = plan_steps.get(state.id)
        steps.append(
            {
                "id": state.id,
                "tool": getattr(step, "tool", "") or "",
                "status": state.status,
                "attempts": state.attempts,
                "failure_class": state.failure_class or "",
                "error_code": state.error_code or "",
                "reason": _clip_text(state.verified_reason or "", SUMMARY_TEXT_CHARS),
                "observations": _bounded_observations(state.observations),
            }
        )
    passed = sum(1 for state in states if state.status == "passed")
    return {
        "run_id": run.run_id,
        "verdict": result.verdict.value,
        "kind": run.kind,
        "steps_total": len(states),
        "steps_passed": passed,
        "steps": steps[:SUMMARY_MAX_STEPS],
        "steps_omitted": max(0, len(steps) - SUMMARY_MAX_STEPS),
        "fail_reason": _clip_text(run.fail_reason or "", SUMMARY_TEXT_CHARS),
        "note": _clip_text(note, SUMMARY_TEXT_CHARS),
        "remaining_plan": list(result.remaining_plan),
        "budget_used": dict(result.budget_used),
    }


def has_model_summary(result: Any) -> bool:
    """``Result.summary`` 是不是已经由模型写过（D-042）。

    ``Reporter`` 的纯模板产出记 ``generated_by="template"``；收尾总结落地后记
    ``"template+model"``。会话层据此决定还要不要做证据收尾（run 层写过就不再写）。
    """

    return str(getattr(result, "generated_by", "template")) != "template"


def _scan_reply(text: str) -> tuple[Any, ...]:
    """reply 合规扫描（惰性 import：gate 依赖 contracts，不反向依赖 app）。"""

    from ..gate.rules import scan_reply

    return scan_reply(text)


def _result_dict(result: Result) -> dict[str, Any]:
    return {
        "run_id": result.run_id,
        "verdict": result.verdict.value,
        "steps": [
            {
                "id": s.id, "status": s.status, "artifact_refs": list(s.artifact_refs),
                "checks": list(s.checks), "evidence_seqs": list(s.evidence_seqs),
            }
            for s in result.steps
        ],
        "summary": result.summary,
        "remaining_plan": list(result.remaining_plan),
        "budget_used": dict(result.budget_used),
        "generated_by": result.generated_by,
    }
