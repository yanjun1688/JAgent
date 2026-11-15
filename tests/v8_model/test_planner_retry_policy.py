"""Wave 2 · C2 · 重试策略只在 Session（D-012）。

Planner 一次调用只产一稿：成功返回 Plan，失败抛 `PlanDraftError`。
这里守住两件事：

* `LLMPlanner` 不再接受 `max_attempts`（内部循环已删除）；
* 生产装配（`wiring.build_app`）不再传这个参数——如果还传，构造会直接 TypeError。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from jagent.agent.llm_planner import LLMPlanner
from jagent.harness.judge import JudgeVerdict
from jagent.ports import ModelReply
from jagent.wiring import build_app


class _Judge:
    """可返回 REJECT 的 judge 替身；本文件不跑建议路径，默认没有意见。"""

    model = "fake-judge"

    def judge(self, request: object) -> JudgeVerdict | None:
        return None


class _Model:
    """能构造 app 即可；本文件不跑 turn。"""

    def complete(
        self, *, system: str = "", prompt: str = "", temperature: float = 0.0
    ) -> ModelReply:
        return ModelReply(text='{"mode":"reply","text":"好"}')


def test_llm_planner_has_no_max_attempts_parameter() -> None:
    params = inspect.signature(LLMPlanner.__init__).parameters
    assert "max_attempts" not in params, "重试次数属于 Session 的 budget，不属于 Planner"


def test_wiring_builds_llm_planner_without_max_attempts(tmp_path: Path) -> None:
    app = build_app(tmp_path, model=_Model(), judge=_Judge())
    try:
        assert isinstance(app.session.planner, LLMPlanner)
    finally:
        app.close()
