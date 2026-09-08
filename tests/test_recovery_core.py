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
    """F-5 (A′ review fix): "known-bad action" = full (tool, normalized input) signature.

    与退化修订守卫 (revision_guard.find_degenerate_revised_steps) 共享同一"同一动作"
    定义：tool 或 input 任一变化 → 新的合法尝试，视为已修复；仅同 tool 且同 input
    （无法取得原始 input 时保守退化为 tool-only）才判 unresolved。
    """

    def _ev(self, sid: str, tool: str = "http_request", state: str = "unsuccessful", error: str = "boom") -> StepEvidence:
        return StepEvidence(step_id=sid, tool_name=tool, exec_state=state, error=error)

    def test_unaddressed_failed_browser_step_flagged(self):
        # e05087b6: s3 failed (browser env error), patch only carries s1/s2 (http).
        prev = {
            "s1": self._ev("s1", tool="browser", error="browser unavailable"),
            "s3": self._ev("s3", tool="browser", error="browser unavailable"),
        }
        patch_steps = [
            {"step_id": "s1", "tool_name": "http_request", "input": {"url": "http://a"}},
            {"step_id": "s2", "tool_name": "http_request", "input": {"url": "http://a"}},
        ]
        original_inputs = {"s1": {"url": "http://a"}, "s3": {"url": "http://a"}}
        unresolved = unresolved_known_bad_steps(prev, patch_steps, original_inputs=original_inputs)
        assert "s3" in unresolved  # not covered by the patch at all → restored unchanged
        assert "s1" not in unresolved  # tool switched → addressed, not flagged

    def test_retryable_failure_not_flagged(self):
        prev = {"s1": StepEvidence(step_id="s1", tool_name="http_request", exec_state="failed", error="ConnectionError: timeout")}
        patch_steps = [{"step_id": "s2", "tool_name": "http_request"}]
        assert unresolved_known_bad_steps(prev, patch_steps) == []

    def test_completed_step_not_flagged(self):
        prev = {"s1": StepEvidence(step_id="s1", tool_name="http_request", exec_state="completed")}
        assert unresolved_known_bad_steps(prev, []) == []

    def test_patch_replays_same_tool_and_same_input_flagged(self):
        # Patch keeps the SAME tool AND the SAME input → unchanged replay of the
        # known-bad action (e05087b6 class) → still unaddressed.
        prev = {"s3": self._ev("s3", tool="browser", error="browser unavailable")}
        patch_steps = [{"step_id": "s3", "tool_name": "browser", "input": {"url": "http://a"}}]
        original_inputs = {"s3": {"url": "http://a"}}
        assert "s3" in unresolved_known_bad_steps(prev, patch_steps, original_inputs=original_inputs)

    def test_same_tool_corrected_input_is_addressed(self):
        # Review finding: same tool + corrected input (e.g. fixed a wrong URL/field)
        # is a legitimate new attempt — NOT an unchanged replay → must not be flagged.
        prev = {"s3": self._ev("s3", tool="http_request", error="404 — wrong url path")}
        patch_steps = [{"step_id": "s3", "tool_name": "http_request", "input": {"url": "http://host/correct"}}]
        original_inputs = {"s3": {"url": "http://host/typo"}}
        assert unresolved_known_bad_steps(prev, patch_steps, original_inputs=original_inputs) == []

    def test_input_normalization_is_key_order_independent(self):
        # Semantically identical input differing only in key order is still the
        # same action (shared canonical normalizer) → unchanged replay → flagged.
        prev = {"s3": self._ev("s3", tool="http_request", error="boom")}
        patch_steps = [
            {
                "step_id": "s3",
                "tool_name": "http_request",
                "input": {"headers": {"b": 2, "a": 1}, "url": "http://x"},
            }
        ]
        original_inputs = {"s3": {"url": "http://x", "headers": {"a": 1, "b": 2}}}
        assert "s3" in unresolved_known_bad_steps(prev, patch_steps, original_inputs=original_inputs)

    def test_same_tool_without_original_input_stays_conservative(self):
        # Caller cannot supply the originally-failed input → input correction cannot
        # be proven → same tool remains unresolved (fail-closed, no false "addressed").
        prev = {"s3": self._ev("s3", tool="browser", error="browser unavailable")}
        patch_steps = [{"step_id": "s3", "tool_name": "browser", "input": {"url": "http://new"}}]
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
