"""P1b · C1：复用键是纯函数，形状与语义都必须稳定。

键 = ``(step_id, digest_of(args), tuple(sorted(上游产物 hash)))``。
这几条钉子保证 Runner 的比较不会因为 args 的键序、依赖声明顺序或
"同一次调用的描述方式"不同而误判成"变了"（那样会把能复用的步骤重跑）。
"""

from __future__ import annotations

from jagent.contracts import Step, Verify, digest_of
from jagent.harness.repair import reuse_key


def _step(
    step_id: str = "s1",
    *,
    args: dict | None = None,
    verify: str = "value == 1",
) -> Step:
    return Step(
        id=step_id,
        tool="probe",
        args={"value": 1} if args is None else args,
        verify=Verify(verify, needs=("value",)),
    )


def test_reuse_key_has_the_documented_shape() -> None:
    key = reuse_key(_step(args={"value": 1}), ["b", "a", "a"])

    assert key == ("s1", digest_of({"value": 1}), ("a", "a", "b"))


def test_reuse_key_is_independent_of_dict_and_ref_order() -> None:
    left = _step(args={"a": 1, "b": 2})
    right = _step(args={"b": 2, "a": 1})

    assert reuse_key(left, ["ref-b", "ref-a"]) == reuse_key(right, ["ref-a", "ref-b"])


def test_reuse_key_changes_when_args_change() -> None:
    assert reuse_key(_step(args={"value": 1}), ()) != reuse_key(_step(args={"value": 2}), ())


def test_reuse_key_changes_when_upstream_artifacts_change() -> None:
    before = reuse_key(_step(), ["artifact-a"])
    after = reuse_key(_step(), ["artifact-b"])

    assert before != after
    assert before[2] == ("artifact-a",)


def test_reuse_key_changes_when_step_id_changes() -> None:
    assert reuse_key(_step("s1"), ()) != reuse_key(_step("s2"), ())


def test_reuse_key_ignores_verify_text_on_purpose() -> None:
    """断言不是这次工具调用的输入：只改断言时，产物没变，下游可以复用。

    这条是"改一步就把整条链重跑"的正面理由，不是遗漏——见 D-027。
    """

    relaxed = _step(verify="value >= 1")
    strict = _step(verify="value == 1")

    assert reuse_key(relaxed, ("artifact-a",)) == reuse_key(strict, ("artifact-a",))
