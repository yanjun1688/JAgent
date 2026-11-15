"""JAgent v8 · 已接线扩展组件的最小契约测试（G-23 re-baseline）。

``harness/ext/`` 停车场已清空（contract_snapshot / judge 迁入 harness 正编，
resources.py 作为未接线的投机组件删除——容器落地时从 git 复活）。
这里只证明「接口存在且默认实现是安全的」：
* 契约快照变了必须 verify=False；
* NoJudge 永远返回 None，且 JudgeVerdict 里没有 PASS。
"""

from __future__ import annotations

import pytest

from jagent.contracts import Idempotency, SideEffect, ToolSpec
from jagent.harness.contract_snapshot import snapshot, verify
from jagent.harness.judge import JudgeRequest, JudgeVerdict, NoJudge


def _spec(**overrides) -> ToolSpec:
    base = {
        "name": "fs.read",
        "version": "1",
        "args_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "side_effect": SideEffect.NONE,
        "idempotency": Idempotency.NATURAL,
        "error_codes": ("sandbox_denied", "not_found"),
        "timeout_s": 30.0,
        "readonly": True,
        "description": "read a file",
        "observations": ("ok", "content"),
    }
    base.update(overrides)
    return ToolSpec(**base)


# --------------------------------------------------------------------------- #
# contract_snapshot
# --------------------------------------------------------------------------- #


def test_snapshot_is_stable_and_order_independent() -> None:
    first = _spec(name="fs.read")
    second = _spec(name="fs.write", readonly=False, side_effect=SideEffect.REVERSIBLE)

    digest = snapshot([first, second])

    assert digest == snapshot([second, first])
    assert digest == snapshot([first, second])
    assert verify(digest, [first, second]) is True


def test_verify_rejects_a_changed_or_malformed_contract() -> None:
    digest = snapshot([_spec()])

    # 观测面变了：旧断言可能变成 evidence_missing，必须视为契约变化
    assert verify(digest, [_spec(observations=("ok",))]) is False
    # 副作用变了：同样的计划不再具有同样的风险
    assert verify(
        digest,
        [_spec(readonly=False, side_effect=SideEffect.REVERSIBLE)],
    ) is False
    # 空/伪造/异前缀一律 fail-closed
    assert verify("", [_spec()]) is False
    assert verify("tool-contract-v1:deadbeef", [_spec()]) is False


# --------------------------------------------------------------------------- #
# judge
# --------------------------------------------------------------------------- #


def test_no_judge_never_decides() -> None:
    request = JudgeRequest(claim="the file exists", evidence={"ok": True}, reason="no judge")

    assert NoJudge().judge(request) is None


def test_judge_cannot_return_pass() -> None:
    values = {verdict.value for verdict in JudgeVerdict}

    assert values == {"reject", "undecidable"}
    assert "pass" not in values
    with pytest.raises(ValueError):
        JudgeVerdict("pass")
