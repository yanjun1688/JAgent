"""JAgent v8 · Planner 草稿校验（不是组件之间的门）。

只在 Planner 成稿前检查它自己的草稿；不合格就丢弃重试，不返回半成品。
它只做确定性检查，不调模型、不改计划。

最重要的一条是 `needs ⊆ provides`：断言要读的观测，必须是该工具真的会产出的。
少了这一条，草稿可以冻结成功、执行成功、然后在 Verifier 那里判"没有证据"——
那时报错已经晚了，用户拿到的是一次莫名其妙的失败。

`evidence_requires` 同理：`step_id` 必须存在于计划里，`field` 必须是该步工具
声明过的观测名。否则只有执行到收尾时才报"证据不齐"，同样太晚。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ..contracts import Plan, ToolSpec


@dataclass(frozen=True)
class PlanIssue:
    code: str
    step_id: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "step_id": self.step_id, "detail": self.detail}


def _parse_expr(expr: str) -> str | None:
    try:
        ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        return f"syntax error at {exc.offset}: {exc.msg}"
    return None


def _names_anything(expr: str) -> bool:
    """断言里至少要引用一个名字（观测字段），否则它是常量真。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return True  # 语法错由 _parse_expr 报，不在这里重复
    return any(isinstance(node, ast.Name) for node in ast.walk(tree))


def check_plan(
    plan: Plan,
    specs: dict[str, ToolSpec],
    *,
    max_steps: int = 8,
) -> list[PlanIssue]:
    issues: list[PlanIssue] = []

    if not plan.steps:
        return [PlanIssue("EMPTY_PLAN", "", "计划没有任何步骤")]
    if len(plan.steps) > max_steps:
        issues.append(PlanIssue("TOO_MANY_STEPS", "", f"{len(plan.steps)} > {max_steps}"))

    seen: set[str] = set()
    for step in plan.steps:
        if not step.id:
            issues.append(PlanIssue("EMPTY_STEP_ID", "", "步骤缺少 id"))
            continue
        if step.id in seen:
            issues.append(PlanIssue("DUPLICATE_STEP_ID", step.id, "步骤 id 重复"))
        seen.add(step.id)

        spec = specs.get(step.tool)
        if spec is None:
            issues.append(PlanIssue("UNKNOWN_TOOL", step.id, f"工具 {step.tool!r} 未注册"))
            continue

        for dep in step.depends_on:
            if dep not in {s.id for s in plan.steps}:
                issues.append(PlanIssue("UNKNOWN_DEPENDENCY", step.id, f"依赖 {dep!r} 不存在"))

        if step.verify is None:
            issues.append(PlanIssue("MISSING_VERIFY", step.id, "每一步都必须带断言"))
            continue

        err = _parse_expr(step.verify.expr)
        if err is not None:
            issues.append(PlanIssue("BAD_EXPR", step.id, err))
        elif not _names_anything(step.verify.expr):
            # 恒真断言（`1 == 1` / `True`）没有任何约束力，却能把 run 判成完成（红队 RT-L1）。
            issues.append(PlanIssue("CONSTANT_ASSERTION", step.id, "断言没有引用任何观测值"))
        if not step.verify.needs:
            issues.append(PlanIssue("EMPTY_NEEDS", step.id, "断言没声明要读哪些观测"))
        else:
            missing = [n for n in step.verify.needs if n not in spec.observations]
            if missing:
                issues.append(
                    PlanIssue(
                        "NEEDS_NOT_PROVIDED",
                        step.id,
                        f"断言要读 {missing}，但 {spec.name} 只产出 {list(spec.observations)}",
                    )
                )

        if spec.side_effect.value == "irreversible" and not step.gated:
            # 不是错误：计划门不管"要不要人确认"，那是 Runner 的执行策略（见 runner._needs_approval）。
            pass

    by_id = plan.by_id()
    for req in plan.evidence_requires:
        step = by_id.get(req.step_id)
        if step is None:
            issues.append(
                PlanIssue(
                    "EVIDENCE_STEP_UNKNOWN",
                    req.step_id,
                    f"evidence_requires 引用了不存在的步骤 {req.step_id!r}",
                )
            )
            continue
        spec = specs.get(step.tool)
        if spec is None:
            # 未知工具已由上面的 UNKNOWN_TOOL 报过，不在这里重复。
            continue
        if req.field not in spec.observations:
            issues.append(
                PlanIssue(
                    "EVIDENCE_FIELD_UNDECLARED",
                    step.id,
                    f"evidence_requires 引用 {req.field!r}，"
                    f"但 {spec.name} 只产出 {list(spec.observations)}",
                )
            )

    try:
        plan.order()
    except ValueError as exc:  # 环或未知依赖
        issues.append(PlanIssue("CYCLIC_OR_BROKEN", "", str(exc)))

    return issues


def first_message(issues: list[PlanIssue]) -> str:
    if not issues:
        return ""
    head = issues[0]
    where = f"（第 {head.step_id} 步）" if head.step_id else ""
    return f"{head.code}{where}: {head.detail}"
