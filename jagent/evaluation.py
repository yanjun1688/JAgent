"""JAgent v8 · 评估投影（D-035，docs/jagent-v8-evaluation.md）。

**纯派生，零写事件**：评估是离线工具面——评分只读事件流 + fold 投影，
与 ``metrics.py`` / ``audit.py`` 同款的中立模块口径，不在运行时组件图上
（先例 ``scripts/verify_run.py``）。判定全部是确定性代码：任务数据只声明
``kind + params``，判定由本模块的 kind registry 实现（I3 / I6）——未知
kind、未知参数、缺必填参数一律拒跑，没有"跳过坏任务继续"的分支。

任务集（``jagent/config/eval/``）是数据、进 config hash；对照以
``eval_tasks_hash`` 为对齐键，不同语料拒绝对比。评估分数不进事件流
（brief §5.5："评估分数只是工具产出的一个观测"），报告由 runner 落
``data/eval/``。
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .audit import run_verification
from .contracts import Event, EventType
from .harness.fold import fold
from .metrics import run_metrics, session_metrics

#: 任务集 schema 版本；不匹配即拒跑。
EVAL_SCHEMA = "eval.tasks.v1"

_RUN_EXPECT = ("completed", "failed", "cancelled")
_AT_CHOICES = ("all", "last")
_TURN_ACTIONS = ("approve", "reject", "cancel")
#: judge 四态（D-021）。judge 没有 PASS（I3）——这个列表里故意没有它。
_JUDGE_VERDICTS = ("reject", "undecidable", "unavailable", "none")
#: 四维指标键（路线图 15 §2.5 / 14 §2.4）：质量由 checks 承载，这里只放
#: 成本 / 效率 / 人工介入的度量值。
FOUR_DIM_KEYS = (
    "tokens_in",
    "tokens_out",
    "model_calls",
    "turns",
    "runs",
    "steps_passed",
    "wall_s",
    "approvals",
)

_TERMINAL = frozenset(
    {EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED}
)


class EvalError(ValueError):
    """任务集 / 对照输入非法：拒跑，没有"用默认值继续"的分支（I6 同款）。"""


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckDef:
    """一个确定性断言：kind 指向 registry，params 已通过校验。"""

    check_id: str
    kind: str
    params: dict[str, Any]


@dataclass(frozen=True)
class TaskDef:
    """一个评估任务：turns 是激励，checks 是判定，全部来自数据。"""

    task_id: str
    description: str
    model: str  # runner 替身脚本库的键；空串 = "default"
    k: int
    tags: tuple[str, ...]
    workspace: dict[str, str]
    turns: tuple[dict[str, Any], ...]  # {"text"[, "answers"]} 或 {"action"}
    checks: tuple[CheckDef, ...]


@dataclass(frozen=True)
class EvalCorpus:
    schema: str
    tasks: tuple[TaskDef, ...]
    tasks_hash: str  # tasks.toml 原始字节的 sha256（对照的对齐键）


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    kind: str
    passed: bool
    observed: Any
    evidence: dict[str, Any]  # {"seqs": [...], "run_ids": [...]}——数据诚实


@dataclass(frozen=True)
class AttemptScore:
    verdict: str  # pass / fail
    checks: tuple[CheckResult, ...]
    metrics: dict[str, Any]  # 四维（FOUR_DIM_KEYS）
    problems: tuple[str, ...]  # runner 级不变量失败原因（重放不一致等）


@dataclass(frozen=True)
class TaskScore:
    task_id: str
    verdict: str  # passed / failed（pass^k）
    k: int
    attempts: tuple[AttemptScore, ...]


# --------------------------------------------------------------------------- #
# 任务集解析（fail-closed）
# --------------------------------------------------------------------------- #

_TASK_KEYS = frozenset(
    {"description", "model", "k", "tags", "workspace", "turns", "checks"}
)


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise EvalError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_tasks(path: str | Path) -> EvalCorpus:
    """读任务集 TOML 并算 ``tasks_hash``（原始字节 sha256）。缺文件即拒跑。"""
    path = Path(path)
    if not path.is_file():
        raise EvalError(f"评估任务集不存在：{path}")
    try:
        raw = path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise EvalError(f"无法读取任务集 {path}：{exc}") from exc
    return tasks_from_mapping(data, tasks_hash=hashlib.sha256(raw).hexdigest())


def tasks_from_mapping(data: Any, *, tasks_hash: str = "") -> EvalCorpus:
    """从已解析的映射构建语料；schema / 字段 / kind / 参数全量校验。"""
    _require(isinstance(data, dict), "任务集顶层必须是表")
    _require(
        data.get("schema") == EVAL_SCHEMA,
        f"schema 必须是 {EVAL_SCHEMA!r}，读到 {data.get('schema')!r}",
    )
    unknown_top = sorted(set(data) - {"schema", "task"})
    _require(not unknown_top, f"任务集存在未知顶层字段：{unknown_top}")
    raw_tasks = data.get("task")
    _require(isinstance(raw_tasks, dict) and raw_tasks, "任务集为空：至少要有一个 [task.*]")
    tasks = tuple(_parse_task(task_id, raw) for task_id, raw in raw_tasks.items())
    return EvalCorpus(schema=EVAL_SCHEMA, tasks=tasks, tasks_hash=tasks_hash)


def _parse_task(task_id: str, raw: Any) -> TaskDef:
    _require(isinstance(raw, dict), f"[task.{task_id}] 必须是表")
    unknown = sorted(set(raw) - _TASK_KEYS)
    _require(not unknown, f"[task.{task_id}] 存在未知字段：{unknown}")
    description = raw.get("description")
    _require(
        isinstance(description, str) and description.strip(),
        f"[task.{task_id}] 缺 description",
    )
    model = raw.get("model", "")
    if "model" in raw:
        _require(
            isinstance(model, str) and bool(model) and model.strip() == model,
            f"[task.{task_id}] model 必须是非空、不带空白的字符串：{model!r}",
        )
    k = raw.get("k", 1)
    _require(_is_int(k) and k >= 1, f"[task.{task_id}] k 必须是 ≥1 的整数")
    tags_raw = raw.get("tags", [])
    _require(
        isinstance(tags_raw, (list, tuple))
        and all(isinstance(t, str) and t for t in tags_raw),
        f"[task.{task_id}] tags 必须是非空字符串列表",
    )
    workspace_raw = raw.get("workspace", {})
    _require(isinstance(workspace_raw, dict), f"[task.{task_id}] workspace 必须是表")
    workspace: dict[str, str] = {}
    for name, content in workspace_raw.items():
        _require(isinstance(content, str), f"[task.{task_id}] workspace {name!r} 的值必须是字符串")
        _require(
            isinstance(name, str) and name and "\\" not in name,
            f"[task.{task_id}] workspace 路径必须是 posix 风格相对路径：{name!r}",
        )
        parts = PurePosixPath(name).parts
        _require(
            not PurePosixPath(name).is_absolute() and parts and ".." not in parts,
            f"[task.{task_id}] workspace 路径不允许绝对路径或上跳：{name!r}",
        )
        workspace[name] = content
    turns = _parse_turns(task_id, raw.get("turns"))
    checks = _parse_checks(task_id, raw.get("checks"))
    return TaskDef(
        task_id=task_id,
        description=description,
        model=model,
        k=k,
        tags=tuple(tags_raw),
        workspace=workspace,
        turns=turns,
        checks=checks,
    )


def _parse_turns(task_id: str, turns_raw: Any) -> tuple[dict[str, Any], ...]:
    _require(
        isinstance(turns_raw, (list, tuple)) and turns_raw, f"[task.{task_id}] 缺 turns"
    )
    turns: list[dict[str, Any]] = []
    for index, turn in enumerate(turns_raw):
        label = f"[task.{task_id}] turns[{index}]"
        if isinstance(turn, str):
            _require(turn.strip(), f"{label} 不能是空字符串")
            turns.append({"text": turn})
        elif isinstance(turn, dict):
            if set(turn) == {"action"}:
                action = turn["action"]
                _require(action in _TURN_ACTIONS, f"{label} 未知动作：{action!r}")
                turns.append({"action": action})
                continue
            _require(
                "text" in turn and set(turn) <= {"text", "answers"},
                f"{label} 文本形式只能是 text + answers（或动作只用 action）",
            )
            text = turn["text"]
            _require(isinstance(text, str) and text.strip(), f"{label} text 不能为空")
            answers = turn.get("answers", {})
            _require(
                isinstance(answers, dict)
                and all(isinstance(k, str) and k for k in answers)
                and all(isinstance(v, str) for v in answers.values()),
                f"{label} answers 必须是字符串到字符串的映射",
            )
            turns.append({"text": text, "answers": dict(answers)})
        else:
            raise EvalError(f"{label} 必须是字符串、动作表或文本表")
    return tuple(turns)


def _parse_checks(task_id: str, checks_raw: Any) -> tuple[CheckDef, ...]:
    _require(
        isinstance(checks_raw, (list, tuple)) and checks_raw,
        f"[task.{task_id}] 缺 checks",
    )
    checks: list[CheckDef] = []
    for index, raw in enumerate(checks_raw):
        label = f"[task.{task_id}] checks[{index}]"
        _require(isinstance(raw, dict), f"{label} 必须是表")
        kind = raw.get("kind")
        spec = _KIND_SPECS.get(kind)
        _require(spec is not None, f"{label} 未知 kind：{kind!r}")
        params = {key: value for key, value in raw.items() if key != "kind"}
        unknown = sorted(set(params) - spec.required - spec.optional)
        _require(not unknown, f"{label}（{kind}）存在未知参数：{unknown}")
        missing = sorted(spec.required - set(params))
        _require(not missing, f"{label}（{kind}）缺必填参数：{missing}")
        spec.validate(params)
        checks.append(CheckDef(check_id=f"check{index + 1}", kind=kind, params=params))
    return tuple(checks)


# --------------------------------------------------------------------------- #
# kind 参数校验（值域在这里冻结，评分时参数视为可信）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _KindSpec:
    required: frozenset[str]
    optional: frozenset[str]
    validate: Any  # Callable[[dict], None]


def _validate_no_params(params: dict[str, Any]) -> None:
    _require(not params, "这个 kind 不接受参数")


def _validate_at(params: dict[str, Any]) -> None:
    if "at" in params:
        _require(params["at"] in _AT_CHOICES, f"at 必须是 {'/'.join(_AT_CHOICES)}")


def _validate_run_verdict(params: dict[str, Any]) -> None:
    _require(
        params.get("expect") in _RUN_EXPECT,
        f"run_verdict.expect 必须是 {'/'.join(_RUN_EXPECT)}",
    )
    _validate_at(params)


def _validate_turn_outcome(params: dict[str, Any]) -> None:
    _require(
        isinstance(params.get("expect"), str) and params["expect"],
        "turn_outcome.expect 必须是非空字符串",
    )
    _validate_at(params)


def _validate_steps(params: dict[str, Any]) -> None:
    _require(
        _is_int(params.get("count")) and params["count"] >= 0,
        "steps_passed_min.count 必须是非负整数",
    )


def _validate_min_max(params: dict[str, Any]) -> None:
    declared = False
    for key in ("min", "max"):
        if key in params:
            _require(_is_int(params[key]) and params[key] >= 0, f"{key} 必须是非负整数")
            declared = True
    _require(declared, "至少声明 min / max 之一")


def _validate_budget(params: dict[str, Any]) -> None:
    bounds = ("tokens_in", "tokens_out", "model_calls", "wall_s")
    _require(any(key in params for key in bounds), "budget_within 至少声明一个上限")
    for key in bounds:
        if key in params:
            _require(_is_num(params[key]) and params[key] >= 0, f"{key} 必须是非负数")


def _validate_advice(params: dict[str, Any]) -> None:
    if "mode" in params:
        _require(isinstance(params["mode"], str) and params["mode"], "mode 必须是非空字符串")
    for key in ("fallback", "partial"):
        if key in params:
            _require(isinstance(params[key], bool), f"{key} 必须是布尔值")
    if "min_citations" in params:
        _require(
            _is_int(params["min_citations"]) and params["min_citations"] >= 0,
            "min_citations 必须是非负整数",
        )


def _validate_gate(params: dict[str, Any]) -> None:
    if "pass" in params:
        _require(isinstance(params["pass"], bool), "pass 必须是布尔值")
    if "mode" in params:
        _require(isinstance(params["mode"], str) and params["mode"], "mode 必须是非空字符串")
    if "judge" in params:
        _require(
            params["judge"] in _JUDGE_VERDICTS,
            f"judge 必须是 {'/'.join(_JUDGE_VERDICTS)}（judge 没有 PASS，I3）",
        )
    for key in ("rules_present", "rules_absent"):
        if key in params:
            value = params[key]
            _require(
                isinstance(value, list)
                and bool(value)
                and all(isinstance(item, str) and item for item in value),
                f"{key} 必须是非空的 rule_id 列表",
            )


def _validate_event_count(params: dict[str, Any]) -> None:
    _require("type" in params, "event_count 缺 type")
    _require(
        params["type"] in {event.value for event in EventType},
        f"未知事件类型：{params['type']!r}",
    )
    declared = False
    for key in ("min", "max", "exact"):
        if key in params:
            _require(_is_int(params[key]) and params[key] >= 0, f"{key} 必须是非负整数")
            declared = True
    _require(declared, "event_count 至少声明 min / max / exact 之一")


def _validate_step_started_max(params: dict[str, Any]) -> None:
    _require(
        isinstance(params.get("step_id"), str) and params["step_id"],
        "step_started_max.step_id 必须是非空字符串",
    )
    _require(
        _is_int(params.get("max")) and params["max"] >= 0,
        "step_started_max.max 必须是非负整数",
    )


_KIND_SPECS: dict[str, _KindSpec] = {
    "run_verdict": _KindSpec(
        frozenset({"expect"}), frozenset({"at"}), _validate_run_verdict
    ),
    "turn_outcome": _KindSpec(
        frozenset({"expect"}), frozenset({"at"}), _validate_turn_outcome
    ),
    "steps_passed_min": _KindSpec(frozenset({"count"}), frozenset(), _validate_steps),
    "verify_consistent": _KindSpec(frozenset(), frozenset(), _validate_no_params),
    "budget_within": _KindSpec(
        frozenset(),
        frozenset({"tokens_in", "tokens_out", "model_calls", "wall_s"}),
        _validate_budget,
    ),
    "approvals": _KindSpec(frozenset(), frozenset({"min", "max"}), _validate_min_max),
    "clarify_asked": _KindSpec(frozenset(), frozenset({"min", "max"}), _validate_min_max),
    "advice_delivered": _KindSpec(
        frozenset(),
        frozenset({"mode", "fallback", "partial", "min_citations"}),
        _validate_advice,
    ),
    "gate_checked": _KindSpec(
        frozenset(),
        frozenset({"pass", "mode", "judge", "rules_present", "rules_absent"}),
        _validate_gate,
    ),
    "step_started_max": _KindSpec(
        frozenset({"step_id", "max"}), frozenset(), _validate_step_started_max
    ),
    "event_count": _KindSpec(
        frozenset({"type"}), frozenset({"min", "max", "exact"}), _validate_event_count
    ),
    "replay_consistent": _KindSpec(frozenset(), frozenset(), _validate_no_params),
}


# --------------------------------------------------------------------------- #
# attempt 上下文与四维指标
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Attempt:
    events: tuple[Event, ...]
    run_ids: tuple[str, ...]
    terminal: dict[str, str]
    session: dict[str, Any]
    runs: dict[str, dict[str, Any]]
    metrics: dict[str, Any]
    fingerprint: str | None
    replay_ok: bool


def _run_skeleton(events: list[Event]) -> tuple[list[str], dict[str, str]]:
    """run 骨架：started 定集合与顺序，终态事件定 verdict。"""
    run_ids: list[str] = []
    terminal: dict[str, str] = {}
    for event in events:
        if event.type is EventType.RUN_STARTED:
            run_id = str(event.payload.get("run_id") or event.run_id or "")
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
        elif event.type is EventType.RUN_COMPLETED:
            terminal[event.run_id or ""] = "completed"
        elif event.type is EventType.RUN_FAILED:
            terminal[event.run_id or ""] = "failed"
        elif event.type is EventType.RUN_CANCELLED:
            terminal[event.run_id or ""] = "cancelled"
    return run_ids, terminal


def _four_dim(
    events: list[Event],
    session: dict[str, Any],
    runs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """四维指标（成本 / 效率 / 人工介入；质量由 checks 承载）。

    token 归属会话级（D-031 边界：调用多发生在 run 边界外），attempt 级
    成本以本 attempt 会话聚合为准；wall_s 取 attempt 事件流跨度。
    """
    wall = (events[-1].at - events[0].at) if events else 0.0
    return {
        "tokens_in": int(session["tokens_in"]),
        "tokens_out": int(session["tokens_out"]),
        "model_calls": int(session["model_calls"]),
        "turns": int(session["turns"]),
        "runs": int(session["runs"]),
        "steps_passed": sum(
            int(state.get("steps", {}).get("passed", 0)) for state in runs.values()
        ),
        "wall_s": round(max(wall, 0.0), 3),
        "approvals": int(session["human_interventions"]),
    }


def _result(
    check: CheckDef,
    passed: Any,
    observed: Any,
    *,
    seqs: Iterable[int] = (),
    run_ids: Iterable[str] = (),
) -> CheckResult:
    return CheckResult(
        check_id=check.check_id,
        kind=check.kind,
        passed=bool(passed),
        observed=observed,
        evidence={"seqs": list(seqs), "run_ids": list(run_ids)},
    )


# --------------------------------------------------------------------------- #
# checks 实现（全部确定性；判定只读事件，不调模型）
# --------------------------------------------------------------------------- #


def _check_run_verdict(ctx: _Attempt, check: CheckDef) -> CheckResult:
    expect = check.params["expect"]
    at = check.params.get("at", "all")
    runs = list(ctx.run_ids) if at == "all" else list(ctx.run_ids[-1:])
    verdicts = [ctx.terminal.get(run_id) for run_id in runs]
    observed = [verdict if verdict else "none" for verdict in verdicts]
    selected = set(runs)
    seqs = [
        event.seq
        for event in ctx.events
        if event.type in _TERMINAL and event.run_id in selected
    ]
    return _result(
        check,
        bool(runs) and all(verdict == expect for verdict in verdicts),
        observed,
        seqs=seqs,
        run_ids=runs,
    )


def _check_turn_outcome(ctx: _Attempt, check: CheckDef) -> CheckResult:
    expect = check.params["expect"]
    at = check.params.get("at", "all")
    ended = [e for e in ctx.events if e.type is EventType.TURN_ENDED]
    selection = ended if at == "all" else ended[-1:]
    outcomes = [str(e.payload.get("outcome") or "") for e in selection]
    return _result(
        check,
        bool(outcomes) and all(outcome == expect for outcome in outcomes),
        outcomes,
        seqs=[e.seq for e in selection],
    )


def _check_steps_passed_min(ctx: _Attempt, check: CheckDef) -> CheckResult:
    count = check.params["count"]
    last = ctx.run_ids[-1] if ctx.run_ids else None
    view = ctx.runs.get(last, {}) if last else {}
    passed_count = int(view.get("steps", {}).get("passed", 0))
    seqs = [
        event.seq
        for event in ctx.events
        if last is not None
        and event.run_id == last
        and event.type is EventType.STEP_STATE_CHANGED
        and event.payload.get("to") == "passed"
    ]
    return _result(
        check, passed_count >= count, passed_count, seqs=seqs, run_ids=[last] if last else []
    )


def _check_verify_consistent(ctx: _Attempt, check: CheckDef) -> CheckResult:
    """audit.run_verification（D-032）复用：声称 vs 证据。"""
    last = ctx.run_ids[-1] if ctx.run_ids else None
    if last is None:
        return _result(check, False, {"problems": ["attempt 没有 run"]})
    view = run_verification(ctx.events, last)
    problems = list(view.get("problems") or [])
    seqs = [
        event.seq
        for event in ctx.events
        if event.run_id == last and event.type in _TERMINAL
    ]
    return _result(
        check,
        bool(view) and view["consistent"],
        {"claimed": view.get("claimed"), "problems": problems},
        seqs=seqs,
        run_ids=[last],
    )


def _check_budget_within(ctx: _Attempt, check: CheckDef) -> CheckResult:
    observed = {key: ctx.metrics[key] for key in check.params}
    exceeded = {
        key: observed[key]
        for key in check.params
        if key in ctx.metrics and ctx.metrics[key] > check.params[key]
    }
    return _result(
        check,
        not exceeded,
        observed | {"exceeded": sorted(exceeded)},
        seqs=[e.seq for e in ctx.events if e.type is EventType.MODEL_CALLED],
        run_ids=list(ctx.run_ids),
    )


def _make_count_check(event_type: EventType) -> Any:
    def _check(ctx: _Attempt, check: CheckDef) -> CheckResult:
        selected = [e for e in ctx.events if e.type is event_type]
        count = len(selected)
        minimum = check.params.get("min")
        maximum = check.params.get("max")
        passed = (minimum is None or count >= minimum) and (
            maximum is None or count <= maximum
        )
        return _result(
            check,
            passed,
            count,
            seqs=[e.seq for e in selected],
            run_ids=sorted({e.run_id for e in selected if e.run_id}),
        )

    return _check


def _check_advice_delivered(ctx: _Attempt, check: CheckDef) -> CheckResult:
    deliveries = [e for e in ctx.events if e.type is EventType.ADVICE_DELIVERED]
    if not deliveries:
        return _result(check, False, None)
    payload = deliveries[-1].payload
    citations = len((payload.get("evidence") or {}).get("citations") or [])
    observed = {
        "mode": payload.get("mode"),
        "fallback": (payload.get("fallback") or {}).get("bool"),
        "partial": payload.get("partial"),
        "citations": citations,
    }
    params = check.params
    violations = []
    if "mode" in params and observed["mode"] != params["mode"]:
        violations.append("mode")
    if "fallback" in params and observed["fallback"] is not params["fallback"]:
        violations.append("fallback")
    if "partial" in params and observed["partial"] is not params["partial"]:
        violations.append("partial")
    if "min_citations" in params and citations < params["min_citations"]:
        violations.append("min_citations")
    return _result(check, not violations, observed | {"violations": violations}, seqs=[deliveries[-1].seq])


def _check_gate_checked(ctx: _Attempt, check: CheckDef) -> CheckResult:
    gates = [e for e in ctx.events if e.type is EventType.GATE_CHECKED]
    if not gates:
        return _result(check, False, None)
    payload = gates[-1].payload
    violation_ids = [str(v.get("rule_id")) for v in payload.get("violations") or []]
    #: judge 记录缺席按 none 处理（D-021：none = 没配 judge——与"调不通"可区分）。
    judge_verdict = (payload.get("judge") or {}).get("verdict") or "none"
    observed = {
        "pass": payload.get("pass"),
        "mode": payload.get("mode"),
        "judge": judge_verdict,
        "violations": violation_ids,
    }
    params = check.params
    violations = []
    if "pass" in params and observed["pass"] is not params["pass"]:
        violations.append("pass")
    if "mode" in params and observed["mode"] != params["mode"]:
        violations.append("mode")
    if "judge" in params and observed["judge"] != params["judge"]:
        violations.append("judge")
    for rule_id in params.get("rules_present", ()):
        if rule_id not in violation_ids:
            violations.append(f"rules_present:{rule_id}")
    for rule_id in params.get("rules_absent", ()):
        if rule_id in violation_ids:
            violations.append(f"rules_absent:{rule_id}")
    return _result(check, not violations, observed | {"violations": violations}, seqs=[gates[-1].seq])


def _check_step_started_max(ctx: _Attempt, check: CheckDef) -> CheckResult:
    """指定步骤的启动次数上限——复用键回归的信号：修复后未变的步不重跑。"""
    step_id = check.params["step_id"]
    maximum = check.params["max"]
    selected = [
        e
        for e in ctx.events
        if e.type is EventType.STEP_STARTED and e.payload.get("step_id") == step_id
    ]
    return _result(
        check,
        len(selected) <= maximum,
        len(selected),
        seqs=[e.seq for e in selected],
        run_ids=sorted({e.run_id for e in selected if e.run_id}),
    )


def _check_event_count(ctx: _Attempt, check: CheckDef) -> CheckResult:
    etype = check.params["type"]
    selected = [e for e in ctx.events if e.type.value == etype]
    count = len(selected)
    passed = True
    if "exact" in check.params:
        passed = count == check.params["exact"]
    if "min" in check.params:
        passed = passed and count >= check.params["min"]
    if "max" in check.params:
        passed = passed and count <= check.params["max"]
    return _result(
        check,
        passed,
        count,
        seqs=[e.seq for e in selected],
        run_ids=sorted({e.run_id for e in selected if e.run_id}),
    )


def _check_replay_consistent(ctx: _Attempt, check: CheckDef) -> CheckResult:
    return _result(check, ctx.replay_ok, ctx.fingerprint)


_REGISTRY = {
    "run_verdict": _check_run_verdict,
    "turn_outcome": _check_turn_outcome,
    "steps_passed_min": _check_steps_passed_min,
    "verify_consistent": _check_verify_consistent,
    "budget_within": _check_budget_within,
    "approvals": _make_count_check(EventType.APPROVAL_REQUESTED),
    "clarify_asked": _make_count_check(EventType.CLARIFY_ASKED),
    "advice_delivered": _check_advice_delivered,
    "gate_checked": _check_gate_checked,
    "step_started_max": _check_step_started_max,
    "event_count": _check_event_count,
    "replay_consistent": _check_replay_consistent,
}


# --------------------------------------------------------------------------- #
# 评分与聚合
# --------------------------------------------------------------------------- #


def score_attempt(events: Iterable[Event], task: TaskDef) -> AttemptScore:
    """给一次 attempt 的事件流打分：runner 级不变量 + 任务的 checks + 四维。

    重放一致性（I1）在这里复查：fold 两次指纹必须相等——不一致或 fold
    失败时整个 attempt 直接判 fail（problem），不依赖 checks。
    """
    events = list(events)
    problems: list[str] = []
    fingerprint: str | None = None
    try:
        world = fold(events)
        fingerprint = world.fingerprint()
        if fingerprint != fold(events).fingerprint():
            problems.append("replay_inconsistent: 两次 fold 指纹不一致")
    except Exception as exc:  # fold 失败 = 重放性质已破，本身就是事实
        fingerprint = None
        problems.append(f"replay_inconsistent: fold 失败（{exc!r}）")

    run_ids, terminal = _run_skeleton(events)
    session = session_metrics(events)
    runs = {run_id: run_metrics(events, run_id) for run_id in run_ids}
    metrics = _four_dim(events, session, runs)
    ctx = _Attempt(
        events=tuple(events),
        run_ids=tuple(run_ids),
        terminal=terminal,
        session=session,
        runs=runs,
        metrics=metrics,
        fingerprint=fingerprint,
        replay_ok=not problems,
    )
    checks = tuple(_REGISTRY[check.kind](ctx, check) for check in task.checks)
    verdict = "pass" if not problems and all(c.passed for c in checks) else "fail"
    return AttemptScore(verdict=verdict, checks=checks, metrics=metrics, problems=tuple(problems))


def score_task(attempts: Iterable[AttemptScore], task: TaskDef) -> TaskScore:
    """pass^k（路线图 15 §3.1）：连续 k 次全部 pass 才算 passed——pass@k
    会掩盖不稳定性。"""
    attempt_tuple = tuple(attempts)
    verdict = (
        "passed"
        if attempt_tuple and all(a.verdict == "pass" for a in attempt_tuple)
        else "failed"
    )
    return TaskScore(
        task_id=task.task_id, verdict=verdict, k=task.k, attempts=attempt_tuple
    )


def corpus_aggregate(scores: dict[str, TaskScore]) -> dict[str, Any]:
    """语料级聚合：任务 / attempt 双口径通过率。"""
    task_list = list(scores.values())
    tasks_total = len(task_list)
    tasks_passed = sum(1 for score in task_list if score.verdict == "passed")
    attempts_total = sum(len(score.attempts) for score in task_list)
    attempts_passed = sum(
        1 for score in task_list for attempt in score.attempts if attempt.verdict == "pass"
    )
    return {
        "tasks_total": tasks_total,
        "tasks_passed": tasks_passed,
        "task_pass_rate": (tasks_passed / tasks_total) if tasks_total else None,
        "attempts_total": attempts_total,
        "attempts_passed": attempts_passed,
        "attempt_pass_rate": (attempts_passed / attempts_total) if attempts_total else None,
    }


# --------------------------------------------------------------------------- #
# 对照协议（compare）
# --------------------------------------------------------------------------- #


def _task_metrics(entry: dict[str, Any]) -> dict[str, Any]:
    """任务级四维：优先顶层 metrics，否则取最后一个 attempt 的 metrics。"""
    metrics = entry.get("metrics")
    if metrics:
        return dict(metrics)
    attempts = entry.get("attempts") or []
    if attempts:
        return dict((attempts[-1] or {}).get("metrics") or {})
    return {}


def _passed_count(tasks: dict[str, dict[str, Any]]) -> int:
    return sum(1 for entry in tasks.values() if entry.get("verdict") == "passed")


def _rates(tasks: dict[str, dict[str, Any]]) -> tuple[float | None, float | None]:
    total = len(tasks)
    if not total:
        return None, None
    task_rate = _passed_count(tasks) / total
    attempts = [a for entry in tasks.values() for a in (entry.get("attempts") or [])]
    attempt_rate = (
        sum(1 for a in attempts if a.get("verdict") == "pass") / len(attempts)
        if attempts
        else None
    )
    return task_rate, attempt_rate


def _config_diff(fp_a: dict[str, Any], fp_b: dict[str, Any]) -> list[str]:
    """config 清单差异：内容有变（或单边缺失）的文件，归因到具体改动。"""
    files_a = fp_a.get("config_files") or {}
    files_b = fp_b.get("config_files") or {}
    return sorted(
        name for name in set(files_a) | set(files_b) if files_a.get(name) != files_b.get(name)
    )


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """对照两份报告：verdict 变化 + 四维 delta + 指纹差异。

    fail-closed：``eval_tasks_hash`` 不一致即拒绝——不同语料上的分数不可比。
    对照只陈列差异，不产生"通过 / 合入"结论（I7）。
    """
    fp_a = dict(baseline.get("fingerprint") or {})
    fp_b = dict(candidate.get("fingerprint") or {})
    if fp_a.get("eval_tasks_hash") != fp_b.get("eval_tasks_hash"):
        raise EvalError(
            "eval_tasks_hash 不一致：两份报告来自不同任务集，拒绝对比"
            f"（{fp_a.get('eval_tasks_hash')!r} vs {fp_b.get('eval_tasks_hash')!r}）"
        )
    tasks_a: dict[str, dict[str, Any]] = baseline.get("tasks") or {}
    tasks_b: dict[str, dict[str, Any]] = candidate.get("tasks") or {}

    regressions: list[dict[str, Any]] = []
    improvements: list[dict[str, Any]] = []
    for task_id in sorted(set(tasks_a) | set(tasks_b)):
        entry_a = tasks_a.get(task_id)
        entry_b = tasks_b.get(task_id)
        verdict_a = entry_a.get("verdict") if entry_a else None
        verdict_b = entry_b.get("verdict") if entry_b else None
        if verdict_a == "passed" and verdict_b != "passed":
            regressions.append({"task_id": task_id, "from": verdict_a, "to": verdict_b})
        elif verdict_a != "passed" and verdict_b == "passed":
            improvements.append({"task_id": task_id, "from": verdict_a, "to": verdict_b})

    deltas: dict[str, dict[str, Any]] = {}
    for task_id in sorted(set(tasks_a) & set(tasks_b)):
        metrics_a = _task_metrics(tasks_a[task_id])
        metrics_b = _task_metrics(tasks_b[task_id])
        deltas[task_id] = {
            "metrics": {
                key: {
                    "a": metrics_a.get(key),
                    "b": metrics_b.get(key),
                    "delta": (
                        metrics_b[key] - metrics_a[key]
                        if key in metrics_a
                        and key in metrics_b
                        and _is_num(metrics_a[key])
                        and _is_num(metrics_b[key])
                        else None
                    ),
                }
                for key in sorted(set(metrics_a) | set(metrics_b))
            }
        }

    rate_a, attempt_rate_a = _rates(tasks_a)
    rate_b, attempt_rate_b = _rates(tasks_b)
    return {
        "baseline": fp_a,
        "candidate": fp_b,
        "regressions": regressions,
        "improvements": improvements,
        "task_deltas": deltas,
        "config_changed_files": _config_diff(fp_a, fp_b),
        "corpus": {
            "tasks_total": len(set(tasks_a) | set(tasks_b)),
            "tasks_passed": {"a": _passed_count(tasks_a), "b": _passed_count(tasks_b)},
            "task_pass_rate": {"a": rate_a, "b": rate_b},
            "attempt_pass_rate": {"a": attempt_rate_a, "b": attempt_rate_b},
        },
    }
