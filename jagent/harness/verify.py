"""JAgent v8 · 断言求值。

断言是代码，不是模型。这里做两件事：

1. `ast.parse(expr, mode="eval")` 之后按白名单走一遍语法树，只放
   「看得见的比较」——没有属性访问、没有下标、没有 import、没有 lambda；
2. 在 `{"__builtins__": {}}` 的空命名空间里求值，名字只能来自 `observations`。

判不了不是通过：表达式引用了一个观测里没有的名字（或 `Verify.needs` 声明了
却没出现），就记 `evidence_missing` 并判 fail。这条是硬规则——"我不知道"
和 "它通过了" 是两件事，日志里不能混。

`step.verify is None` 返回 fail：没有断言即 fail-closed，理由 `evidence_missing`。
这是 G-09 的判定点，不是策略点——调用方不决定接不接受，Verifier 自己判死。
调用方只许匹配 `evidence_missing` 前缀并检查 `checks` 是否为空；**禁止解析 `:` 后的后缀**——
后缀只给人读，不是枚举。
"""

from __future__ import annotations

import ast
from typing import Any, Callable

from ..contracts import Step, StepOutcome, VerifyOutcome

#: JSON 风格字面量 → Python 字面量。
#: Planner 在 JSON 语境里起草断言，写 `ok == true` / `x == null` 是自然的；
#: 按 Python 语法它们只是普通名字，会被判成「观测缺失」而假失败。
#: 归一发生在语法树上，字符串字面量里的 "true" 不受影响。
_JSON_LITERALS: dict[str, Any] = {"true": True, "false": False, "null": None}


class _JsonLiterals(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        if node.id in _JSON_LITERALS:
            return ast.copy_location(ast.Constant(_JSON_LITERALS[node.id]), node)
        return node

#: 允许调用的函数。多一个都不行：`open` / `eval` / `getattr` 不在名单里。
_ALLOWED_FUNCS: dict[str, Callable[..., Any]] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "abs": abs,
    "any": any,
    "all": all,
}

#: 允许的语法节点。两处是白名单的「容器」而非行为：`ast.BinOp` 是 `Add/Sub/Mult/Div`
#: 的宿主，`ast.Tuple` / `ast.List` 只作为 `in` 的字面量存在（`x in ("a", "b")`）。
#: 没有 BinOp，算子白名单就永远走不到；两处都是惰性节点，不带任何能力。
_ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expression,
        ast.BoolOp,
        ast.Compare,
        ast.UnaryOp,
        ast.BinOp,
        ast.Name,
        ast.Constant,
        ast.Call,
        ast.Tuple,
        ast.List,
        ast.Load,
        # boolop / unaryop / cmpop 的算子节点
        ast.And,
        ast.Or,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.In,
        ast.NotIn,
        ast.Eq,
        ast.NotEq,
        ast.Gt,
        ast.GtE,
        ast.Lt,
        ast.LtE,
        ast.Is,
        ast.IsNot,
        # binop 的算子节点
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
    }
)

_DETAIL_LIMIT = 200


def _short(value: Any, limit: int = _DETAIL_LIMIT) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _unsupported(tree: ast.AST) -> list[str]:
    """白名单之外的东西，全部点名报出来。"""
    bad: list[str] = []
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            bad.append(type(node).__name__)
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                bad.append("Call(target)")
            elif node.func.id not in _ALLOWED_FUNCS:
                bad.append(f"Call({node.func.id})")
            elif node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
                bad.append("Call(*args/**kwargs)")
    return list(dict.fromkeys(bad))


def _missing_names(
    tree: ast.AST, observations: dict[str, Any], needs: tuple[str, ...]
) -> list[str]:
    """观测里取不到的名字。被当作被调函数用的白名单函数不算观测。"""
    callees = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    missing: list[str] = []

    def note(name: str) -> None:
        if name not in observations and name not in missing:
            missing.append(name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Name):
            continue
        if node.id in _ALLOWED_FUNCS and id(node) in callees:
            continue
        note(node.id)
    for name in needs:  # 声明了要读却没读到，也是证据缺失
        note(name)
    return missing


def _failed(expr: str, reason: str) -> VerifyOutcome:
    return VerifyOutcome(
        passed=False,
        checks=({"expr": expr, "passed": False, "detail": reason},),
        reason=reason,
    )


class Verifier:
    """纯函数式：只读 `StepOutcome.observations`，不写事件、不调工具。"""

    def verify(self, step: Step, outcome: StepOutcome) -> VerifyOutcome:
        if step.verify is None:
            # 没有断言 = 没有判定；checks 保持空，不造一条假检查。
            return VerifyOutcome(
                passed=False,
                checks=(),
                reason="evidence_missing: no_assertion",
            )

        expr = step.verify.expr
        observations = dict(outcome.observations or {})

        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            return _failed(expr, f"eval_error: {exc}")

        tree = _JsonLiterals().visit(tree)
        ast.fix_missing_locations(tree)

        problems = _unsupported(tree)
        if problems:
            return _failed(expr, f"eval_error: unsupported syntax: {', '.join(problems)}")

        missing = _missing_names(tree, observations, tuple(step.verify.needs))
        if missing:
            names = ", ".join(sorted(missing))
            return VerifyOutcome(
                passed=False,
                checks=({"expr": expr, "passed": False, "detail": f"missing observations: {names}"},),
                reason=f"evidence_missing: {names}",
            )

        try:
            value = eval(  # noqa: S307 - 语法树已在上面按白名单筛过，命名空间是空的
                compile(tree, "<verify>", "eval"),
                {"__builtins__": {}},
                {**observations, **_ALLOWED_FUNCS},
            )
        except Exception as exc:
            return _failed(expr, f"eval_error: {type(exc).__name__}: {exc}")

        passed = bool(value)
        check = {"expr": expr, "passed": passed, "detail": f"{expr} -> {_short(value)}"}
        if passed:
            return VerifyOutcome(passed=True, checks=(check,), reason="")
        return VerifyOutcome(
            passed=False,
            checks=(check,),
            reason=f"assertion_failed: {expr} -> {_short(value)}",
        )
