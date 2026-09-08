"""F-4/F-5/F-6 (v3.4): 受信恢复核心 — 执行态重建、修订合并覆盖校验、局部修复预算。

回归 run e05087b6：
- F-4: resume 时执行进度（results）是纯内存、无法从事件流重建，崩溃即丢。
- F-5: LLM 修订补丁只修了 s1/s2（换 http），失败的 s3（browser）未被补丁覆盖却被
  merge 原样加回计划，用同一幂等键重放已知坏动作并再次失败（seq22/seq23）。
- F-6: 步骤失败后无 bounded 局部修复，只能全局 revise（弱模型漏修/谎报）。

本模块全为纯函数，无 I/O、无 LLM —— 恢复边界的"强制权"归受信组件（AGENTS.md §2.2）。
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.core.dag_types import ExecState
from harness.core.fold import StepEvidence
from harness.core.recovery import (
    LocalRepairProposal,
    RecoveryBudget,
    classify_failure_tier,
    rebuild_results_from_evidence,
    unresolved_known_bad_steps,
    validate_local_repair,
)


# ── F-4: rebuild results from trusted evidence projection ──


class TestRebuildFromEvidence:
    def _ev(self, sid: str, state: str, **kw) -> StepEvidence:
        e = StepEvidence(step_id=sid, plan_id="p1", tool_name=kw.pop("tool", "http_request"))
        e.exec_state = state
        for k, v in kw.items():
            setattr(e, k, v)
        return e

    def test_completed_steps_become_terminal_results(self):
        ev = {
            "s1": self._ev("s1", "completed", tool_call_id="tc-1"),
            "s2": self._ev("s2", "unsuccessful", tool_call_id="tc-2"),
            "s4": self._ev("s4", "skipped", tool="browser"),
        }
        results = rebuild_results_from_evidence(ev)
        assert results["s1"].exec_state == ExecState.COMPLETED
        assert results["s1"].should_not_rerun is True
        assert results["s2"].exec_state == ExecState.UNSUCCESSFUL
        assert results["s2"].should_not_rerun is False  # re-runnable
        assert results["s4"].exec_state == ExecState.SKIPPED

    def test_running_step_not_treated_as_completed(self):
        ev = {"s1": self._ev("s1", "running", tool_call_id="tc-1")}
        results = rebuild_results_from_evidence(ev)
        # A step mid-flight at crash time must not be resumed as completed.
        assert results["s1"].exec_state != ExecState.COMPLETED
        assert results["s1"].should_not_rerun is False

    def test_probe_unsuccessful_is_normal(self):
        ev = {"s9": self._ev("s9", "unsuccessful", probe=True)}
        results = rebuild_results_from_evidence(ev)
        assert results["s9"].step_normal is True

    def test_deterministic(self):
        ev = {"s1": self._ev("s1", "completed")}
        a = rebuild_results_from_evidence(ev)
        b = rebuild_results_from_evidence(ev)
        assert a["s1"].exec_state == b["s1"].exec_state


# ── F-5: revision merge must not silently replay known-bad actions ──


class TestUnresolvedKnownBad:
    def test_unaddressed_failed_browser_step_flagged(self):
        # e05087b6: s3 failed (browser env error), patch only carries s1/s2 (http).
        prev = {
            "s1": StepEvidence(step_id="s1", tool_name="browser", exec_state="unsuccessful", error="browser unavailable"),
            "s3": StepEvidence(step_id="s3", tool_name="browser", exec_state="unsuccessful", error="browser unavailable"),
        }
        patch_steps = [
            {"step_id": "s1", "tool_name": "http_request"},
            {"step_id": "s2", "tool_name": "http_request"},
        ]
        unresolved = unresolved_known_bad_steps(prev, patch_steps)
        assert "s3" in unresolved
        # s1 was failed but the patch changes its tool → addressed, not flagged.
        assert "s1" not in unresolved

    def test_retryable_failure_not_flagged(self):
        prev = {"s1": StepEvidence(step_id="s1", tool_name="http_request", exec_state="failed", error="ConnectionError: timeout")}
        patch_steps = [{"step_id": "s2", "tool_name": "http_request"}]
        assert unresolved_known_bad_steps(prev, patch_steps) == []

    def test_completed_step_not_flagged(self):
        prev = {"s1": StepEvidence(step_id="s1", tool_name="http_request", exec_state="completed")}
        assert unresolved_known_bad_steps(prev, []) == []

    def test_patch_keeps_failed_step_but_same_tool_flagged(self):
        # Patch includes s3 but keeps the same broken tool (browser) → still unaddressed.
        prev = {"s3": StepEvidence(step_id="s3", tool_name="browser", exec_state="unsuccessful", error="browser unavailable")}
        patch_steps = [{"step_id": "s3", "tool_name": "browser"}]
        assert "s3" in unresolved_known_bad_steps(prev, patch_steps)


# ── Failure tier classification (F-5) ──


class TestFailureTier:
    def test_transient_error_is_tool_retry(self):
        assert classify_failure_tier("ConnectionError: timeout", retryable=True) == "tool_retry"

    def test_semantic_env_error_escalates_past_tool_retry(self):
        # browser unavailable on this event loop — not a transient network blip.
        assert classify_failure_tier("Browser unavailable on this event loop", retryable=False) == "step_repair"

    def test_hard_failure_escalates_to_replan(self):
        assert classify_failure_tier("boom", retryable=False) == "step_repair"


# ── F-6: bounded local think-act budget (trusted enforcement) ──


@dataclass
class _Tool:
    name: str
    read_only: bool


class TestLocalRepairBudget:
    def _budget(self, max_rounds=2):
        return RecoveryBudget(
            max_repair_rounds=max_rounds,
            allowed_tools={"http_request", "fetch_output"},
        )

    def test_proposal_within_budget_accepted(self):
        budget = self._budget()
        prop = LocalRepairProposal(step_id="s1", tool_name="http_request", input={"url": "https://x"})
        decision = validate_local_repair(prop, round_used=1, budget=budget, step_tools_read_only={})
        assert decision.allowed is True

    def test_round_over_budget_rejected(self):
        budget = self._budget(max_rounds=1)
        prop = LocalRepairProposal(step_id="s1", tool_name="http_request", input={"url": "https://x"})
        decision = validate_local_repair(prop, round_used=2, budget=budget, step_tools_read_only={})
        assert decision.allowed is False
        assert "budget" in decision.reason

    def test_non_whitelisted_tool_rejected(self):
        budget = self._budget()
        prop = LocalRepairProposal(step_id="s1", tool_name="browser", input={"action": "navigate"})
        decision = validate_local_repair(prop, round_used=1, budget=budget, step_tools_read_only={})
        assert decision.allowed is False
        assert "tool" in decision.reason

    def test_mutating_tool_rejected_even_if_whitelisted(self):
        budget = RecoveryBudget(max_repair_rounds=2, allowed_tools={"file_op", "http_request"})
        # file_op write is mutating → local repair must not introduce side effects.
        prop = LocalRepairProposal(step_id="s1", tool_name="file_op", input={"operation": "write", "path": "f", "content": "x"})
        read_only = {"file_op": False}
        decision = validate_local_repair(prop, round_used=1, budget=budget, step_tools_read_only=read_only)
        assert decision.allowed is False
        assert "side" in decision.reason or "mutat" in decision.reason

    def test_empty_proposal_rejected(self):
        budget = self._budget()
        prop = LocalRepairProposal(step_id="s1", tool_name="", input={})
        decision = validate_local_repair(prop, round_used=1, budget=budget, step_tools_read_only={})
        assert decision.allowed is False
