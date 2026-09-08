"""v3.4 (F-4/F-5/F-6): trusted recovery core — pure functions, no I/O and no LLM.

The *enforcement* of recovery boundaries lives here (AGENTS.md §2.2: 强制权归受信组件).
The Agent/LLM may *propose* repairs; these functions decide mechanically whether a
proposal is within budget and whether a revision actually addresses previously
failed steps. Nothing here calls an LLM or performs side effects.

- F-4: :func:`rebuild_results_from_evidence` reconstructs the in-memory DAG
  ``results`` map from the trusted :class:`~harness.core.fold.StepEvidence`
  projection so a resumed/crashed run does not re-run completed steps.
- F-5: :func:`unresolved_known_bad_steps` catches the e05087b6 defect where a
  revision silently re-adds a failed step with the same broken action.
- F-6: :class:`RecoveryBudget` / :func:`validate_local_repair` bound the
  step-local think-act loop (rounds, tool whitelist, no new side effects).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from harness.core.dag_types import ExecState, StepResult
from harness.core.fold import StepEvidence

# ── F-4: rebuild in-memory results from the trusted evidence projection ──

_EVIDENCE_STATE_MAP: dict[str, ExecState] = {
    "completed": ExecState.COMPLETED,
    "idempotent": ExecState.IDEMPOTENT,
    "unsuccessful": ExecState.UNSUCCESSFUL,
    "failed": ExecState.FAILED,
    "skipped": ExecState.SKIPPED,
    "cancelled": ExecState.CANCELLED,
    # A step that was mid-flight (running) or never started at crash time must be
    # resumed as PENDING so it re-executes — never silently as completed.
    "running": ExecState.PENDING,
    "pending": ExecState.PENDING,
    "": ExecState.PENDING,
}


def rebuild_results_from_evidence(evidence: dict[str, StepEvidence]) -> dict[str, StepResult]:
    """Rebuild a ``results`` map (as used by ``_execute_plan``) from evidence.

    Pure/deterministic. Terminal states are restored so the scheduler's
    ``topological_sort(completed_step_ids=...)`` skips them; a running/pending step
    becomes PENDING and is re-executed. Probe flags are preserved (a probe step
    that was unsuccessful is ``step_normal``).
    """
    results: dict[str, StepResult] = {}
    for sid, ev in evidence.items():
        exec_state = _EVIDENCE_STATE_MAP.get(ev.exec_state, ExecState.PENDING)
        results[sid] = StepResult(
            step_id=sid,
            exec_state=exec_state,
            output=ev.output,
            summary=ev.output_summary or "",
            error=ev.error,
            tool_call_id=ev.tool_call_id,
            probe=ev.probe,
        )
    return results


# ── F-5: revision coverage of known-bad steps ──

# Errors that look transient (network blip / timeout / server 5xx) are eligible
# for tool-level retry and are NOT treated as "known-bad action" that a revision
# must re-plan. ``5\d{2}\b`` also matches the semantic fallback message a tool
# emits for a 5xx response ("status_code=503 (op=lt, value=400)") — server-side
# errors are infra-transient by definition and safe to re-run on a read-only op.
_TRANSIENT_PATTERNS = re.compile(
    r"timeout|timed out|connection\s*(error|reset|refused|aborted)|temporar|"
    r"5\d{2}\s*(status|error)|rate.?limit|unavailable.*retry|ECONNRESET|ETIMEDOUT|"
    r"\b5\d{2}\b",
    re.IGNORECASE,
)

# Infrastructure/tool-unavailable errors are not fixed by re-running the same action.
_TOOL_UNAVAILABLE_PATTERNS = re.compile(
    r"browser unavailable|event loop|not supported|tool .* unavailable|no handler|"
    r"module not found|not installed|environment",
    re.IGNORECASE,
)


def _is_transient(error: str | None) -> bool:
    return bool(error) and bool(_TRANSIENT_PATTERNS.search(error or ""))


def unresolved_known_bad_steps(
    prev_evidence: dict[str, StepEvidence],
    patch_steps: list[dict],
) -> list[str]:
    """Return failed (non-transient) step ids that a revision patch fails to address.

    A previously failed step is "addressed" only if the patch changes its *tool*
    (a genuinely different action). Keeping the same tool means the same broken
    action would be replayed — the e05087b6 defect (s3 browser re-run with the same
    idempotency key). Transient failures (timeouts) are excluded: they belong to
    tool-level retry, not revision coverage.
    """
    patch_by_id = {str(s.get("step_id")): s for s in patch_steps if s.get("step_id")}
    unresolved: list[str] = []
    for sid, ev in prev_evidence.items():
        if ev.exec_state not in ("unsuccessful", "failed"):
            continue
        if _is_transient(ev.error):
            continue
        patch = patch_by_id.get(sid)
        if patch is None:
            unresolved.append(sid)
            continue
        patched_tool = str(patch.get("tool_name") or "")
        if patched_tool and patched_tool != ev.tool_name:
            continue  # tool switched → a different action, considered addressed
        # Same tool (or no tool declared) on a non-transient failure → still bad.
        unresolved.append(sid)
    return sorted(unresolved)


# ── F-5: failure tier classification ──


def classify_failure_tier(error: str | None, *, retryable: bool) -> str:
    """Mechanically classify a step failure into a recovery tier.

    Returns ``"tool_retry"`` for transient/retryable failures (handled by the
    Tool Layer RetryRunner), otherwise ``"step_repair"`` (escalate to bounded
    local repair; the scheduler escalates further to sub-DAG replan once the
    repair budget is exhausted).
    """
    if retryable or _is_transient(error):
        return "tool_retry"
    return "step_repair"


# ── F-6: bounded step-local think-act budget ──

# Tools that are inherently read-only and therefore safe for local repair.
_READ_ONLY_TOOLS = frozenset({"fetch_output"})
# Per-tool read-only operations (mutation-free verbs only).
_READ_ONLY_OPERATIONS: dict[str, frozenset[str]] = {
    "http_request": frozenset({"GET", "HEAD", "get", "head", ""}),
    "file_op": frozenset({"read", "list"}),
}


@dataclass(frozen=True)
class RecoveryBudget:
    """Trusted bounds for step-local repair (F-6).

    Local repair may never introduce a new mutating step or alter the delivery
    contracts; those are enforced structurally by :func:`validate_local_repair`
    and by the Tool Layer guardrails.
    """

    max_repair_rounds: int = 2
    allowed_tools: frozenset[str] = field(default_factory=frozenset)


@dataclass
class LocalRepairProposal:
    """A non-trusted LLM proposal for a step-local retry action."""

    step_id: str
    tool_name: str
    input: dict


@dataclass
class RepairDecision:
    allowed: bool
    reason: str = ""


def _is_read_only_action(tool_name: str, tool_input: dict, explicit: dict[str, bool]) -> bool:
    if tool_name in explicit:
        return bool(explicit[tool_name])
    if tool_name in _READ_ONLY_TOOLS:
        return True
    if tool_name in _READ_ONLY_OPERATIONS:
        if tool_name == "http_request":
            method = str(tool_input.get("method", "GET"))
            return method.upper() in {"GET", "HEAD"}
        op = str(tool_input.get("operation", ""))
        return op in _READ_ONLY_OPERATIONS[tool_name]
    # Unknown tool: fail closed.
    return False


def is_read_only_action(tool_name: str, tool_input: dict) -> bool:
    """Trusted whitelist: is this tool+operation safe to automatically re-run?

    Fail-closed for anything outside the declared read-only sets (unknown tool →
    False). Shared by F-5 (Tool Layer semantic retry — an auto re-run must not be
    able to repeat a side effect) and F-6 (step-local repair budget).
    """
    return _is_read_only_action(tool_name, tool_input or {}, {})


def validate_local_repair(
    proposal: LocalRepairProposal,
    *,
    round_used: int,
    budget: RecoveryBudget,
    step_tools_read_only: dict[str, bool],
) -> RepairDecision:
    """Decide mechanically whether a step-local repair proposal may execute.

    Trusted rules (no LLM): within round budget; tool in whitelist; action
    read-only (local repair must not introduce new side effects); non-empty.
    """
    if round_used > budget.max_repair_rounds:
        return RepairDecision(False, f"local repair budget exhausted (round {round_used} > {budget.max_repair_rounds})")
    if not proposal.tool_name:
        return RepairDecision(False, "empty repair proposal (no tool)")
    if proposal.tool_name not in budget.allowed_tools:
        return RepairDecision(False, f"tool '{proposal.tool_name}' not in local-repair whitelist")
    if not _is_read_only_action(proposal.tool_name, proposal.input or {}, step_tools_read_only):
        return RepairDecision(False, f"local repair action '{proposal.tool_name}' is mutating/side-effecting")
    return RepairDecision(True, "")
