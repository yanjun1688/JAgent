"""JAgent v8 · 结果渲染（Reporter）。

模板渲染，不调模型：同一 World 渲染两次必须得到同一个 Result。
措辞可以变，事实不能变——每个字段都只能来自 RunState，不做推断、不补装饰。
"""

from __future__ import annotations

from ..contracts import Result, RunVerdict, StepReport
from .fold import RunState, StepState


def _verdict_of(status: str) -> RunVerdict:
    if status == "completed":
        return RunVerdict.COMPLETED
    if status == "failed":
        return RunVerdict.FAILED
    # planning / running / waiting_human / cancelled 都不是「完成」，
    # 对投递方一律按取消处理：没有结果就是没有结果。
    return RunVerdict.CANCELLED


def _failure_tag(step: StepState) -> str:
    """失败步骤的短标签：分类优先，其次错误码；都没有就不加括号。"""
    return step.failure_class or step.error_code or ""


class Reporter:
    """把 RunState 投影成可投递的 Result。纯函数式：不写事件、不改状态。"""

    def render(self, run: RunState, *, generated_by: str = "template",
               status_override: str | None = None) -> Result:
        """`status_override` 用于「先出报告、后写终态事件」的顺序：
        报告里的 verdict 必须反映这次运行将要落定的结论，而不是此刻还没翻的状态。"""
        status = status_override or run.status
        states = run.step_list()

        steps = tuple(
            StepReport(
                id=step.id,
                status=step.status,
                artifact_refs=tuple(step.artifact_refs),
                checks=tuple(step.checks),
                evidence_seqs=tuple(step.evidence_seqs),
            )
            for step in states
        )

        passed = [s for s in states if s.status == "passed"]
        failed = [s for s in states if s.status == "failed"]

        summary = f"{len(passed)}/{len(states)} 步通过"
        if failed:
            detail = "、".join(
                f"{s.id}（{_failure_tag(s)}）" if _failure_tag(s) else s.id for s in failed
            )
            summary += f"；失败：{detail}"
        elif run.fail_reason and status == "failed":
            summary += f"；失败：{run.fail_reason}"

        return Result(
            run_id=run.run_id,
            verdict=_verdict_of(status),
            steps=steps,
            summary=summary,
            remaining_plan=tuple(s.id for s in states if s.status != "passed"),
            budget_used=dict(run.budget),
            generated_by=generated_by,
        )
