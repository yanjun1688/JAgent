"""P1a · wiring：有模型才装 LLMRepairer，没模型仍是 HeuristicRepairer。"""

from __future__ import annotations

from jagent.agent.llm_repair import LLMRepairer
from jagent.agent.model import ScriptedModel
from jagent.harness.repair import HeuristicRepairer
from jagent.ports import NoModel
from jagent.wiring import build_app


class _Judge:
    """真实模型装配要求注入 judge；本用例不触发建议路径，只需要它存在。"""

    model = "fake-judge"

    def judge(self, request):
        return None


def test_real_model_wires_the_llm_repairer(tmp_path) -> None:
    model = ScriptedModel([])  # 构造期不调用模型，空脚本足够
    app = build_app(tmp_path / "app", model=model, judge=_Judge())
    try:
        assert isinstance(app.repairer, LLMRepairer)
        assert app.runner.repairer is app.repairer
    finally:
        app.close()


def test_no_model_keeps_the_heuristic_repairer(tmp_path) -> None:
    app = build_app(tmp_path / "app")
    try:
        assert isinstance(app.repairer, HeuristicRepairer)
        assert app.runner.repairer is app.repairer
    finally:
        app.close()


def test_nomodel_sentinel_is_not_a_model(tmp_path) -> None:
    app = build_app(tmp_path / "app", model=NoModel())
    try:
        assert isinstance(app.repairer, HeuristicRepairer)
    finally:
        app.close()


def test_explicit_repairer_wins_over_the_model(tmp_path) -> None:
    explicit = HeuristicRepairer()
    app = build_app(
        tmp_path / "app", model=ScriptedModel([]), judge=_Judge(), repairer=explicit
    )
    try:
        assert app.repairer is explicit
    finally:
        app.close()
