"""可观测性 S1 · 模型调用事件（D-031）。

``model.called`` 是路线图 14 的 Run 原语采集面：purpose / token（ModelReply
已解析 usage）/ 耗时 / 长度；prompt 与回复**全文落产物库**（用户裁决），
事件只留 id——事件溯源已是 Trace/Thread，这里补的是 Run 原语。

wiring 组合方式：基础模型只有一个，每个组件拿到带 purpose 标签的
``ObservationModel`` 视图——``ModelPort`` 签名零改动，15 个测试替身不受影响。
已知边界（D-031）：create_app 直连装配的 judge 不经观测视图。
"""

from __future__ import annotations

import json

from jagent.agent.llm_judge import LLMJudge
from jagent.agent.model import ScriptedModel
from jagent.harness.observability import ObservationModel
from jagent.contracts import ReplyDraft
from jagent.ports import ModelReply
from jagent.harness.artifacts import ArtifactStore
from jagent.harness.fold import World
from jagent.harness.store import EventStore
from jagent.harness.writer import Writer
from jagent.wiring import build_app
from tests.support import DraftPlanner

from .conftest import SESSION_ID


def _router_json() -> str:
    return json.dumps({"type": "advise", "confidence": 0.9, "reason": "recording"})


def test_observation_model_emits_model_called(tmp_path) -> None:
    """Given ObservationModel 包着 ScriptedModel，When complete，
    Then model.called 落盘：purpose/token/耗时/长度齐全，
    prompt 全文落产物库且内容可按 id 取回。"""
    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, World(), SESSION_ID)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    inner = ScriptedModel([ModelReply(text="pong", tokens_in=100, tokens_out=20)])
    observed = ObservationModel(inner, purpose="plan", writer=writer, artifacts=artifacts)

    reply = observed.complete(system="你是 JAgent 的计划器。", prompt="任务说明", temperature=0.0)

    assert reply.text == "pong"
    called = [e.payload for e in store.read() if e.type.value == "model.called"]
    assert len(called) == 1
    payload = called[0]
    assert payload["purpose"] == "plan"
    assert payload["tokens_in"] == 100 and payload["tokens_out"] == 20
    assert payload["duration_ms"] >= 0
    assert payload["prompt_chars"] > 0 and payload["reply_chars"] == 4
    assert payload["prompt_artifact_id"]
    stored = artifacts.get_text(payload["prompt_artifact_id"])
    assert "计划器" in stored and "任务说明" in stored, "prompt 全文必须按 id 可取回"
    store.close()


def test_failed_model_call_is_still_observed(tmp_path) -> None:
    """Given 内层模型抛 outage，When complete，
    Then model.called 照样落盘（error 字段 + 已耗时长 + prompt 全文）后**原样上抛**——
    故障恰恰是最需要观测的时刻，静默等于没监控。"""
    import pytest

    from jagent.ports import ModelUnavailable

    store = EventStore(tmp_path / "events.db")
    writer = Writer(store, World(), SESSION_ID)
    artifacts = ArtifactStore(tmp_path / "artifacts")

    class DownModel:
        model = "down-1"

        def complete(self, *, system: str, prompt: str, temperature: float = 0.0) -> ModelReply:
            raise ModelUnavailable("upstream down")

    observed = ObservationModel(DownModel(), purpose="plan", writer=writer, artifacts=artifacts)

    with pytest.raises(ModelUnavailable):
        observed.complete(system="系统提示", prompt="任务", temperature=0.0)

    called = [e.payload for e in store.read() if e.type.value == "model.called"]
    assert len(called) == 1, "失败的模型调用必须被观测"
    assert "ModelUnavailable" in called[0]["error"]
    assert called[0]["tokens_in"] == 0 and called[0]["tokens_out"] == 0
    assert called[0]["prompt_artifact_id"], "失败调用的 prompt 全文也落盘"
    store.close()


def test_wiring_observes_router_calls(tmp_path) -> None:
    """Given build_app 接真实观测视图（ScriptedModel 按脚本回 router JSON），
    When handle_turn，Then model.called 的 purpose=router——
    wiring 的 purpose 视图生效，fold 账本里 token 合计随行。"""
    model = ScriptedModel(
        [ModelReply(text=_router_json(), tokens_in=100, tokens_out=20)]
    )
    app = build_app(
        tmp_path,
        model=model,
        judge=LLMJudge(model),
        planner=DraftPlanner([ReplyDraft(text="好的。")]),
    )
    try:
        out = app.session.handle_turn("给我个分析框架")
        assert out.status == "answered"
        called = [e.payload for e in app.store.read() if e.type.value == "model.called"]
        assert called and called[0]["purpose"] == "router"
        assert called[0]["tokens_in"] == 100 and called[0]["tokens_out"] == 20
        assert app.session.state.tokens_in == 100
        assert app.session.state.tokens_out == 20
        assert len(app.session.state.model_calls) == 1
    finally:
        app.close()
