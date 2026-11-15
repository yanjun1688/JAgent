"""Wave 2 · C3 · Citation.seq 由系统回填（grounding §2.1 / §8 / D-018）。

模型看不到事件序号：它给的 ``seq`` 一律丢弃；系统按 ``(step_id, field)``
从 ``step.finished``（同一 step 取最后一次）或 ``artifact.written`` 解析。
"""

from __future__ import annotations

from pathlib import Path

from jagent.contracts import Advice, Citation, EventType
from jagent.wiring import build_app

RUN_ID = "run_seq"


def _app(tmp_path: Path):
    return build_app(tmp_path)


def _finished(app, *, step_id: str, observations: dict[str, str], artifacts=()):
    return app.writer.emit(
        EventType.STEP_FINISHED,
        {
            "step_id": step_id,
            "status": "ok",
            "artifact_refs": list(artifacts),
            "observations": observations,
        },
        run_id=RUN_ID,
        step_id=step_id,
    )


def _citation(step_id: str, field: str, *, seq: int | None = 999) -> Citation:
    return Citation(step_id=step_id, field=field, label="来源", seq=seq)


def test_backfill_resolves_step_finished_and_artifact_and_discards_model_seq(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    try:
        finished = _finished(app, step_id="s1", observations={"data.pe": "45"})
        artifact = app.writer.emit(
            EventType.ARTIFACT_WRITTEN,
            {"artifact_id": "art-1", "sha256": "x" * 64, "bytes": 12},
            run_id=RUN_ID,
            step_id="s1",
        )
        advice = Advice(
            body="PE 45。",
            refs=(
                _citation("s1", "data.pe"),          # step.finished 观测
                _citation("s1", "art-1"),            # artifact 工件
                _citation("s1", "nope"),             # 解析不到
                _citation("s9", "data.pe"),          # 不在本次 run
            ),
        )

        resolved = app.session.backfill_citation_seqs(advice, RUN_ID)
    finally:
        app.close()

    assert [c.seq for c in resolved.refs] == [
        finished.seq,
        artifact.seq,
        None,
        None,
    ]
    # 模型给的 999 从未被采信。
    assert 999 not in [c.seq for c in resolved.refs]


def test_backfill_uses_the_latest_step_finished_for_the_same_step(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        _finished(app, step_id="s1", observations={"data.pe": "40"})
        latest = _finished(app, step_id="s1", observations={"data.pe": "45"})
        advice = Advice(body="PE 45。", refs=(_citation("s1", "data.pe"),))

        resolved = app.session.backfill_citation_seqs(advice, RUN_ID)
    finally:
        app.close()

    assert resolved.refs[0].seq == latest.seq


def test_backfill_without_run_id_still_discards_model_seq(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        advice = Advice(body="只给框架。", refs=(_citation("s1", "data.pe", seq=77),))
        resolved = app.session.backfill_citation_seqs(advice, None)
        # 没有 run 就没有可解析的事件；模型值必须消失。
        assert [c.seq for c in resolved.refs] == [None]
    finally:
        app.close()


def test_advice_payload_keeps_refs_with_seq(tmp_path: Path) -> None:
    """``_advice_dict`` 不许把 refs 丢掉（C3 事件正确性）。"""
    from jagent.app.session import _advice_dict

    payload = _advice_dict(
        Advice(body="PE 45。", refs=(Citation("s1", "data.pe", "行情", 7),))
    )

    assert payload["refs"] == [{"step_id": "s1", "field": "data.pe", "label": "行情", "seq": 7}]
